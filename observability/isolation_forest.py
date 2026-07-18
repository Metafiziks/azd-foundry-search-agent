"""
IsolationForest wrapper for Azure — train, persist to Blob Storage, load for scoring.
"""
from __future__ import annotations

import io
import logging
import os
import pickle
from typing import Optional

import numpy as np
from sklearn.ensemble import IsolationForest

logger = logging.getLogger(__name__)

CONTAMINATION = float(os.environ.get("IFOREST_CONTAMINATION", "0.05"))


def train(X: np.ndarray, contamination: float = CONTAMINATION) -> IsolationForest:
    if len(X) < 10:
        raise ValueError(f"Need at least 10 samples, got {len(X)}")
    model = IsolationForest(n_estimators=100, contamination=contamination, random_state=42, n_jobs=-1)
    model.fit(X)
    logger.info("IForest trained: %d samples, %d features", len(X), X.shape[1])
    return model


def save_local(model: IsolationForest, path: str) -> None:
    with open(path, "wb") as f:
        pickle.dump(model, f)


def upload_to_blob(local_path: str, account_url: str, container: str, key: str) -> None:
    from azure.identity import AzureCliCredential
    from azure.storage.blob import BlobServiceClient
    client = BlobServiceClient(account_url=account_url, credential=AzureCliCredential())
    with open(local_path, "rb") as f:
        client.get_blob_client(container=container, blob=key).upload_blob(f, overwrite=True)
    logger.info("IForest model uploaded: %s/%s/%s", account_url, container, key)


def load_from_blob(account_url: str, container: str, key: str) -> Optional[IsolationForest]:
    try:
        from azure.identity import AzureCliCredential
        from azure.storage.blob import BlobServiceClient
        client = BlobServiceClient(account_url=account_url, credential=AzureCliCredential())
        data = client.get_blob_client(container=container, blob=key).download_blob().readall()
        return pickle.loads(data)
    except Exception as exc:
        logger.warning("Could not load IForest from Blob: %s", exc)
        return None


def train_and_upload(
    X: np.ndarray,
    account_url: str,
    container: str,
    key: str,
    local_path: str = "/tmp/iforest.pkl",
    contamination: float = CONTAMINATION,
) -> IsolationForest:
    model = train(X, contamination=contamination)
    save_local(model, local_path)
    upload_to_blob(local_path, account_url, container, key)
    return model


def score_features(model: IsolationForest, features: np.ndarray) -> tuple[float, bool]:
    x = features.reshape(1, -1)
    return float(model.decision_function(x)[0]), int(model.predict(x)[0]) == -1
