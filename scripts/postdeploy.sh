#!/usr/bin/env bash
set -euo pipefail

KB_ACCESS_MODE="${KB_ACCESS_MODE:-sample}"
if [ -n "${AZURE_SUBSCRIPTION_ID:-}" ]; then
  az() { command az "$@" --subscription "$AZURE_SUBSCRIPTION_ID"; }
fi
case "$KB_ACCESS_MODE" in
  purview|sample) ;;
  *) echo "KB_ACCESS_MODE must be 'sample' or 'purview'." >&2; exit 1 ;;
esac

echo ""
echo "=== Post-Deploy: Granting search permissions to agent ==="
echo ""

SEARCH_RESOURCE_ID="${AZURE_SEARCH_RESOURCE_ID:-}"
RG="${AZURE_RESOURCE_GROUP:-}"
SAFE=$(echo "$AZURE_ENV_NAME" | tr -cs '[:alnum:]' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-20)
SAFE="${SAFE%-}"

if [ -z "$SEARCH_RESOURCE_ID" ] || [ -z "$RG" ]; then
  if [ "$KB_ACCESS_MODE" = "purview" ]; then
    echo "Protected Search resource ID or resource group missing. Run azd up again." >&2
    exit 1
  fi
  echo "⚠  Required environment variables not set. Re-run 'azd provision' first."
  exit 0
fi

# Foundry creates a user-assigned managed identity for the agent using this naming pattern:
#   {cogservice}-{env_name}-{agent_name}-AgentIdentity
# Look it up directly from Azure AD by display name (faster and more reliable than log scraping).
echo "► Locating agent hosting identity..."

COG_ACCOUNT=$(az cognitiveservices account list   --resource-group "$RG"   --query "[0].name" -o tsv 2>/dev/null || true)

AGENT_IDENTITY_NAME="${COG_ACCOUNT}-${AZURE_ENV_NAME}-search-agent-AgentIdentity"
DEPLOYMENT_TENANT=$(az account show --query tenantId -o tsv)
GRAPH=(python3 "$(dirname "$0")/purview_graph.py" --subscription "$AZURE_SUBSCRIPTION_ID" --tenant "$DEPLOYMENT_TENANT")

if [ "$KB_ACCESS_MODE" = "purview" ]; then
  : "${PURVIEW_SEARCH_INDEX:?Run azd provision to create the protected index}"
  : "${COG_ACCOUNT:?Could not find the deployed Foundry account}"
  echo "  Looking for: ${AGENT_IDENTITY_NAME}"
  for attempt in 1 2 3 4 5 6; do
    HOSTING_OID=$("${GRAPH[@]}" --display-name "$AGENT_IDENTITY_NAME")
    if [ -n "$HOSTING_OID" ]; then break; fi
    if [ "$attempt" -lt 6 ]; then sleep 10; fi
  done
  if ! [[ "$HOSTING_OID" =~ ^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$ ]]; then
    echo "Expected exactly one hosted agent identity for protected Search." >&2
    exit 1
  fi
  az role assignment create --assignee-object-id "$HOSTING_OID" --assignee-principal-type ServicePrincipal \
    --role "Reader" --scope "$SEARCH_RESOURCE_ID" --output none
  az role assignment create --assignee-object-id "$HOSTING_OID" --assignee-principal-type ServicePrincipal \
    --role "Search Index Data Reader" --scope "$SEARCH_RESOURCE_ID" --output none
  echo "Foundry IQ read access granted on the dedicated protected Search service."
  if [ -n "${PURVIEW_ACCEPTANCE_CONFIG:-}" ]; then
    : "${PURVIEW_STORAGE_ACCOUNT:?Run azd provision to create the protected Blob account}"
    echo "► Running the required Purview ingestion and two-user acceptance gate..."
    python3 "$(dirname "$0")/test_purview_live.py" \
      --config "$PURVIEW_ACCEPTANCE_CONFIG" --account "$COG_ACCOUNT" --project "$AZURE_ENV_NAME" \
      --subscription "$AZURE_SUBSCRIPTION_ID" --search-service "${SEARCH_RESOURCE_ID##*/}" \
      --index "$PURVIEW_SEARCH_INDEX" --storage-account "$PURVIEW_STORAGE_ACCOUNT"
  else
    echo "Purview live acceptance not configured. Do not route traffic until it passes."
  fi
  exit 0
fi

HOSTING_OID=""
if [ -n "$COG_ACCOUNT" ]; then
  echo "  Looking for: ${AGENT_IDENTITY_NAME}"
  for attempt in 1 2 3 4 5 6; do
    HOSTING_OID=$("${GRAPH[@]}" --display-name "$AGENT_IDENTITY_NAME")
    if [ -n "$HOSTING_OID" ]; then break; fi
    if [ "$attempt" -lt 6 ]; then sleep 10; fi
  done
