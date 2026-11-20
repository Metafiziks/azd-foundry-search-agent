#!/usr/bin/env python3
"""Create a separate Purview-enabled index and Blob indexer using Azure CLI RBAC."""

import argparse
import json
import re
import subprocess
import time
import urllib.error
import urllib.request

SEARCH_API_VERSION = "2026-11-01-preview"


def blob_source_description(storage_id):
    return f"Protected corpus storage: {storage_id}"


def request(method, url, token, body=None, create_only=False):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if create_only:
        headers["If-None-Match"] = "*"
    payload = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(url, data=payload, headers=headers, method=method),
                               timeout=30) as response:
        return response.read()


def deploy_search(url, token, body, resume):
    # A new Search Service Contributor assignment can take several minutes to propagate.
    for attempt in range(13):
        try:
            request("PUT", url, token, body, create_only=not resume)
            return
        except urllib.error.HTTPError as error:
            if not resume or error.code not in (401, 403) or attempt == 12:
                details = error.read().decode(errors="replace")[:2000]
                raise SystemExit(f"Search configuration failed: HTTP {error.code}: {details}") from None
            print(f"Waiting for Search RBAC (HTTP {error.code})...", flush=True)
            time.sleep(20)


def search_token(subscription):
    return subprocess.check_output(
        ["az", "account", "get-access-token", "--subscription", subscription,
         "--resource", "https://search.azure.com", "--query", "accessToken", "-o", "tsv"],
        text=True,
    ).strip()


def definitions(index, storage_id, container):
    metadata = (
        ("metadata_sensitivity_label", "sensitivity_label_id", "sensitivityLabelId"),
        ("metadata_sensitivity_label_name", "sensitivity_label_name", "sensitivityLabelName"),
        ("metadata_source_document_id", "source_document_id", "sourceDocumentId"),
    )
    index_definition = {
        "name": index,
        "purviewEnabled": True,
        "fields": [
            {"name": "id", "type": "Edm.String", "key": True},
            {"name": "content", "type": "Edm.String", "searchable": True, "retrievable": True},
            {"name": "metadata_storage_name", "type": "Edm.String", "searchable": True, "retrievable": True},
            *(
                {"name": name, "type": "Edm.String", flag: True, "retrievable": True,
                 "filterable": flag == "sensitivityLabelId"}
                for _, name, flag in metadata
            ),
        ],
        "semantic": {"configurations": [{
            "name": "default",
            "prioritizedFields": {
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [{"fieldName": "metadata_storage_name"}],
            },
        }]},
    }
    source = {
        "name": f"{index}-blob",
        "type": "azureblob",
        "description": blob_source_description(storage_id),
        "credentials": {"connectionString": f"ResourceId={storage_id}/;"},
        "container": {"name": container},
        "indexerPermissionOptions": ["sensitivityLabel"],
        "dataDeletionDetectionPolicy": {"@odata.type": "#Microsoft.Azure.Search.NativeBlobSoftDeleteDeletionDetectionPolicy"},
    }
    indexer = {
        "name": f"{index}-indexer",
        "dataSourceName": source["name"],
        "targetIndexName": index,
        "schedule": {"interval": "PT2H"},
        "parameters": {
            "maxFailedItems": 0,
            "maxFailedItemsPerBatch": 0,
            "configuration": {"dataToExtract": "contentAndMetadata", "parsingMode": "default"},
        },
        "fieldMappings": [
            {"sourceFieldName": "metadata_storage_path", "targetFieldName": "id",
             "mappingFunction": {"name": "base64Encode"}},
            *({"sourceFieldName": field, "targetFieldName": name} for field, name, _ in metadata),
        ],
    }
    knowledge_source = {
        "name": f"{index}-source",
        "kind": "searchIndex",
        "searchIndexParameters": {
            "searchIndexName": index,
            "semanticConfigurationName": "default",
            "sourceDataFields": [{"name": field} for field in ("metadata_storage_name", "content", "sensitivity_label_id")],
        },
    }
    knowledge_base = {
        "name": f"{index}-kb",
        "knowledgeSources": [{"name": knowledge_source["name"]}],
        "retrievalReasoningEffort": {"kind": "minimal"},
        "outputMode": "extractiveData",
    }
    return [
        ("indexes", index_definition), ("datasources", source), ("indexers", indexer),
        ("knowledgesources", knowledge_source), ("knowledgebases", knowledge_base),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-service", required=True, help="Name of the existing Azure AI Search service")
    parser.add_argument("--index", required=True, help="Name for a new index (up to 100 characters)")
    parser.add_argument("--storage-resource-id", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--resume", action="store_true", help="Update the dedicated, azd-managed Search objects")
    args = parser.parse_args()
    storage_id = args.storage_resource_id.rstrip("/")
    if not re.fullmatch(r"/subscriptions/[a-fA-F0-9-]+/resourceGroups/[^/?#]+/providers/"
                        r"Microsoft\.Storage/storageAccounts/[a-z0-9]+", storage_id, re.IGNORECASE):
        parser.error("--storage-resource-id must be an Azure Storage account resource ID")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,99}", args.index):
        parser.error("--index must be a new lowercase Search index name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", args.search_service):
        parser.error("--search-service must be an Azure AI Search service name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", args.container):
        parser.error("--container must be a named Azure Blob container")

    subscription = storage_id.split("/")[2]

    def token(resource):
        return subprocess.check_output(
            ["az", "account", "get-access-token", "--subscription", subscription, "--resource", resource,
             "--query", "accessToken", "-o", "tsv"], text=True
        ).strip()

    arm = "https://management.azure.com"
    arm_token = token(arm)
    storage_url = f"{arm}{storage_id}"
    account = json.loads(request("GET", f"{storage_url}?api-version=2023-05-01", arm_token))
    blob = json.loads(request("GET", f"{storage_url}/blobServices/default?api-version=2023-05-01", arm_token))
    if account["properties"].get("allowBlobPublicAccess") is not False:
        parser.error("Use a storage account with anonymous Blob access explicitly disabled")
    if not blob["properties"].get("deleteRetentionPolicy", {}).get("enabled"):
        parser.error("Enable Blob soft delete before creating the indexer")
    if blob["properties"].get("isVersioningEnabled"):
        parser.error("Native Search deletion detection requires Blob versioning to be disabled")

    endpoint = f"https://{args.search_service}.search.windows.net"
    bearer = search_token(subscription)
    for collection, body in definitions(args.index, storage_id, args.container):
        url = f"{endpoint}/{collection}/{body['name']}?api-version={SEARCH_API_VERSION}"
        deploy_search(url, bearer, body, args.resume)
        print(f"Configured {collection}/{body['name']}", flush=True)
    print("Provisioned a protected Foundry IQ knowledge base. No documents were uploaded by setup.")


if __name__ == "__main__":
    main()
