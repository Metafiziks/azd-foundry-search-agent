#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "=== Post-Provision: Setting up AI Search, Storage, and Documents ==="
echo ""

# --- Variables ---
RG="${AZURE_RESOURCE_GROUP}"
LOCATION="${AZURE_LOCATION:-eastus2}"
ACCOUNT="${AZURE_AI_ACCOUNT_NAME}"

SAFE=$(echo "$AZURE_ENV_NAME" | tr -cs '[:alnum:]' '-' | tr '[:upper:]' '[:lower:]' | cut -c1-20)
SAFE="${SAFE%-}"
SEARCH_NAME="${SAFE}-search"
STORAGE_NAME=$(echo "$AZURE_ENV_NAME" | tr -cd '[:alnum:]' | tr '[:upper:]' '[:lower:]' | cut -c1-20)docs
INDEX_NAME="${SAFE}-index"

# --- Model deployment ---
echo "► Checking model deployment..."
MODEL_COUNT=$(az cognitiveservices account deployment list \
  --name "$ACCOUNT" --resource-group "$RG" \
  --query "length(@)" -o tsv 2>/dev/null || echo "0")

if [ "$MODEL_COUNT" = "0" ]; then
  echo "  Deploying gpt-5 model (this takes 2-3 minutes)..."
  az cognitiveservices account deployment create \
    --name "$ACCOUNT" \
    --resource-group "$RG" \
    --deployment-name "gpt-5" \
    --model-name "gpt-5" \
    --model-version "2025-08-07" \
    --model-format "OpenAI" \
    --sku-name "GlobalStandard" \
    --sku-capacity 10 \
    --output none
  echo "  ✓ Model deployed: gpt-5"
else
  echo "  ✓ Model already deployed"
fi

azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "gpt-5"
AZURE_AI_MODEL_DEPLOYMENT_NAME="gpt-5"

echo "► Checking embedding deployment for Foundry Memory..."
EMBEDDING_DEPLOYMENT="${AZURE_AI_EMBEDDING_MODEL_DEPLOYMENT_NAME:-text-embedding-3-small}"
if az cognitiveservices account deployment show \
  --name "$ACCOUNT" \
  --resource-group "$RG" \
  --deployment-name "$EMBEDDING_DEPLOYMENT" \
  --query name -o tsv >/dev/null 2>&1; then
  echo "  ✓ Embedding deployment already exists: ${EMBEDDING_DEPLOYMENT}"
else
  echo "  Deploying embedding model: ${EMBEDDING_DEPLOYMENT}..."
  if az cognitiveservices account deployment create \
    --name "$ACCOUNT" \
    --resource-group "$RG" \
    --deployment-name "$EMBEDDING_DEPLOYMENT" \
    --model-name "text-embedding-3-small" \
    --model-version "1" \
    --model-format "OpenAI" \
    --sku-name "Standard" \
    --sku-capacity 10 \
    --output none; then
    echo "  ✓ Embedding model deployed: ${EMBEDDING_DEPLOYMENT}"
  else
    echo "  ⚠ Could not deploy embedding model; managed memory will be disabled unless MEMORY_OPTIONAL=false."
    if [ "${MEMORY_OPTIONAL:-true}" = "false" ]; then
      exit 1
    fi
    azd env set MEMORY_ENABLED "false"
  fi
fi
azd env set AZURE_AI_EMBEDDING_MODEL_DEPLOYMENT_NAME "$EMBEDDING_DEPLOYMENT"

# --- AI Search service ---
echo ""
echo "► Creating AI Search service: ${SEARCH_NAME}..."
EXISTING_SEARCH=$(az search service show --name "$SEARCH_NAME" --resource-group "$RG" --query name -o tsv 2>/dev/null || true)
SEARCH_ENDPOINT="https://${SEARCH_NAME}.search.windows.net"

if [ "$EXISTING_SEARCH" = "$SEARCH_NAME" ]; then
  echo "  (already exists)"