fi

# Fallback: invoke the agent and scan recent logs for the OID
if [ -z "$HOSTING_OID" ]; then
  echo "  Identity not found by name — invoking agent to capture OID from logs..."
  for attempt in 1 2 3; do
    echo "  Attempt ${attempt}/3..."
    azd ai agent invoke search-agent       '{"messages":[{"role":"user","content":"What documents are in the knowledge base?"}]}'       > /dev/null 2>&1 || true
    sleep 10
  done
  LOGS=$(azd ai agent monitor search-agent --tail 200 2>&1 || true)
  HOSTING_OID=$(echo "$LOGS" | grep "IDENTITY OID"     | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'     | head -1 || true)
fi

if [[ "$HOSTING_OID" =~ ^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$ ]]; then
  echo "  Hosting identity: ${HOSTING_OID}"
  echo "► Granting Search Index Data Reader..."
  az role assignment create --assignee-object-id "$HOSTING_OID" --assignee-principal-type ServicePrincipal \
    --role "Search Index Data Reader" --scope "$SEARCH_RESOURCE_ID" --output none
  COG_RESOURCE_ID=$(az cognitiveservices account show \
    --name "$COG_ACCOUNT" --resource-group "$RG" \
    --query id -o tsv 2>/dev/null || true)
  PROJECT_RESOURCE_ID="${COG_RESOURCE_ID}/projects/${AZURE_ENV_NAME}"
  if [ -n "$COG_RESOURCE_ID" ]; then
    # Memory Store runs as the caller, which at runtime is this hosting identity. Foundry User
    # (data actions such as accounts/AIServices/agents/read) is mandatory: without it every
    # memory read fails with "Identity ... does not have permissions". Cognitive Services OpenAI
    # User is what Microsoft's quickstart lists for embeddings; it was not required in testing
    # but is kept for parity with the documented prerequisites.
    for ROLE_SCOPE in "$COG_RESOURCE_ID" "$PROJECT_RESOURCE_ID"; do
      for ROLE in "Foundry User" "Cognitive Services OpenAI User"; do
        echo "► Granting ${ROLE} on ${ROLE_SCOPE##*/}..."
        if ROLE_OUT=$(az role assignment create \
            --assignee-object-id "$HOSTING_OID" --assignee-principal-type ServicePrincipal \
            --role "$ROLE" --scope "$ROLE_SCOPE" --output none 2>&1); then
          echo "  ✓ ${ROLE} granted"
        else
          case "$ROLE_OUT" in
            *RoleAssignmentExists*|*"already exists"*) echo "  ✓ ${ROLE} already assigned" ;;
            *)
              echo "  ERROR: could not grant ${ROLE} on ${ROLE_SCOPE##*/}: ${ROLE_OUT##*ERROR: }" >&2
              if [ "$ROLE" = "Foundry User" ] && [ "${MEMORY_OPTIONAL:-true}" = "false" ]; then exit 1; fi
              ;;
          esac
        fi
      done
    done
  fi
  DCR_RESOURCE_ID=$(az resource show --resource-type Microsoft.Insights/dataCollectionRules \
    --name "${SAFE}-dcr" --resource-group "$RG" --query id -o tsv)
  STORAGE_NAME=$(echo "$AZURE_ENV_NAME" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]' | cut -c1-20)docs
  MODEL_SCOPE="$(az storage account show -n "$STORAGE_NAME" -g "$RG" --query id -o tsv)/blobServices/default/containers/models"
  echo "► Granting telemetry and model artifact access..."
  CALLER_TYPE=$(az account show --query user.type -o tsv)
  case "$CALLER_TYPE" in
    user) CALLER_OID=$("${GRAPH[@]}"); CALLER_TYPE=User ;;
    servicePrincipal)
      CALLER_OID=$("${GRAPH[@]}" --app-id "$(az account show --query user.name -o tsv)")
      CALLER_TYPE=ServicePrincipal ;;
    *) echo "Unsupported deployment identity type: $CALLER_TYPE" >&2; exit 1 ;;
  esac
  if ! [[ "$CALLER_OID" =~ ^[[:xdigit:]]{8}(-[[:xdigit:]]{4}){3}-[[:xdigit:]]{12}$ ]]; then
    echo "Expected exactly one deployment principal for telemetry and model training." >&2
    exit 1
  fi
  az role assignment create --assignee-object-id "$HOSTING_OID" --assignee-principal-type ServicePrincipal \
    --role "Monitoring Metrics Publisher" --scope "$DCR_RESOURCE_ID" --output none
  az role assignment create --assignee-object-id "$CALLER_OID" --assignee-principal-type "$CALLER_TYPE" \
    --role "Monitoring Metrics Publisher" --scope "$DCR_RESOURCE_ID" --output none
  az role assignment create --assignee-object-id "$HOSTING_OID" --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Reader" --scope "$MODEL_SCOPE" --output none
  az role assignment create --assignee-object-id "$CALLER_OID" --assignee-principal-type "$CALLER_TYPE" \
    --role "Storage Blob Data Contributor" --scope "$MODEL_SCOPE" --output none
