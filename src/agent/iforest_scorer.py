"""
IsolationForest scorer for the Azure AI Foundry agent.
Loads model from Azure Blob on first call (lazy, module-level cache).
Falls back gracefully if model not trained yet or Blob unreachable.
"""
from __future__ import annotations

import io
import logging
import math
import os
import pickle
import threading

import numpy as np

logger = logging.getLogger(__name__)

BLOB_ACCOUNT_URL  = os.environ.get("BLOB_ACCOUNT_URL", "")   # https://<account>.blob.core.windows.net
BLOB_CONTAINER    = os.environ.get("BLOB_MODEL_CONTAINER", "models")
BLOB_MODEL_KEY    = os.environ.get("BLOB_MODEL_KEY", "iforest.pkl")

_model = None
_load_lock = threading.Lock()


def _load_model():
    global _model
    if _model is not None:
        return _model
    with _load_lock:
        if _model is not None:
            return _model
        if not BLOB_ACCOUNT_URL:
            return None
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
            credential = DefaultAzureCredential()
            client = BlobServiceClient(account_url=BLOB_ACCOUNT_URL, credential=credential)
            blob = client.get_blob_client(container=BLOB_CONTAINER, blob=BLOB_MODEL_KEY)
            data = blob.download_blob().readall()
            _model = pickle.loads(data)
            logger.info("IForest loaded from %s/%s/%s", BLOB_ACCOUNT_URL, BLOB_CONTAINER, BLOB_MODEL_KEY)
        except Exception as exc:
            logger.warning("IForest load failed (non-fatal): %s", exc)
    return _model


def _entropy(scores: list[float]) -> float:
    if not scores or sum(scores) == 0:
        return 0.0
    total = sum(scores)
    probs = [s / total for s in scores]
    return -sum(p * math.log(p + 1e-10) for p in probs)


def score(
    retrieval_scores: list[float],
    reranker_scores: list[float],
    search_latency_ms: float,
) -> tuple[float, bool]:
    """Returns (anomaly_score, is_anomaly). Returns (0.0, False) on error."""
    model = _load_model()
    if model is None:
        return 0.0, False
    try:
        rs = retrieval_scores or [0.0]
        rr = reranker_scores or [0.0]
        x = np.array([
            float(np.mean(rs)),
            float(np.std(rs)) if len(rs) > 1 else 0.0,
            _entropy(rs),
            float(len(rs)),
            float(np.mean(rr)),
            search_latency_ms,
        ], dtype=np.float64).reshape(1, -1)
        decision = float(model.decision_function(x)[0])
        label    = int(model.predict(x)[0])
        return decision, label == -1
    except Exception as exc:
        logger.warning("IForest score failed (non-fatal): %s", exc)
        return 0.0, False
