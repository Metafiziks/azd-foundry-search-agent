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
      break
    else
      ERR=$(cat /tmp/search_create_err)
      if echo "$ERR" | grep -q "InsufficientResourcesAvailable"; then
        echo "  Region ${REGION} has no capacity, trying next..."
      else
        echo "  ERROR: $ERR" >&2
        exit 1
      fi
    fi
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
    --allow-blob-public-access false \
    --output none
fi

STORAGE_RESOURCE_ID=$(az storage account show --name "$STORAGE_NAME" --resource-group "$RG" --query id -o tsv)
STORAGE_KEY=$(az storage account keys list --account-name "$STORAGE_NAME" --resource-group "$RG" --query "[0].value" -o tsv)

az storage container create \
  --name docs \
  --account-name "$STORAGE_NAME" \
  --account-key "$STORAGE_KEY" \
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

# Step 2: Create capabilityHost/Agents to register the project with the agents data plane
# The account-level host must be created first (with enablePublicHostingEnvironment=true),
# then the project-level host. This is required for the Foundry agents data plane to
# recognize the project. Uses 2025-10-01-preview which supports enablePublicHostingEnvironment.
arm_token_r = subprocess.run(
    ["az", "account", "get-access-token",
     "--resource", "https://management.azure.com",
     "--query", "accessToken", "-o", "tsv"],
    capture_output=True, text=True
)
arm_token = arm_token_r.stdout.strip()

def ensure_cap_host(url, body_dict, label):
    """Create capability host only if it does not already exist (idempotent).
    Re-creating/updating a capability host resets the agents runtime initialization,
    so we skip the PUT entirely if it already exists and is Succeeded."""
    try:
        get_req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {arm_token}"}
        )
        with urllib.request.urlopen(get_req, timeout=30) as resp:
            existing = _json.loads(resp.read())
            state = existing.get("properties", {}).get("provisioningState", "")
            if state in ("Succeeded", "Creating", "Updating"):
                print(f"  \u2713 {label}: already {state}, skipping", flush=True)
                return
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print(f"  \u26a0  {label} GET error {e.code}, will attempt create", flush=True)
    except Exception:
        pass

    try:
        body = _json.dumps(body_dict).encode()
        req = urllib.request.Request(
            url, data=body, method="PUT",
            headers={"Authorization": f"Bearer {arm_token}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_data = _json.loads(resp.read())
            state = resp_data.get("properties", {}).get("provisioningState", "unknown")
            print(f"  \u2713 {label}: {state}", flush=True)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()[:300]
        print(f"  \u26a0  {label} HTTP {e.code}: {body_text}", flush=True)
    except Exception as e:
        print(f"  \u26a0  {label}: {type(e).__name__}: {e}", flush=True)

cap_api = "2025-10-01-preview"
arm_base = f"https://management.azure.com/subscriptions/{sub_id}/resourceGroups/{rg}/providers/Microsoft.CognitiveServices/accounts/{account}"

# Account-level capability host (required first)
ensure_cap_host(
    f"{arm_base}/capabilityHosts/agents?api-version={cap_api}",
    {"properties": {"capabilityHostKind": "Agents", "enablePublicHostingEnvironment": True}},
    "Account capability host"
)
# Project-level capability host
ensure_cap_host(
    f"{arm_base}/projects/{project}/capabilityHosts/agents?api-version={cap_api}",
    {"properties": {}},
    "Project capability host"
)

# Step 3: Poll agents endpoint until 200
# Step 3: Poll agents endpoint until 200
for i in range(180):  # up to 30 minutes
    try:
        token = get_token()
        req = urllib.request.Request(
            agents_url, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"  \u2713 Foundry project ready (HTTP {resp.status})", flush=True)
            sys.exit(0)
    except urllib.error.HTTPError as e:
        label = f"HTTP {e.code}"
    except Exception as e:
        label = type(e).__name__
    print(f"  Waiting for Foundry data plane sync... ({i+1}/180, ~{(i+1)*10}s elapsed, {label})", flush=True)
    time.sleep(10)

print("  \u26a0  Foundry data plane not synced after 30 min.")
print("     Run 'azd deploy' again in 30-60 min. This is an Azure platform delay, not a template bug.")
print("     Tip: visiting ai.azure.com and clicking your project may accelerate initialization.")
WAIT_EOF
echo ""
echo "=== Post-Provision Complete ==="
echo "  Search : ${SEARCH_ENDPOINT}"
echo "  Index  : ${INDEX_NAME}"
echo ""