else
  echo ""
  echo "ERROR: Could not identify the hosting principal for Search, telemetry, and model access." >&2
  echo "   Grant the role manually after running the agent once:"
  echo ""
  echo "     OID=\$(azd ai agent monitor search-agent --tail 50 2>&1 | grep 'IDENTITY OID' | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
  echo "     az role assignment create --assignee \"\$OID\" --role 'Search Index Data Reader' --scope ${SEARCH_RESOURCE_ID}"
  echo ""
  exit 1
fi

# RBAC assignments propagate over several minutes. Measured on a live deployment: a new
# Foundry User grant took ~4 minutes before Memory Store calls from the hosting identity
# stopped failing. The hub and project identities were granted in postprovision, so this
# wait covers the hosting identity grant above.
RBAC_WAIT="${RBAC_PROPAGATION_WAIT_SECONDS:-300}"
echo ""
echo "  Waiting $((RBAC_WAIT / 60)) minutes for RBAC propagation (override with RBAC_PROPAGATION_WAIT_SECONDS)..."
sleep "$RBAC_WAIT"

# --- Generate eval cases from docs + run evaluations ---
echo ""
echo "► Generating eval cases from docs/..."
VENV_EVAL="${HOME}/.azd-eval-venv"
[ -f "${VENV_EVAL}/bin/python3" ] || python3 -m venv "${VENV_EVAL}"
"${VENV_EVAL}/bin/pip" install -q azure-identity azure-monitor-ingestion
if [ "${SKIP_HHEM:-false}" != "true" ]; then
  "${VENV_EVAL}/bin/pip" install -q 'transformers>=4.51,<5' 'torch>=2.6'
fi

# azd supplies environment values to this hook; the eval scripts use them directly.
EVAL_DIR="${POSTDEPLOY_EVAL_DIR:-$(dirname "$(dirname "$0")")/.azure/${AZURE_ENV_NAME:?}/evals}"
mkdir -p "$EVAL_DIR"
GENERATED_CASES="$EVAL_DIR/generated-cases.json"

FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT:-}" \
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}" \
AZURE_AI_MODEL_DEPLOYMENT_NAME="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5}" \
MEMORY_ENABLED="${MEMORY_ENABLED:-false}" \
MEMORY_STORE_NAME="${MEMORY_STORE_NAME:-}" \
MEMORY_SCOPE="${MEMORY_SCOPE:-}" \
MEMORY_UPDATE_DELAY_SECONDS="${MEMORY_UPDATE_DELAY_SECONDS:-5}" \
  "${VENV_EVAL}/bin/python3" "$(dirname "$0")/generate_eval_cases.py" --output "$GENERATED_CASES"
echo ""

echo "► Running automated evaluations..."
POSTDEPLOY_EVAL_ARGS="${POSTDEPLOY_EVAL_ARGS:-}"
EVAL_STATUS=0
FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT:-}" \
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}" \
AZURE_AI_MODEL_DEPLOYMENT_NAME="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5}" \
LOG_ANALYTICS_DCE="${LOG_ANALYTICS_DCE:-}" \
LOG_ANALYTICS_DCR_IMMUTABLE_ID="${LOG_ANALYTICS_DCR_IMMUTABLE_ID:-}" \
LOG_ANALYTICS_STREAM_NAME="${LOG_ANALYTICS_STREAM_NAME:-Custom-AgentTelemetry_CL}" \
MEMORY_ENABLED="${MEMORY_ENABLED:-false}" \
MEMORY_STORE_NAME="${MEMORY_STORE_NAME:-}" \
MEMORY_SCOPE="${MEMORY_SCOPE:-}" \
MEMORY_UPDATE_DELAY_SECONDS="${MEMORY_UPDATE_DELAY_SECONDS:-5}" \
EVAL_INTER_CASE_WAIT_SECONDS="${EVAL_INTER_CASE_WAIT_SECONDS:-60}" \
EVAL_AGENT_TIMEOUT_SECONDS="${EVAL_AGENT_TIMEOUT_SECONDS:-420}" \
SKIP_HHEM="${SKIP_HHEM:-false}" \
  "${VENV_EVAL}/bin/python3" "$(dirname "$0")/run_evals.py" \
    --cases "$GENERATED_CASES" --output "$EVAL_DIR/results.json" \
    ${POSTDEPLOY_EVAL_ARGS} || EVAL_STATUS=$?
