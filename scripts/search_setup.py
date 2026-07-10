#!/usr/bin/env python3
"""search_setup.py - called from postprovision.sh to handle Search REST API calls."""
import json, sys, time, urllib.request, urllib.error

endpoint, key, index, storage, storage_key = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]

def search_put(path, body):
    url = f"{endpoint}{path}?api-version=2024-07-01"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="PUT")
    req.add_header("api-key", key)
    req.add_header("Content-Type", "application/json")
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code in (200, 201, 204):
            return
        print(f"  HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

def search_post(path):
    url = f"{endpoint}{path}?api-version=2024-07-01"
    req = urllib.request.Request(url, data=b"", method="POST")
    req.add_header("api-key", key)
    req.add_header("Content-Length", "0")
    try:
        urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code in (200, 201, 202, 204):
            return
        print(f"  HTTP {e.code}: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)

def search_get(path):
    url = f"{endpoint}{path}?api-version=2024-07-01"
    req = urllib.request.Request(url, method="GET")
    req.add_header("api-key", key)
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read())

print("  Creating index...", flush=True)
search_put(f"/indexes/{index}", {
    "name": index,
    "fields": [
        {"name": "id",                             "type": "Edm.String",         "key": True,  "filterable": True, "retrievable": True},
        {"name": "content",                        "type": "Edm.String",         "searchable": True, "retrievable": True, "analyzer": "en.microsoft"},
        {"name": "metadata_storage_name",          "type": "Edm.String",         "searchable": True, "retrievable": True, "filterable": True},
        {"name": "metadata_storage_path",          "type": "Edm.String",         "retrievable": True},
        {"name": "source_url",                     "type": "Edm.String",         "retrievable": True},
        {"name": "metadata_storage_last_modified", "type": "Edm.DateTimeOffset", "retrievable": True}
    ],
    "semantic": {
        "configurations": [{
            "name": "default",
            "prioritizedFields": {
                "prioritizedContentFields": [{"fieldName": "content"}],
                "prioritizedKeywordsFields": [{"fieldName": "metadata_storage_name"}]
            }
        }]
    }
})

print("  Creating data source...", flush=True)
conn_str = f"DefaultEndpointsProtocol=https;AccountName={storage};AccountKey={storage_key};EndpointSuffix=core.windows.net"
search_put(f"/datasources/{index}-blob", {
    "name": f"{index}-blob",
    "type": "azureblob",
    "credentials": {"connectionString": conn_str},
    "container": {"name": "docs"}
})

print("  Creating indexer...", flush=True)
search_put(f"/indexers/{index}-indexer", {
    "name": f"{index}-indexer",
    "dataSourceName": f"{index}-blob",
    "targetIndexName": index,
    "schedule": {"interval": "PT2H"},
    "parameters": {
        "batchSize": 100,
        "configuration": {"dataToExtract": "contentAndMetadata", "parsingMode": "text"}
    },
    "fieldMappings": [
        {"sourceFieldName": "metadata_storage_path", "targetFieldName": "id",
         "mappingFunction": {"name": "base64Encode"}},
        {"sourceFieldName": "metadata_storage_path", "targetFieldName": "source_url"}
    ]
})

print("  Running indexer...", flush=True)
search_post(f"/indexers/{index}-indexer/run")

print("  Waiting for indexer...", flush=True)
for i in range(24):
    time.sleep(5)
    try:
        status = search_get(f"/indexers/{index}-indexer/status")
        s = status.get("lastResult", {}).get("status", "running")
        if s in ("success", "reset"):
            print(f"  ✓ Indexer complete", flush=True)
            sys.exit(0)
        if s not in ("running", "inProgress"):
            print(f"  (indexer status: {s})", flush=True)
    except Exception as e:
        print(f"  (status check: {e}, retrying...)", flush=True)

print("  ⚠ Indexer did not finish in time — check Azure portal", flush=True)
