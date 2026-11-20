#!/usr/bin/env python3
"""
Bootstrap IsolationForest training from hosted retrieval telemetry (Azure).

Queries baseline rows from Log Analytics Workspace (KQL), trains an IForest,
and uploads model.pkl to Azure Blob Storage for the agent to load.

Usage:
    LOG_ANALYTICS_WORKSPACE_ID=<workspace-id> \
    BLOB_ACCOUNT_URL=https://<account>.blob.core.windows.net \
    BLOB_MODEL_CONTAINER=models \
    BLOB_MODEL_KEY=iforest.pkl \
    python3 observability/train_baseline.py
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from observability.isolation_forest import train_and_upload
from observability.shared.features import RETRIEVAL_FEATURE_NAMES, retrieval_from_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BLOB_CONTAINER = os.environ.get("BLOB_MODEL_CONTAINER", "models")
BLOB_KEY       = os.environ.get("BLOB_MODEL_KEY", "iforest.pkl")
MIN_ROWS       = int(os.environ.get("MIN_BASELINE_ROWS", "20"))


def load_retrieval_rows(workspace_id: str, timespan: datetime.timedelta, limit: int = 1000) -> list[dict]:
    from azure.identity import AzureCliCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    credential = AzureCliCredential()
    client = LogsQueryClient(credential)

    kql = f"""
    AgentTelemetry_CL
    | where Source_s == "runtime" and RetrievalScoreMean_d > 0
    | top {limit} by TimeGenerated desc
    | project RetrievalScoreMean_d, RetrievalScoreStd_d, RetrievalScoreEntropy_d,
              ChunkCount_d, RerankerScoreMean_d, SearchLatencyMs_d
    """
    response = client.query_workspace(
        workspace_id=workspace_id,
        query=kql,
        timespan=timespan,
    )
    if response.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(f"KQL query failed: {response.partial_error}")

    return [dict(zip(RETRIEVAL_FEATURE_NAMES, row, strict=True))
            for table in response.tables for row in table.rows if None not in row]


def main() -> None:
    workspace_id = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
    blob_account = os.environ["BLOB_ACCOUNT_URL"]
    logger.info("Querying baseline telemetry from Log Analytics workspace %s...", workspace_id)
    deadline = time.monotonic() + int(os.environ.get("BASELINE_INGEST_WAIT_SECONDS", "0"))
    while True:
        rows = load_retrieval_rows(workspace_id, datetime.timedelta(days=7))
        remaining = deadline - time.monotonic()
        if len(rows) >= MIN_ROWS or remaining <= 0:
            break
        logger.info("Only %d/%d retrieval rows are visible; waiting for Azure Monitor ingestion.", len(rows), MIN_ROWS)
        time.sleep(min(15, remaining))
    logger.info("Found %d baseline rows", len(rows))

    if len(rows) < MIN_ROWS:
        logger.error("Only %d rows (need >= %d). Run evals first.", len(rows), MIN_ROWS)
        sys.exit(1)

    X = np.array([retrieval_from_row(r) for r in rows])
    logger.info("Feature matrix: %s", X.shape)

    train_and_upload(
        X=X,
        account_url=blob_account,
        container=BLOB_CONTAINER,
        key=BLOB_KEY,
        local_path="/tmp/iforest_baseline.pkl",
    )
    print(f"\n✓ IsolationForest trained on {len(X)} samples")
    print(f"✓ Model uploaded → {blob_account}/{BLOB_CONTAINER}/{BLOB_KEY}")
    print("  The agent loads the model on the next retrieval if it has no cached model")


if __name__ == "__main__":
    main()
