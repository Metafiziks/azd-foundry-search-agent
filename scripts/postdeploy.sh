#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "=== Post-Deploy: Granting search permissions to agent ==="
echo ""

SEARCH_RESOURCE_ID="${AZURE_SEARCH_RESOURCE_ID:-}"
RG="${AZURE_RESOURCE_GROUP:-}"
SAFE=$(echo "$AZURE_ENV_NAME" | tr -cs '[:alnum:]' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-20)
SAFE="${SAFE%-}"

if [ -z "$SEARCH_RESOURCE_ID" ] || [ -z "$RG" ]; then
  echo "⚠  Required environment variables not set. Re-run 'azd provision' first."
  exit 0
fi

# Foundry creates a user-assigned managed identity for the agent using this naming pattern:
#   {cogservice}-{env_name}-{agent_name}-AgentIdentity
# Look it up directly from Azure AD by display name (faster and more reliable than log scraping).
echo "► Locating agent hosting identity..."

COG_ACCOUNT=$(az cognitiveservices account list   --resource-group "$RG"   --query "[0].name" -o tsv 2>/dev/null || true)

HOSTING_OID=""
if [ -n "$COG_ACCOUNT" ]; then
  AGENT_IDENTITY_NAME="${COG_ACCOUNT}-${AZURE_ENV_NAME}-search-agent-AgentIdentity"
  echo "  Looking for: ${AGENT_IDENTITY_NAME}"
  HOSTING_OID=$(az ad sp list     --display-name "$AGENT_IDENTITY_NAME"     --query "[0].id" -o tsv 2>/dev/null || true)
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

if [ -n "$HOSTING_OID" ]; then
  echo "  Hosting identity: ${HOSTING_OID}"
  echo "► Granting Search Index Data Reader..."
  az role assignment create     --assignee "$HOSTING_OID"     --role "Search Index Data Reader"     --scope "$SEARCH_RESOURCE_ID"     --output none 2>/dev/null     && echo "  ✓ Permission granted"     || echo "  (role already assigned)"
  COG_RESOURCE_ID=$(az cognitiveservices account show \
    --name "$COG_ACCOUNT" --resource-group "$RG" \
    --query id -o tsv 2>/dev/null || true)
  PROJECT_RESOURCE_ID="${COG_RESOURCE_ID}/projects/${AZURE_ENV_NAME}"
  if [ -n "$COG_RESOURCE_ID" ]; then
    for ROLE_SCOPE in "$COG_RESOURCE_ID" "$PROJECT_RESOURCE_ID"; do
      echo "► Granting Foundry User for memory store read/write access on ${ROLE_SCOPE##*/}..."
      if az role assignment create \
        --assignee "$HOSTING_OID" \
        --role "Foundry User" \
        --scope "$ROLE_SCOPE" \
        --output none 2>/dev/null; then
        echo "  ✓ Foundry User role granted"
      elif az role assignment create \
          --assignee "$HOSTING_OID" \
          --role "Azure AI User" \
          --scope "$ROLE_SCOPE" \
          --output none 2>/dev/null; then
        echo "  ✓ Azure AI User role granted"
      else
        echo "  (Foundry/Azure AI User role already assigned or unavailable)"
      fi

      echo "► Granting Cognitive Services OpenAI User for memory store access on ${ROLE_SCOPE##*/}..."
      az role assignment create \
        --assignee "$HOSTING_OID" \
        --role "Cognitive Services OpenAI User" \
        --scope "$ROLE_SCOPE" \
        --output none 2>/dev/null \
        && echo "  ✓ OpenAI role granted" \
        || echo "  (OpenAI role already assigned)"
    done
  fi
  DCR_RESOURCE_ID=$(az monitor data-collection rule show \
    --name "${SAFE}-dcr" --resource-group "$RG" \
    --query id -o tsv 2>/dev/null || true)
  if [ -n "$DCR_RESOURCE_ID" ]; then
    echo "► Granting Monitoring Metrics Publisher for telemetry ingestion..."
    az role assignment create \
      --assignee "$HOSTING_OID" \
      --role "Monitoring Metrics Publisher" \
      --scope "$DCR_RESOURCE_ID" \
      --output none 2>/dev/null \
      && echo "  ✓ Telemetry ingestion role granted" \
      || echo "  (telemetry role already assigned)"
  fi