echo ""

# --- Bootstrap IsolationForest with 5× eval baseline runs ---
BASELINE_RUNS="${BASELINE_RUNS:-5}"
echo "► Collecting baseline telemetry (${BASELINE_RUNS} eval runs → IsolationForest training)..."
BASELINE_COUNT=0
if [ "$BASELINE_RUNS" -gt 0 ]; then
  for BASELINE_RUN in $(seq 1 "$BASELINE_RUNS"); do
    echo "  Baseline run ${BASELINE_RUN}/${BASELINE_RUNS}..."
    EVAL_IS_BASELINE=true \
    FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT:-}" \
    AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}" \
    AZURE_AI_MODEL_DEPLOYMENT_NAME="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5}" \
    LOG_ANALYTICS_DCE="${LOG_ANALYTICS_DCE:-}" \
    LOG_ANALYTICS_DCR_IMMUTABLE_ID="${LOG_ANALYTICS_DCR_IMMUTABLE_ID:-}" \
    LOG_ANALYTICS_STREAM_NAME="${LOG_ANALYTICS_STREAM_NAME:-Custom-AgentTelemetry_CL}" \
    MEMORY_ENABLED="${MEMORY_ENABLED:-false}" \
    MEMORY_STORE_NAME="${MEMORY_STORE_NAME:-}" \
    MEMORY_SCOPE="${MEMORY_SCOPE:-}" \
    MEMORY_UPDATE_DELAY_SECONDS="${MEMORY_UPDATE_DELAY_SECONDS:-5}" \
    EVAL_INTER_CASE_WAIT_SECONDS="${EVAL_INTER_CASE_WAIT_SECONDS:-60}" \
    EVAL_AGENT_TIMEOUT_SECONDS="${EVAL_AGENT_TIMEOUT_SECONDS:-420}" \
    SKIP_HHEM=true \
      "${VENV_EVAL}/bin/python3" "$(dirname "$0")/run_evals.py" --no-judge --cases "$GENERATED_CASES" \
        --output "$EVAL_DIR/baseline-${BASELINE_RUN}.json" \
      && BASELINE_COUNT=$((BASELINE_COUNT + 1))
  done
fi
echo "  ${BASELINE_COUNT}/${BASELINE_RUNS} additional baseline eval runs passed"
echo ""

# Train IsolationForest on runtime retrieval telemetry
echo "► Training IsolationForest anomaly model..."
TRAIN_STATUS=0
if [ -n "${BLOB_ACCOUNT_URL:-}" ]; then
  "${VENV_EVAL}/bin/pip" install scikit-learn numpy azure-monitor-query azure-storage-blob azure-identity -q
  LOG_ANALYTICS_WORKSPACE_ID="${LOG_ANALYTICS_WORKSPACE_ID:-}" \
  BLOB_ACCOUNT_URL="${BLOB_ACCOUNT_URL}" \
  BLOB_MODEL_CONTAINER="models" \
  BLOB_MODEL_KEY="iforest.pkl" \
  MIN_BASELINE_ROWS=10 \
  BASELINE_INGEST_WAIT_SECONDS="${BASELINE_INGEST_WAIT_SECONDS:-120}" \
    "${VENV_EVAL}/bin/python3" "$(dirname "$(dirname "$0")")/observability/train_baseline.py" || TRAIN_STATUS=$?
else
  echo "ERROR: BLOB_ACCOUNT_URL is missing; cannot train and upload IsolationForest." >&2
  TRAIN_STATUS=1
fi
echo ""

if [ "$EVAL_STATUS" -ne 0 ] || [ "$TRAIN_STATUS" -ne 0 ]; then
  echo "Post-deploy validation failed: eval=$EVAL_STATUS training=$TRAIN_STATUS" >&2
  exit 1
fi

echo ""
echo "=== Post-Deploy Complete ==="
echo ""
echo "► Test your agent in the Azure AI Foundry portal (recommended — fresh session each time):"
echo "  https://ai.azure.com"
echo ""
echo "  Or invoke from the CLI:"
echo "  azd ai agent invoke search-agent '{\"messages\":[{\"role\":\"user\",\"content\":\"What documents are available?\"}]}'"
echo "  Note: the CLI reuses conversation history across invokes. If you see stale"
echo "  responses, test via the portal instead."
echo ""
