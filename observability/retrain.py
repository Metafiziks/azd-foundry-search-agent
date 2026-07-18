#!/usr/bin/env python3
"""
Scheduled IsolationForest retraining (Azure). Queries last N days from
Log Analytics, retrains IForest, uploads new model to Blob Storage.
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent))
from observability.isolation_forest import train_and_upload
from observability.shared.features import from_bq_row as from_row

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WORKSPACE_ID   = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
BLOB_ACCOUNT   = os.environ["BLOB_ACCOUNT_URL"]
BLOB_CONTAINER = os.environ.get("BLOB_MODEL_CONTAINER", "models")
BLOB_KEY       = os.environ.get("BLOB_MODEL_KEY", "iforest.pkl")
LOOKBACK_DAYS  = int(os.environ.get("RETRAIN_LOOKBACK_DAYS", "30"))
MIN_ROWS       = int(os.environ.get("MIN_RETRAIN_ROWS", "20"))


def load_recent_rows() -> list[dict]:
    from azure.identity import AzureCliCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus

    client = LogsQueryClient(AzureCliCredential())
    kql = f"""
    AgentTelemetry_CL
    | where TimeGenerated >= ago({LOOKBACK_DAYS}d)
    | where RetrievalScoreMean_d > 0
    | project RetrievalScoreMean_d, RetrievalScoreStd_d, RetrievalScoreEntropy_d,
              ChunkCount_d, RerankerScoreMean_d, SearchLatencyMs_d,
              AnswerLength_d, CitationCount_d, HhemScore_d, LatencyMs_d
    | limit 5000
    """
    response = client.query_workspace(
        workspace_id=WORKSPACE_ID,
        query=kql,
        timespan=datetime.timedelta(days=LOOKBACK_DAYS),
    )
    if response.status.name != "SUCCESS":
        logger.warning("KQL query failed — falling back to baseline only: %s", response.partial_error)
        return []
    rows = []
    for table in response.tables:
        for r in table.rows:
            rows.append({
                "retrieval_score_mean":    r[0], "retrieval_score_std":     r[1],
                "retrieval_score_entropy": r[2], "chunk_count":             r[3],
                "reranker_score_mean":     r[4], "search_latency_ms":       r[5],
                "answer_length":           r[6], "citation_count":          r[7],
                "hhem_score":              r[8], "latency_ms":              r[9],
            })
    return rows


def main() -> None:
    logger.info("Querying last %d days of telemetry...", LOOKBACK_DAYS)
    rows = load_recent_rows()
    logger.info("Found %d rows", len(rows))

    if len(rows) < MIN_ROWS:
        print(f"⚠ Only {len(rows)} rows — not enough to retrain. Keeping existing model.")
        sys.exit(0)

    X = np.array([from_row(r) for r in rows])
    model = train_and_upload(X=X, account_url=BLOB_ACCOUNT, container=BLOB_CONTAINER,
                              key=BLOB_KEY, local_path="/tmp/iforest_retrained.pkl")

    print(f"\n✓ IsolationForest retrained on {len(X)} samples ({LOOKBACK_DAYS}-day window)")
    print(f"✓ Model uploaded → {BLOB_ACCOUNT}/{BLOB_CONTAINER}/{BLOB_KEY}")
    print(f"  Agent will pick up new model on next cold start")


if __name__ == "__main__":
    main()