else
  echo ""
  echo "⚠  Could not auto-detect hosting identity OID."
  echo "   Grant the role manually after running the agent once:"
  echo ""
  echo "     OID=\$(azd ai agent monitor search-agent --tail 50 2>&1 | grep 'IDENTITY OID' | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1)"
  echo "     az role assignment create --assignee \"\$OID\" --role 'Search Index Data Reader' --scope ${SEARCH_RESOURCE_ID}"
  echo ""
fi

# RBAC assignments propagate globally over 2–10 minutes.
# The hub and project identities were already granted in postprovision (earlier in the pipeline),
# so this wait covers the agent identity grant above.
echo ""
echo "  Waiting 5 minutes for RBAC propagation..."
sleep 300

# --- Generate eval cases from docs + run evaluations ---
echo ""
echo "► Generating eval cases from docs/..."
VENV_EVAL="${HOME}/.azd-eval-venv"
[ -f "${VENV_EVAL}/bin/python3" ] || python3 -m venv "${VENV_EVAL}"
"${VENV_EVAL}/bin/pip" install azure-identity -q

eval "$(azd env get-values 2>/dev/null)" || true

FOUNDRY_PROJECT_ENDPOINT="${FOUNDRY_PROJECT_ENDPOINT:-}" \
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}" \
AZURE_AI_MODEL_DEPLOYMENT_NAME="${AZURE_AI_MODEL_DEPLOYMENT_NAME:-gpt-5}" \
MEMORY_ENABLED="${MEMORY_ENABLED:-false}" \
MEMORY_STORE_NAME="${MEMORY_STORE_NAME:-}" \
MEMORY_SCOPE="${MEMORY_SCOPE:-}" \
MEMORY_UPDATE_DELAY_SECONDS="${MEMORY_UPDATE_DELAY_SECONDS:-5}" \
  "${VENV_EVAL}/bin/python3" "$(dirname "$0")/generate_eval_cases.py" || true
echo ""

echo "► Running automated evaluations..."
POSTDEPLOY_EVAL_ARGS="${POSTDEPLOY_EVAL_ARGS:-}"
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
    --output "$(dirname "$(dirname "$0")")/eval_results.json" ${POSTDEPLOY_EVAL_ARGS} || true
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
      "${VENV_EVAL}/bin/python3" "$(dirname "$0")/run_evals.py" --no-judge \
        --output "/tmp/baseline_run_${BASELINE_RUN}.json" 2>/dev/null || true
    BASELINE_COUNT=$((BASELINE_COUNT + 1))
  done
fi
echo "  ✓ ${BASELINE_COUNT} baseline runs completed — telemetry ingested to Log Analytics"
echo ""

# Wait a moment for LA ingestion latency before training
sleep 30

# Train IsolationForest on the baseline telemetry
echo "► Training IsolationForest anomaly model..."
BLOB_ACCOUNT_URL_VAL=$(az storage account show \
  --name "${STORAGE_NAME}" --resource-group "${AZURE_RESOURCE_GROUP:-}" \
  --query primaryEndpoints.blob -o tsv 2>/dev/null | sed 's|/$||' || true)

if [ -n "${BLOB_ACCOUNT_URL_VAL}" ]; then
  "${VENV_EVAL}/bin/pip" install scikit-learn numpy azure-monitor-query azure-storage-blob azure-identity -q
  LOG_ANALYTICS_WORKSPACE_ID="${LOG_ANALYTICS_WORKSPACE_ID:-}" \
  BLOB_ACCOUNT_URL="${BLOB_ACCOUNT_URL_VAL}" \
  BLOB_MODEL_CONTAINER="models" \
  BLOB_MODEL_KEY="iforest.pkl" \
  MIN_BASELINE_ROWS=10 \
    "${VENV_EVAL}/bin/python3" "$(dirname "$(dirname "$0")")/observability/train_baseline.py" || \
    echo "  ⚠ IForest training deferred — will retry on first weekly retrain"
else
  echo "  ⚠ Could not resolve storage account URL — IForest training skipped"
fi
echo ""

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
