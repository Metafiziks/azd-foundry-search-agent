#!/usr/bin/env python3
"""Upload a protected corpus using Entra RBAC and verify a fresh Foundry IQ indexer run."""

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError

from setup_purview_index import SEARCH_API_VERSION, blob_source_description, request, search_token

SUPPORTED_FILES = {".docx", ".pptx", ".xlsx", ".pdf", ".txt"}


def corpus_files(directory):
    root = Path(directory).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("The corpus must be a directory")
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in SUPPORTED_FILES
        and not any(part.startswith((".", "~$")) for part in path.relative_to(root).parts)
    )
    if not files:
        raise ValueError("No supported documents in the corpus")
    if any(path.is_symlink() or not path.resolve().is_relative_to(root) for path in files):
        raise ValueError("The corpus must not contain document symlinks")
    return root, files


def matches_corpus(source, indexer, index, account, index_name):
    # Search omits connection strings from GET. The setup helper records the non-secret ARM
    # resource ID in the source description so ingestion can check the intended destination.
    connection = (source.get("credentials") or {}).get("connectionString")
    description = source.get("description")
    storage_id = account["id"]
    return (
        account.get("allowBlobPublicAccess") is False
        and account.get("allowSharedKeyAccess") is False
        and index.get("purviewEnabled") is True
        and indexer.get("targetIndexName") == index_name
        and indexer.get("dataSourceName") == f"{index_name}-blob"
        and source.get("name") == f"{index_name}-blob"
        and source.get("type") == "azureblob"
        and source.get("container", {}).get("name") == "docs"
        and "sensitivityLabel" in (source.get("indexerPermissionOptions") or [])
        and isinstance(description, str)
        and description.lower() == blob_source_description(storage_id).lower()
        and (
            connection in (None, "<redacted>")
            or isinstance(connection, str) and connection.lower() == f"ResourceId={storage_id}/;".lower()
        )
    )


def index_uploaded_files(endpoint, index, token, minimum, timeout, show_details=False):
    indexer = f"{endpoint}/indexers/{index}-indexer"
    suffix = f"?api-version={SEARCH_API_VERSION}"
    deadline = time.monotonic() + timeout
    before = json.loads(request("GET", f"{indexer}/status{suffix}", token))
    previous = (before.get("lastResult") or {}).get("startTime")

    while True:
        try:
            request("POST", f"{indexer}/run{suffix}", token)
            break
        except HTTPError as exc:
            if exc.code != 409 or time.monotonic() >= deadline:
                raise
        time.sleep(5)

    while time.monotonic() < deadline:
        current = json.loads(request("GET", f"{indexer}/status{suffix}", token)).get("lastResult") or {}
        started = current.get("startTime")
        if started and started != previous and current.get("endTime"):
            count = current.get("itemsProcessed")
            if (
                current.get("status") != "success"
                or current.get("itemsFailed") != 0
                or current.get("errors")
                or current.get("warnings")
                or not isinstance(count, int)
                or count < minimum
            ):
                detail = current.get("errorMessage") if show_details else None
                diagnostic = f" Indexer message: {detail[:1000]}" if isinstance(detail, str) and detail else ""
                raise RuntimeError(
                    f"Indexer did not verify ingestion: status={current.get('status')}, "
                    f"processed={count}, failed={current.get('itemsFailed')}, "
                    f"errors={len(current.get('errors') or [])}, warnings={len(current.get('warnings') or [])}. "
                    f"Inspect the indexer status in Azure before accepting traffic.{diagnostic}"
                )
            return count
        time.sleep(5)
    raise TimeoutError("Timed out waiting for a fresh Foundry IQ indexer execution")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--search-service", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--storage-account", required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--timeout", type=int, default=600, help="Indexer wait in seconds")
    parser.add_argument("--show-indexer-error", action="store_true",
                        help="Show the indexer's run-level diagnostic locally (can include corpus names)")
    args = parser.parse_args()
    for name, value, pattern in (
        ("subscription", args.subscription, r"[a-fA-F0-9-]{36}"),
        ("search service", args.search_service, r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"),
        ("index", args.index, r"[a-z0-9][a-z0-9-]{0,99}"),
        ("storage account", args.storage_account, r"[a-z0-9]{3,24}"),
    ):
        if not re.fullmatch(pattern, value):
            parser.error(f"Invalid {name}")
    if args.timeout < 10:
        parser.error("--timeout must be at least 10 seconds")

    endpoint = f"https://{args.search_service}.search.windows.net"
    try:
        root, files = corpus_files(args.directory)
        token = search_token(args.subscription)
        # Refuse to upload private files unless the configured indexer actually targets this private corpus.
        indexer = json.loads(request(
            "GET", f"{endpoint}/indexers/{args.index}-indexer?api-version={SEARCH_API_VERSION}", token,
        ))
        source = json.loads(request(
            "GET", f"{endpoint}/datasources/{args.index}-blob?api-version={SEARCH_API_VERSION}", token,
        ))
        index = json.loads(request("GET", f"{endpoint}/indexes/{args.index}?api-version={SEARCH_API_VERSION}", token))
        account = json.loads(subprocess.check_output([
            "az", "storage", "account", "show", "--subscription", args.subscription,
            "--name", args.storage_account, "--output", "json", "--only-show-errors",
        ]))
        if not matches_corpus(source, indexer, index, account, args.index):
            raise RuntimeError("The protected indexer and private storage account do not match. Run azd up to configure them.")
        for path in files:
            subprocess.run([
                "az", "storage", "blob", "upload", "--subscription", args.subscription,
                "--auth-mode", "login", "--account-name", args.storage_account,
                "--container-name", "docs", "--name", path.relative_to(root).as_posix(),
                "--file", str(path), "--overwrite", "true", "--only-show-errors", "--output", "none",
            ], check=True)
        count = index_uploaded_files(
            endpoint, args.index, token, len(files), args.timeout, show_details=args.show_indexer_error,
        )
    except HTTPError as exc:
        raise SystemExit(f"Foundry IQ ingestion failed: HTTP {exc.code}") from None
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, URLError):
            raise SystemExit("Could not reach Foundry IQ") from None
        if isinstance(exc, subprocess.CalledProcessError):
            raise SystemExit("Azure CLI failed during protected ingestion") from None
        raise SystemExit(str(exc)) from None
    print(f"Uploaded {len(files)} documents; the fresh indexer run processed {count} items.")
    print("Verify per-user label enforcement with the live acceptance test before routing traffic.")


if __name__ == "__main__":
    main()
