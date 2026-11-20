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
from observability.shared.features import retrieval_from_row
from observability.train_baseline import load_retrieval_rows

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

WORKSPACE_ID   = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
BLOB_ACCOUNT   = os.environ["BLOB_ACCOUNT_URL"]
BLOB_CONTAINER = os.environ.get("BLOB_MODEL_CONTAINER", "models")
BLOB_KEY       = os.environ.get("BLOB_MODEL_KEY", "iforest.pkl")
LOOKBACK_DAYS  = int(os.environ.get("RETRAIN_LOOKBACK_DAYS", "30"))
MIN_ROWS       = int(os.environ.get("MIN_RETRAIN_ROWS", "20"))


def main() -> None:
    logger.info("Querying last %d days of telemetry...", LOOKBACK_DAYS)
    rows = load_retrieval_rows(WORKSPACE_ID, datetime.timedelta(days=LOOKBACK_DAYS), limit=5000)
    logger.info("Found %d rows", len(rows))

    if len(rows) < MIN_ROWS:
        print(f"⚠ Only {len(rows)} rows — not enough to retrain. Keeping existing model.")
        sys.exit(0)

    X = np.array([retrieval_from_row(r) for r in rows])
    train_and_upload(X=X, account_url=BLOB_ACCOUNT, container=BLOB_CONTAINER,
                     key=BLOB_KEY, local_path="/tmp/iforest_retrained.pkl")

    print(f"\n✓ IsolationForest retrained on {len(X)} samples ({LOOKBACK_DAYS}-day window)")
    print(f"✓ Model uploaded → {BLOB_ACCOUNT}/{BLOB_CONTAINER}/{BLOB_KEY}")
    print(f"  Agent will pick up new model on next cold start")


if __name__ == "__main__":
    main()
