"""
Log Analytics telemetry sink for the Azure AI Foundry agent.

Ingests structured telemetry rows via the Azure Monitor Logs Ingestion API
(DCE + DCR). Fires in a background daemon thread so the agent response path
is never blocked.

Required env vars:
  LOG_ANALYTICS_DCE_ENDPOINT or LOG_ANALYTICS_DCE — e.g. https://xxx.ingest.monitor.azure.com
  LOG_ANALYTICS_DCR_IMMUTABLE_ID — e.g. dcr-xxxxxxxxxxxxxxxx
  LOG_ANALYTICS_STREAM_NAME   — e.g. Custom-AgentTelemetry_CL (default)
"""
from __future__ import annotations

import datetime
import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

DCE_ENDPOINT  = os.environ.get("LOG_ANALYTICS_DCE_ENDPOINT") or os.environ.get("LOG_ANALYTICS_DCE", "")
DCR_ID        = os.environ.get("LOG_ANALYTICS_DCR_IMMUTABLE_ID", "")
STREAM_NAME   = os.environ.get("LOG_ANALYTICS_STREAM_NAME", "Custom-AgentTelemetry_CL")

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                if not DCE_ENDPOINT or not DCR_ID:
                    return None
                try:
                    from azure.identity import DefaultAzureCredential
                    from azure.monitor.ingestion import LogsIngestionClient
                    _client = LogsIngestionClient(
                        endpoint=DCE_ENDPOINT,
                        credential=DefaultAzureCredential(),
                    )
                except Exception as exc:
                    logger.warning("Log Analytics client init failed (non-fatal): %s", exc)
    return _client


def _write(row: dict) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.upload(
            rule_id=DCR_ID,
            stream_name=STREAM_NAME,
            logs=[row],
        )
    except Exception as exc:
        logger.warning("Log Analytics write failed (non-fatal): %s", exc)


def log_async(
    *,
    request_id: str,
    query: str,
    source: str = "runtime",
    search_latency_ms: float = 0.0,
    retrieval_score_mean: float = 0.0,
    retrieval_score_std: float = 0.0,
    retrieval_score_entropy: float = 0.0,
    chunk_count: int = 0,
    reranker_score_mean: float = 0.0,
    anomaly_score: float = 0.0,
    is_anomaly: bool = False,
    is_baseline: bool = False,
    answer_length: Optional[int] = None,
    citation_count: Optional[int] = None,
    hhem_score: Optional[float] = None,
    latency_ms: Optional[float] = None,
    memory_enabled: Optional[bool] = None,
    memory_read_count: Optional[int] = None,
    memory_write_count: Optional[int] = None,
    memory_latency_ms: Optional[float] = None,
    memory_status: Optional[str] = None,
) -> None:
    """Enqueue a telemetry row to Log Analytics. Returns immediately."""
    row = {
        "TimeGenerated":          datetime.datetime.utcnow().isoformat() + "Z",
        "RequestId":              request_id,
        "Query":                  query[:1024],
        "Source":                 source,
        "SearchLatencyMs":        round(search_latency_ms, 2),
        "RetrievalScoreMean":     round(retrieval_score_mean, 6),
        "RetrievalScoreStd":      round(retrieval_score_std, 6),
        "RetrievalScoreEntropy":  round(retrieval_score_entropy, 6),
        "ChunkCount":             chunk_count,
        "RerankerScoreMean":      round(reranker_score_mean, 6),
        "AnomalyScore":           round(anomaly_score, 6),
        "IsAnomaly":              is_anomaly,
        "IsBaseline":             is_baseline,
        "AnswerLength":           answer_length,
        "CitationCount":          citation_count,
        "HhemScore":              round(hhem_score, 6) if hhem_score is not None else None,
        "LatencyMs":              round(latency_ms, 2) if latency_ms is not None else None,
        "MemoryEnabled":          memory_enabled,
        "MemoryReadCount":        memory_read_count,
        "MemoryWriteCount":       memory_write_count,
        "MemoryLatencyMs":        round(memory_latency_ms, 2) if memory_latency_ms is not None else None,
        "MemoryStatus":           memory_status,
    }
    threading.Thread(target=_write, args=(row,), daemon=True).start()
