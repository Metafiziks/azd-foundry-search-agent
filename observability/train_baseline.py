#!/usr/bin/env python3
"""
Bootstrap IsolationForest training from eval telemetry (Azure).

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
MIN_ROWS       = int(os.environ.get("MIN_BASELINE_ROWS", "20"))


def _kql_row_to_dict(row) -> dict:
    """Map Log Analytics column names to the shared feature dict format."""
    return {
        "retrieval_score_mean":    getattr(row, "RetrievalScoreMean", None),
        "retrieval_score_std":     getattr(row, "RetrievalScoreStd", None),
        "retrieval_score_entropy": getattr(row, "RetrievalScoreEntropy", None),
        "chunk_count":             getattr(row, "ChunkCount", None),
        "reranker_score_mean":     getattr(row, "RerankerScoreMean", None),
        "search_latency_ms":       getattr(row, "SearchLatencyMs", None),
        "answer_length":           getattr(row, "AnswerLength", None),
        "citation_count":          getattr(row, "CitationCount", None),
        "hhem_score":              getattr(row, "HhemScore", None),
        "latency_ms":              getattr(row, "LatencyMs", None),
    }


def load_baseline_rows() -> list[dict]:
    from azure.identity import AzureCliCredential
    from azure.monitor.query import LogsQueryClient, LogsQueryStatus
    import datetime

    credential = AzureCliCredential()
    client = LogsQueryClient(credential)

    kql = """
    AgentTelemetry_CL
    | where IsBaseline_b == true
    | where RetrievalScoreMean_d > 0
    | project RetrievalScoreMean_d, RetrievalScoreStd_d, RetrievalScoreEntropy_d,
              ChunkCount_d, RerankerScoreMean_d, SearchLatencyMs_d,
              AnswerLength_d, CitationCount_d, HhemScore_d, LatencyMs_d
    | limit 1000
    """
    response = client.query_workspace(
        workspace_id=WORKSPACE_ID,
        query=kql,
        timespan=datetime.timedelta(days=90),
    )
    if response.status != LogsQueryStatus.SUCCESS:
        raise RuntimeError(f"KQL query failed: {response.partial_error}")

    rows = []
    for table in response.tables:
        for r in table.rows:
            rows.append({
                "retrieval_score_mean":    r[0],
                "retrieval_score_std":     r[1],
                "retrieval_score_entropy": r[2],
                "chunk_count":             r[3],
                "reranker_score_mean":     r[4],
                "search_latency_ms":       r[5],
                "answer_length":           r[6],
                "citation_count":          r[7],
                "hhem_score":              r[8],
                "latency_ms":              r[9],
            })
    return rows


def main() -> None:
    logger.info("Querying baseline telemetry from Log Analytics workspace %s...", WORKSPACE_ID)
    rows = load_baseline_rows()
    logger.info("Found %d baseline rows", len(rows))

    if len(rows) < MIN_ROWS:
        logger.error("Only %d rows (need >= %d). Run evals first.", len(rows), MIN_ROWS)
        sys.exit(1)

    X = np.array([from_row(r) for r in rows])
    logger.info("Feature matrix: %s", X.shape)

    model = train_and_upload(
        X=X,
        account_url=BLOB_ACCOUNT,
        container=BLOB_CONTAINER,
        key=BLOB_KEY,
        local_path="/tmp/iforest_baseline.pkl",
    )
    print(f"\n✓ IsolationForest trained on {len(X)} samples")
    print(f"✓ Model uploaded → {BLOB_ACCOUNT}/{BLOB_CONTAINER}/{BLOB_KEY}")
    print(f"  Agent container will pick up new model on next cold start")


if __name__ == "__main__":
    main()