else
  SEARCH_CREATED=false
  for REGION in "$LOCATION" eastus westus2 westeurope northeurope centralus uksouth; do
    echo "  Trying region: ${REGION}..."
    REGION_DELAY=10
    for ATTEMPT in $(seq 1 12); do
      if az search service create \
        --name "$SEARCH_NAME" \
        --resource-group "$RG" \
        --location "$REGION" \
        --sku Standard \
        --auth-options aadOrApiKey \
        --aad-auth-failure-mode http401WithBearerChallenge \
        --output none 2>/tmp/search_create_err; then
        SEARCH_CREATED=true
        echo "  ✓ Search service created in ${REGION}"
        break 2
      else
        ERR=$(cat /tmp/search_create_err)
        if echo "$ERR" | grep -q "InsufficientResourcesAvailable"; then
          echo "  Region ${REGION} has no capacity, trying next..."
          break
        elif echo "$ERR" | grep -q "ServiceDeleting"; then
          echo "  Previous deletion still in progress (attempt ${ATTEMPT}/12), waiting ${REGION_DELAY}s..."
          sleep "$REGION_DELAY"
          REGION_DELAY=$((REGION_DELAY * 2 > 120 ? 120 : REGION_DELAY * 2))
        else
          echo "  ERROR: $ERR" >&2
          exit 1
        fi
      fi
    done
  done
  if [ "$SEARCH_CREATED" = "false" ]; then
    echo "  ERROR: Could not create AI Search service in any region." >&2
    exit 1
  fi

  echo "  Waiting for Search endpoint to become reachable..."
  for i in $(seq 1 30); do
    CODE=$(python3 -c "
import urllib.request, urllib.error
try:
    req = urllib.request.Request('${SEARCH_ENDPOINT}/?api-version=2024-07-01')
    req.add_header('api-key', 'placeholder')
    urllib.request.urlopen(req, timeout=5)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception:
    print(0)
" 2>/dev/null || echo "0")
    if [ "$CODE" = "401" ] || [ "$CODE" = "403" ] || [ "$CODE" = "200" ]; then
      echo "  ✓ Search endpoint is ready"
      break
    fi
    sleep 5
  done
fi
echo "  ✓ Search service ready"

SEARCH_RESOURCE_ID=$(az search service show --name "$SEARCH_NAME" --resource-group "$RG" --query id -o tsv)
SEARCH_KEY=$(az search admin-key show --service-name "$SEARCH_NAME" --resource-group "$RG" --query primaryKey -o tsv)

# --- Storage account ---
echo ""
echo "► Creating Storage account: ${STORAGE_NAME}..."
EXISTING_STORAGE=$(az storage account show --name "$STORAGE_NAME" --resource-group "$RG" --query name -o tsv 2>/dev/null || true)
if [ "$EXISTING_STORAGE" = "$STORAGE_NAME" ]; then
  echo "  (already exists)"
else
  az storage account create \
    --name "$STORAGE_NAME" \
    --resource-group "$RG" \
    --location "$LOCATION" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --allow-blob-public-access true \
    --output none
fi

STORAGE_RESOURCE_ID=$(az storage account show --name "$STORAGE_NAME" --resource-group "$RG" --query id -o tsv)
STORAGE_KEY=$(az storage account keys list --account-name "$STORAGE_NAME" --resource-group "$RG" --query "[0].value" -o tsv)

az storage container create \
  --name docs \
  --account-name "$STORAGE_NAME" \
  --account-key "$STORAGE_KEY" \
  --public-access blob \
  --output none 2>/dev/null || true
echo "  ✓ Storage ready"

# --- Upload documents ---
echo ""
echo "► Uploading documents from docs/ to blob storage..."
az storage blob upload-batch \
  --account-name "$STORAGE_NAME" \
  --account-key "$STORAGE_KEY" \
  --destination docs \
  --source ./docs \
  --overwrite true \
  --output none
echo "  ✓ Documents uploaded"

# --- Grant Search identity Storage access ---
echo ""
echo "► Configuring Search identity permissions..."
SEARCH_PRINCIPAL=$(az search service show \
  --name "$SEARCH_NAME" --resource-group "$RG" \
  --query "identity.principalId" -o tsv 2>/dev/null || true)

if [ -n "$SEARCH_PRINCIPAL" ] && [ "$SEARCH_PRINCIPAL" != "null" ]; then
  az role assignment create \
    --assignee "$SEARCH_PRINCIPAL" \
    --role "Storage Blob Data Reader" \
    --scope "$STORAGE_RESOURCE_ID" \
    --output none 2>/dev/null || true
  echo "  ✓ Search identity granted Storage Blob Data Reader"
fi
# Grant Search access to all Foundry-related identities.
# Foundry uses multiple identities (hub, project, agent) depending on context.
echo ""
echo "► Granting Search access to Foundry identities..."

# Hub (Cognitive Services account) identity
AI_HUB_PRINCIPAL=$(az cognitiveservices account show \
  --name "$ACCOUNT" --resource-group "$RG" \
  --query "identity.principalId" -o tsv 2>/dev/null || true)

# Project identity
AI_PROJECT_PRINCIPAL=$(az rest --method GET \
  --url "https://management.azure.com/subscriptions/${AZURE_SUBSCRIPTION_ID}/resourceGroups/${RG}/providers/Microsoft.CognitiveServices/accounts/${ACCOUNT}/projects/${AZURE_ENV_NAME}?api-version=2025-04-01-preview" \
  --query "identity.principalId" -o tsv 2>/dev/null || true)

for PRINCIPAL in "$AI_HUB_PRINCIPAL" "$AI_PROJECT_PRINCIPAL"; do
  if [ -n "$PRINCIPAL" ] && [ "$PRINCIPAL" != "null" ]; then
    az role assignment create \
      --assignee "$PRINCIPAL" \
      --role "Search Index Data Reader" \
      --scope "$SEARCH_RESOURCE_ID" \
      --output none 2>/dev/null || true
    echo "  ✓ Identity $PRINCIPAL granted Search Index Data Reader"
  fi
done

COG_RESOURCE_ID=$(az cognitiveservices account show \
  --name "$ACCOUNT" --resource-group "$RG" \
  --query id -o tsv)
PROJECT_RESOURCE_ID="${COG_RESOURCE_ID}/projects/${AZURE_ENV_NAME}"
for PRINCIPAL in "$AI_HUB_PRINCIPAL" "$AI_PROJECT_PRINCIPAL"; do
  if [ -n "$PRINCIPAL" ] && [ "$PRINCIPAL" != "null" ]; then
    for ROLE_SCOPE in "$COG_RESOURCE_ID" "$PROJECT_RESOURCE_ID"; do
      az role assignment create \
        --assignee "$PRINCIPAL" \
        --role "Foundry User" \
        --scope "$ROLE_SCOPE" \
        --output none 2>/dev/null || true
      az role assignment create \
        --assignee "$PRINCIPAL" \
        --role "Cognitive Services OpenAI User" \
        --scope "$ROLE_SCOPE" \
        --output none 2>/dev/null || true
    done
    echo "  ✓ Identity $PRINCIPAL granted Foundry/OpenAI roles for memory"
  fi
done

# --- Search REST via Python (avoids macOS curl/TLS issues) ---
echo ""
echo "► Creating search index, data source, and indexer..."
python3 "$(dirname "$0")/search_setup.py" \
  "${SEARCH_ENDPOINT}" \
  "${SEARCH_KEY}" \
  "${INDEX_NAME}" \
  "${STORAGE_NAME}" \
  "${STORAGE_KEY}"

# --- Save env vars ---
azd env set AZURE_SEARCH_ENDPOINT "$SEARCH_ENDPOINT"
azd env set AZURE_SEARCH_INDEX "$INDEX_NAME"
azd env set AZURE_SEARCH_RESOURCE_ID "$SEARCH_RESOURCE_ID"

echo ""
# --- Register Foundry project with data plane and wait for readiness ---
# The ARM resource is created by provision, but the Foundry data plane uses lazy
# initialization. We explicitly POST to register the project, then poll until ready.
echo ""
echo "► Registering Foundry project with data plane..."
python3 - "${ACCOUNT}" "${AZURE_ENV_NAME}" "${AZURE_SUBSCRIPTION_ID}" "${AZURE_RESOURCE_GROUP}" << 'WAIT_EOF'
import subprocess, sys, time, urllib.request, urllib.error, json as _json

def get_token():
    r = subprocess.run(
        ["az", "account", "get-access-token",
         "--resource", "https://ai.azure.com",
         "--query", "accessToken", "-o", "tsv"],
        capture_output=True, text=True
    )
    return r.stdout.strip()

account, project, sub_id, rg = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
base = f"https://{account}.services.ai.azure.com"
projects_url = f"{base}/api/projects?api-version=2025-05-01"
agents_url = f"{base}/api/projects/{project}/agents?api-version=v1"
arm_resource_id = (
    f"/subscriptions/{sub_id}/resourceGroups/{rg}"
    f"/providers/Microsoft.CognitiveServices/accounts/{account}/projects/{project}"
)

# Step 1: POST to register the project in the data plane
token = get_token()
body = _json.dumps({
    "displayName": project,
    "description": f"{project} Project",
    "armResourceId": arm_resource_id
}).encode()
try:
    req = urllib.request.Request(
        projects_url, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"  \u2713 Project registered via POST (HTTP {resp.status})", flush=True)
except urllib.error.HTTPError as e:
    if e.code == 409:
        print(f"  \u2713 Project already registered (HTTP 409 Conflict)", flush=True)
    else:
        body_text = e.read().decode()[:200]
        print(f"  POST HTTP {e.code}: {body_text} — will poll for readiness", flush=True)
except Exception as e:
    print(f"  POST error: {type(e).__name__} — will poll for readiness", flush=True)

# Step 2: Poll the write path of the agents endpoint
# The Foundry data plane has separate read/write tiers. GET /agents returns 200
# before the write path is ready. We probe with a POST (same path azd deploy uses):
# - "Project not found" 404  => write path not ready, keep waiting
# - any other response (400/409/200) => write path ready, proceed
# We do NOT manually create capability hosts — doing so resets the initialization
# timer and can break the remote_build image registry linkage.
probe_url = f"{base}/api/projects/{project}/agents/readiness-probe?api-version=v1"
probe_body = _json.dumps({"name": "readiness-probe", "model": "gpt-4o", "instructions": "probe"}).encode()
for i in range(360):  # up to 30 minutes at 5s intervals
    try:
        token = get_token()
        req = urllib.request.Request(
            probe_url, data=probe_body, method="POST",
            headers={"Authorization": f"******", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            # 200/201 means write path is ready (agent created, clean up)
            print(f"  ✓ Foundry write path ready (HTTP {resp.status})", flush=True)
            # Try to delete the probe agent
            try:
                del_req = urllib.request.Request(
                    probe_url, method="DELETE",
                    headers={"Authorization": f"******"}
                )
                urllib.request.urlopen(del_req, timeout=10)
            except Exception:
                pass
            sys.exit(0)
    except urllib.error.HTTPError as e:
        body_bytes = e.read()
        try:
            err_data = _json.loads(body_bytes)
            err_msg = err_data.get("error", {}).get("message", "")
        except Exception:
            err_msg = body_bytes.decode()[:100]
        if "Project not found" in err_msg:
            print(f"  Waiting for Foundry write path... ({i+1}/360, ~{(i+1)*5}s elapsed, write-404)", flush=True)
        else:
            # Any other error means the project IS reachable for writes
            print(f"  ✓ Foundry write path ready (HTTP {e.code}: {err_msg[:60]})", flush=True)
            sys.exit(0)
    except Exception as e:
        print(f"  Waiting for Foundry write path... ({i+1}/360, ~{(i+1)*5}s elapsed, {type(e).__name__})", flush=True)
    time.sleep(5)

print("  ⚠  Foundry write path not ready after 30 min.")
print("     Run 'azd deploy' again in 30-60 min. This is an Azure platform delay, not a template bug.")
print("     Tip: visiting ai.azure.com and clicking your project may accelerate initialization.")
WAIT_EOF
echo ""

# --- Azure AI Foundry Memory Store (preview) ---
echo "► Setting up Azure AI Foundry Memory Store..."
MEMORY_OPTIONAL="${MEMORY_OPTIONAL:-true}"
if [ -z "${MEMORY_STORE_NAME:-}" ]; then
  MEMORY_STORE_NAME="${SAFE}-memory"
fi
if [ -z "${MEMORY_SCOPE:-}" ]; then
  MEMORY_SCOPE='{{$userId}}'
fi
MEMORY_UPDATE_DELAY_SECONDS="${MEMORY_UPDATE_DELAY_SECONDS:-5}"

azd env set MEMORY_OPTIONAL "$MEMORY_OPTIONAL"
azd env set MEMORY_STORE_NAME "$MEMORY_STORE_NAME"
azd env set MEMORY_SCOPE "$MEMORY_SCOPE"
azd env set MEMORY_UPDATE_DELAY_SECONDS "$MEMORY_UPDATE_DELAY_SECONDS"

if [ "${MEMORY_ENABLED:-true}" = "false" ]; then
  echo "  (memory disabled by MEMORY_ENABLED=false)"
else
  VENV_MEMORY="${HOME}/.azd-memory-venv"
  [ -f "${VENV_MEMORY}/bin/python3" ] || python3 -m venv "${VENV_MEMORY}"
  "${VENV_MEMORY}/bin/pip" install "azure-ai-projects>=2.3.0" azure-identity aiohttp -q

  if MEMORY_STORE_NAME="$MEMORY_STORE_NAME" \
     AZURE_AI_MODEL_DEPLOYMENT_NAME="$AZURE_AI_MODEL_DEPLOYMENT_NAME" \
     AZURE_AI_EMBEDDING_MODEL_DEPLOYMENT_NAME="$EMBEDDING_DEPLOYMENT" \
     "${VENV_MEMORY}/bin/python3" "$(dirname "$0")/provision_memory_store.py"; then
    azd env set MEMORY_ENABLED "true"
    echo "  ✓ Memory store ready: ${MEMORY_STORE_NAME}"
  else
    echo "  ⚠ Memory store provisioning failed."
    if [ "$MEMORY_OPTIONAL" = "false" ]; then
      exit 1
    fi
    azd env set MEMORY_ENABLED "false"
    echo "  Continuing without memory because MEMORY_OPTIONAL=true."
  fi
fi

# --- Log Analytics Workspace + DCE + DCR for ML observability telemetry ---
echo "► Setting up Log Analytics Workspace for ML observability..."
LA_WORKSPACE="${SAFE}-law"
DCE_NAME="${SAFE}-dce"
DCR_NAME="${SAFE}-dcr"

# Create workspace
EXISTING_LAW=$(az monitor log-analytics workspace show \
  --workspace-name "$LA_WORKSPACE" --resource-group "$RG" \
  --query name -o tsv 2>/dev/null || true)
if [ "$EXISTING_LAW" != "$LA_WORKSPACE" ]; then
  az monitor log-analytics workspace create \
    --workspace-name "$LA_WORKSPACE" \
    --resource-group "$RG" \
    --location "$LOCATION" \
    --output none
  echo "  ✓ Log Analytics workspace created: ${LA_WORKSPACE}"
else
  echo "  (workspace already exists)"
fi

WORKSPACE_ID=$(az monitor log-analytics workspace show \
  --workspace-name "$LA_WORKSPACE" --resource-group "$RG" \
  --query customerId -o tsv)
WORKSPACE_RESOURCE_ID=$(az monitor log-analytics workspace show \
  --workspace-name "$LA_WORKSPACE" --resource-group "$RG" \
  --query id -o tsv)

# Create Data Collection Endpoint
EXISTING_DCE=$(az monitor data-collection endpoint show \
  --name "$DCE_NAME" --resource-group "$RG" \
  --query name -o tsv 2>/dev/null || true)
if [ "$EXISTING_DCE" != "$DCE_NAME" ]; then
  az monitor data-collection endpoint create \
    --name "$DCE_NAME" \
    --resource-group "$RG" \
    --location "$LOCATION" \
    --public-network-access Enabled \
    --output none
  echo "  ✓ Data Collection Endpoint created: ${DCE_NAME}"
fi
DCE_ENDPOINT=$(az monitor data-collection endpoint show \
  --name "$DCE_NAME" --resource-group "$RG" \
  --query logsIngestion.endpoint -o tsv)

# Create custom table AgentTelemetry_CL in the workspace
az monitor log-analytics workspace table create \
  --workspace-name "$LA_WORKSPACE" \
  --resource-group "$RG" \
  --name "AgentTelemetry_CL" \
  --columns \
    TimeGenerated=datetime \
    RequestId_s=string \
    Source_s=string \
    IsBaseline_b=boolean \
    RetrievalScoreMean_d=real \
    RetrievalScoreStd_d=real \
    RetrievalScoreEntropy_d=real \
    ChunkCount_d=real \
    RerankerScoreMean_d=real \
    SearchLatencyMs_d=real \
    AnswerLength_d=real \
    CitationCount_d=real \
    HhemScore_d=real \
    AnomalyScore_d=real \
    IsAnomaly_b=boolean \
    LatencyMs_d=real \
    Faithfulness_d=real \
    AnswerRelevance_d=real \
    KeywordRecall_d=real \
    CitationRecall_d=real \
    MemoryEnabled_b=boolean \
    MemoryReadCount_d=real \
    MemoryWriteCount_d=real \
    MemoryLatencyMs_d=real \
    MemoryStatus_s=string \
  --output none 2>/dev/null || true
echo "  ✓ Custom log table AgentTelemetry_CL ready"

# Create Data Collection Rule
DCE_RESOURCE_ID=$(az monitor data-collection endpoint show \
  --name "$DCE_NAME" --resource-group "$RG" \
  --query id -o tsv)

# Build DCR JSON
cat > /tmp/dcr_def.json << DCR_EOF
{
  "location": "${LOCATION}",
  "properties": {
    "dataCollectionEndpointId": "${DCE_RESOURCE_ID}",
    "streamDeclarations": {
      "Custom-AgentTelemetry_CL": {
        "columns": [
          {"name": "TimeGenerated", "type": "datetime"},
          {"name": "RequestId_s", "type": "string"},
          {"name": "Source_s", "type": "string"},
          {"name": "IsBaseline_b", "type": "boolean"},
          {"name": "RetrievalScoreMean_d", "type": "real"},
          {"name": "RetrievalScoreStd_d", "type": "real"},
          {"name": "RetrievalScoreEntropy_d", "type": "real"},
          {"name": "ChunkCount_d", "type": "real"},
          {"name": "RerankerScoreMean_d", "type": "real"},
          {"name": "SearchLatencyMs_d", "type": "real"},
          {"name": "AnswerLength_d", "type": "real"},
          {"name": "CitationCount_d", "type": "real"},
          {"name": "HhemScore_d", "type": "real"},
          {"name": "AnomalyScore_d", "type": "real"},
          {"name": "IsAnomaly_b", "type": "boolean"},
          {"name": "LatencyMs_d", "type": "real"},
          {"name": "Faithfulness_d", "type": "real"},
          {"name": "AnswerRelevance_d", "type": "real"},
          {"name": "KeywordRecall_d", "type": "real"},
          {"name": "CitationRecall_d", "type": "real"},
          {"name": "MemoryEnabled_b", "type": "boolean"},
          {"name": "MemoryReadCount_d", "type": "real"},
          {"name": "MemoryWriteCount_d", "type": "real"},
          {"name": "MemoryLatencyMs_d", "type": "real"},
          {"name": "MemoryStatus_s", "type": "string"}
        ]
      }
    },
    "destinations": {
      "logAnalytics": [{
        "name": "law-dest",
        "workspaceResourceId": "${WORKSPACE_RESOURCE_ID}"
      }]
    },
    "dataFlows": [{
      "streams": ["Custom-AgentTelemetry_CL"],
      "destinations": ["law-dest"],
      "outputStream": "Custom-AgentTelemetry_CL",
      "transformKql": "source"
    }]
  }
}
DCR_EOF

EXISTING_DCR=$(az monitor data-collection rule show \
  --name "$DCR_NAME" --resource-group "$RG" \
  --query name -o tsv 2>/dev/null || true)
if [ "$EXISTING_DCR" != "$DCR_NAME" ]; then
  az monitor data-collection rule create \
    --name "$DCR_NAME" \
    --resource-group "$RG" \
    --rule-file /tmp/dcr_def.json \
    --output none
  echo "  ✓ Data Collection Rule created: ${DCR_NAME}"
fi
DCR_IMMUTABLE_ID=$(az monitor data-collection rule show \
  --name "$DCR_NAME" --resource-group "$RG" \
  --query immutableId -o tsv)

# Persist env vars for agent and eval scripts
azd env set LOG_ANALYTICS_WORKSPACE_ID    "$WORKSPACE_ID"
azd env set LOG_ANALYTICS_DCE             "$DCE_ENDPOINT"
azd env set LOG_ANALYTICS_DCR_IMMUTABLE_ID "$DCR_IMMUTABLE_ID"
azd env set LOG_ANALYTICS_STREAM_NAME    "Custom-AgentTelemetry_CL"
echo "  ✓ Log Analytics env vars saved"

# Create models container in blob for IsolationForest
echo ""
echo "► Creating blob container for ML models..."
az storage container create \
  --name models \
  --account-name "$STORAGE_NAME" \
  --account-key "$STORAGE_KEY" \
  --public-access off \
  --output none 2>/dev/null || true
echo "  ✓ models container ready"

echo ""
echo "=== Post-Provision Complete ==="
echo "  Search              : ${SEARCH_ENDPOINT}"
echo "  Index               : ${INDEX_NAME}"
echo "  Log Analytics WS    : ${WORKSPACE_ID}"
echo "  DCE Endpoint        : ${DCE_ENDPOINT}"
echo ""
