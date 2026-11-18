#!/usr/bin/env python3
"""Create a separate Purview-enabled index and Blob indexer using Azure CLI RBAC."""

import argparse
import json
import re
import subprocess


def az_rest(method, url, resource, body=None):
    command = ["az", "rest", "--method", method, "--url", url, "--resource", resource]
    if body is not None:
        command += ["--headers", "If-None-Match=*", "--body", json.dumps(body)]
    return subprocess.check_output(command, text=True)


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
    return [("indexes", index_definition), ("datasources", source), ("indexers", indexer)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--search-service", required=True, help="Name of the existing Azure AI Search service")
    parser.add_argument("--index", required=True, help="Name for a new index (up to 100 characters)")
    parser.add_argument("--storage-resource-id", required=True)
    parser.add_argument("--container", required=True)
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

    arm = "https://management.azure.com"
    storage_url = f"{arm}{storage_id}"
    account = json.loads(az_rest("get", f"{storage_url}?api-version=2023-05-01", arm))
    blob = json.loads(az_rest("get", f"{storage_url}/blobServices/default?api-version=2023-05-01", arm))
    if account["properties"].get("allowBlobPublicAccess") is not False:
        parser.error("Use a storage account with anonymous Blob access explicitly disabled")
    if not blob["properties"].get("deleteRetentionPolicy", {}).get("enabled"):
        parser.error("Enable Blob soft delete before creating the indexer")
    if blob["properties"].get("isVersioningEnabled"):
        parser.error("Native Search deletion detection requires Blob versioning to be disabled")

    endpoint = f"https://{args.search_service}.search.windows.net"
    for collection, body in definitions(args.index, storage_id, args.container):
        url = f"{endpoint}/{collection}/{body['name']}?api-version=2026-11-01-preview"
        az_rest("put", url, "https://search.azure.com", body)
        print(f"Created {collection}/{body['name']}", flush=True)
    print("Verify the initial indexer execution and labeled documents before sending user traffic.")


if __name__ == "__main__":
    main()
