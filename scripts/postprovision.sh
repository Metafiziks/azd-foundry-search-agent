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

# --- Search REST via Python (avoids macOS curl/TLS issues) ---
echo ""
echo "► Creating search index, data source, and indexer..."
python3 /tmp/search_setup.py \
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
echo "=== Post-Provision Complete ==="
echo "  Search : ${SEARCH_ENDPOINT}"
echo "  Index  : ${INDEX_NAME}"
echo ""
