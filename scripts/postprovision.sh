#!/usr/bin/env bash
set -euo pipefail

echo ""
echo "=== Post-Provision: Setting up AI Search, Storage, and Documents ==="
echo ""

# --- Variables ---
RG="${AZURE_RESOURCE_GROUP}"
LOCATION="${AZURE_LOCATION:-eastus2}"
ACCOUNT="${AZURE_AI_ACCOUNT_NAME}"

# Derive globally unique resource names from the azd env name
SAFE=$(echo "$AZURE_ENV_NAME" | tr -cs "[:alnum:]" "-" | tr "[:upper:]" "[:lower:]" | cut -c1-20)
SAFE="${SAFE%-}"  # strip trailing dash
SEARCH_NAME="${SAFE}-search"
STORAGE_NAME=$(echo "$AZURE_ENV_NAME" | tr -cd "[:alnum:]" | tr "[:upper:]" "[:lower:]" | cut -c1-20)
STORAGE_NAME="${STORAGE_NAME}docs"  # max 24 chars, no hyphens
INDEX_NAME="${SAFE}-index"

# --- Model deployment ---
echo "► Checking model deployment..."
MODEL_COUNT=$(az cognitiveservices account deployment list   --name "$ACCOUNT" --resource-group "$RG"   --query "length(@)" -o tsv 2>/dev/null || echo "0")

if [ "$MODEL_COUNT" = "0" ]; then
  echo "  Deploying gpt-5 model (this takes 2-3 minutes)..."
  az cognitiveservices account deployment create     --name "$ACCOUNT"     --resource-group "$RG"     --deployment-name "gpt-5"     --model-name "gpt-5"     --model-version "2025-08-07"     --model-format "OpenAI"     --sku-name "GlobalStandard"     --sku-capacity 10     --output none
  echo "  ✓ Model deployed: gpt-5"
else
  echo "  ✓ Model already deployed"
fi

azd env set AZURE_AI_MODEL_DEPLOYMENT_NAME "gpt-5"

# --- AI Search service ---
echo ""
echo "► Creating AI Search service: ${SEARCH_NAME}..."
az search service create   --name "$SEARCH_NAME"   --resource-group "$RG"   --location "$LOCATION"   --sku Standard   --output none 2>/dev/null || echo "  (already exists)"
echo "  ✓ Search service ready"

SEARCH_ENDPOINT="https://${SEARCH_NAME}.search.windows.net"
SEARCH_RESOURCE_ID=$(az search service show --name "$SEARCH_NAME" --resource-group "$RG" --query id -o tsv)
SEARCH_KEY=$(az search admin-key show --service-name "$SEARCH_NAME" --resource-group "$RG" --query primaryKey -o tsv)

# --- Storage account ---
echo ""
echo "► Creating Storage account: ${STORAGE_NAME}..."
if az storage account show --name "$STORAGE_NAME" --resource-group "$RG" --output none 2>/dev/null; then
  echo "  (already exists)"
else
  az storage account create \
    --name "$STORAGE_NAME" \
    --resource-group "$RG" \
    --location "$LOCATION" \
    --sku Standard_LRS --kind StorageV2 \
    --allow-blob-public-access false \
    --output none
fi

STORAGE_RESOURCE_ID=$(az storage account show --name "$STORAGE_NAME" --resource-group "$RG" --query id -o tsv)
STORAGE_KEY=$(az storage account keys list --account-name "$STORAGE_NAME" --resource-group "$RG" --query "[0].value" -o tsv)

# Create blob container
az storage container create   --name docs   --account-name "$STORAGE_NAME"   --account-key "$STORAGE_KEY"   --output none 2>/dev/null || echo ""
echo "  ✓ Storage ready"

# --- Upload documents ---
echo ""
echo "► Uploading documents from docs/ to blob storage..."
az storage blob upload-batch   --account-name "$STORAGE_NAME"   --account-key "$STORAGE_KEY"   --destination docs   --source ./docs   --overwrite true   --output none
echo "  ✓ Documents uploaded"

# --- Grant Search identity access to Storage (for managed-identity indexer auth) ---
echo ""
echo "► Configuring Search identity permissions..."
SEARCH_PRINCIPAL=$(az search service show   --name "$SEARCH_NAME" --resource-group "$RG"   --query "identity.principalId" -o tsv 2>/dev/null || echo "")

if [ -n "$SEARCH_PRINCIPAL" ] && [ "$SEARCH_PRINCIPAL" != "null" ]; then
  az role assignment create     --assignee "$SEARCH_PRINCIPAL"     --role "Storage Blob Data Reader"     --scope "$STORAGE_RESOURCE_ID"     --output none 2>/dev/null || true
  echo "  ✓ Search identity granted Storage Blob Data Reader"
fi

# --- Create Search index ---
echo ""
echo "► Creating search index: ${INDEX_NAME}..."
curl -sf -X PUT "${SEARCH_ENDPOINT}/indexes/${INDEX_NAME}?api-version=2024-07-01"   -H "Content-Type: application/json"   -H "api-key: ${SEARCH_KEY}"   -d "{
    "name": "${INDEX_NAME}",
    "fields": [
      {"name": "id",                            "type": "Edm.String",        "key": true,  "filterable": true,  "retrievable": true},
      {"name": "content",                       "type": "Edm.String",        "searchable": true, "retrievable": true, "analyzer": "en.microsoft"},
      {"name": "metadata_storage_name",         "type": "Edm.String",        "searchable": true, "retrievable": true, "filterable": true},
      {"name": "metadata_storage_path",         "type": "Edm.String",        "retrievable": true},
      {"name": "metadata_storage_last_modified","type": "Edm.DateTimeOffset","retrievable": true}
    ],
    "semantic": {
      "configurations": [{
        "name": "default",
        "prioritizedFields": {
          "contentFields": [{"fieldName": "content"}],
          "keywordsFields": [{"fieldName": "metadata_storage_name"}]
        }
      }]
    }
  }" > /dev/null
echo "  ✓ Index created"

# --- Create Search data source ---
echo "► Creating data source..."
curl -sf -X PUT "${SEARCH_ENDPOINT}/datasources/${INDEX_NAME}-blob?api-version=2024-07-01"   -H "Content-Type: application/json"   -H "api-key: ${SEARCH_KEY}"   -d "{
    "name": "${INDEX_NAME}-blob",
    "type": "azureblob",
    "credentials": {
      "connectionString": "DefaultEndpointsProtocol=https;AccountName=${STORAGE_NAME};AccountKey=${STORAGE_KEY};EndpointSuffix=core.windows.net"
    },
    "container": {"name": "docs"}
  }" > /dev/null
echo "  ✓ Data source created"

# --- Create Search indexer ---
echo "► Creating and running indexer..."
curl -sf -X PUT "${SEARCH_ENDPOINT}/indexers/${INDEX_NAME}-indexer?api-version=2024-07-01"   -H "Content-Type: application/json"   -H "api-key: ${SEARCH_KEY}"   -d "{
    "name": "${INDEX_NAME}-indexer",
    "dataSourceName": "${INDEX_NAME}-blob",
    "targetIndexName": "${INDEX_NAME}",
    "schedule": {"interval": "PT2H"},
    "parameters": {
      "batchSize": 100,
      "configuration": {
        "dataToExtract": "contentAndMetadata",
        "parsingMode": "text"
      }
    },
    "fieldMappings": [
      {"sourceFieldName": "metadata_storage_path", "targetFieldName": "id", "mappingFunction": {"name": "base64Encode"}}
    ]
  }" > /dev/null

# Run indexer immediately
curl -sf -X POST "${SEARCH_ENDPOINT}/indexers/${INDEX_NAME}-indexer/run?api-version=2024-07-01"   -H "api-key: ${SEARCH_KEY}" > /dev/null

# Wait for indexer
echo "  Waiting for indexer to finish..."
for i in $(seq 1 18); do
  STATUS=$(curl -sf "${SEARCH_ENDPOINT}/indexers/${INDEX_NAME}-indexer/status?api-version=2024-07-01"     -H "api-key: ${SEARCH_KEY}" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('lastResult',{}).get('status','running'))" 2>/dev/null || echo "running")
  if [ "$STATUS" = "success" ] || [ "$STATUS" = "reset" ]; then
    echo "  ✓ Indexer complete (status: $STATUS)"
    break
  fi
  printf "  . %s" "$STATUS"
  sleep 5
done

# --- Save env vars for agent ---
azd env set AZURE_SEARCH_ENDPOINT "$SEARCH_ENDPOINT"
azd env set AZURE_SEARCH_INDEX "$INDEX_NAME"
azd env set AZURE_SEARCH_RESOURCE_ID "$SEARCH_RESOURCE_ID"

echo ""
echo "=== Post-Provision Complete ==="
echo "  Search endpoint : ${SEARCH_ENDPOINT}"
echo "  Search index    : ${INDEX_NAME}"
echo ""
