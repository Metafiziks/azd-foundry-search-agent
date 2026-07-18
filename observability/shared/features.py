"""
Feature vector extraction for the IsolationForest models.

Two feature sets:
  retrieval_features (6-d) — available at runtime from every search call
  full_features (10-d)     — available from eval runs (includes answer quality)

These match the feature order expected by trained IForest models.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np

# ── Feature names ────────────────────────────────────────────────────────────

RETRIEVAL_FEATURE_NAMES = [
    "retrieval_score_mean",     # mean relevance score across retrieved chunks
    "retrieval_score_std",      # spread — low std can mean all chunks equally bad
    "retrieval_score_entropy",  # entropy of score distribution (higher = more uniform)
    "chunk_count",              # number of chunks returned
    "reranker_score_mean",      # mean Vertex AI Ranking score (post-rerank)
    "search_latency_ms",        # wall-clock search time
]

FULL_FEATURE_NAMES = RETRIEVAL_FEATURE_NAMES + [
    "answer_length",    # character count of final answer
    "citation_count",   # number of source citations in answer
    "hhem_score",       # HHEM hallucination probability [0,1]
    "latency_ms",       # end-to-end request latency
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _entropy(scores: list[float]) -> float:
    """Shannon entropy over a normalised score distribution."""
    if not scores or sum(scores) == 0:
        return 0.0
    total = sum(scores)
    probs = [s / total for s in scores]
    return -sum(p * math.log(p + 1e-10) for p in probs)


# ── Feature extractors ────────────────────────────────────────────────────────

def retrieval_features(
    retrieval_scores: list[float],
    reranker_scores: list[float],
    search_latency_ms: float,
) -> np.ndarray:
    """6-dimensional feature vector from search/rerank results only."""
    rs = retrieval_scores or [0.0]
    rr = reranker_scores or [0.0]
    return np.array([
        float(np.mean(rs)),
        float(np.std(rs)) if len(rs) > 1 else 0.0,
        _entropy(rs),
        float(len(rs)),
        float(np.mean(rr)),
        search_latency_ms,
    ], dtype=np.float64)


def full_features(
    retrieval_scores: list[float],
    reranker_scores: list[float],
    search_latency_ms: float,
    answer_length: int,
    citation_count: int,
    hhem_score: float,
    latency_ms: float,
) -> np.ndarray:
    """10-dimensional feature vector including answer-quality metrics."""
    rf = retrieval_features(retrieval_scores, reranker_scores, search_latency_ms)
    af = np.array([
        float(answer_length),
        float(citation_count),
        float(hhem_score),
        float(latency_ms),
    ], dtype=np.float64)
    return np.concatenate([rf, af])


def from_bq_row(row: dict) -> np.ndarray:
    """
    Re-construct a feature vector from a BigQuery telemetry row.
    Rows missing answer metrics fall back to retrieval-only (zero-padded to 10-d).
    """
    n = int(max(1, row.get("chunk_count") or 1))
    mean_r = float(row.get("retrieval_score_mean") or 0)
    mean_k = float(row.get("reranker_score_mean") or 0)

    # Approximate per-chunk lists from stored summary stats
    rs = [mean_r] * n
    rr = [mean_k] * n

    if row.get("latency_ms") is not None:
        return full_features(
            retrieval_scores=rs,
            reranker_scores=rr,
            search_latency_ms=float(row.get("search_latency_ms") or 0),
            answer_length=int(row.get("answer_length") or 0),
            citation_count=int(row.get("citation_count") or 0),
            hhem_score=float(row.get("hhem_score") or 0),
            latency_ms=float(row["latency_ms"]),
        )
    else:
        rf = retrieval_features(
            retrieval_scores=rs,
            reranker_scores=rr,
            search_latency_ms=float(row.get("search_latency_ms") or 0),
        )
        return np.pad(rf, (0, 4))  # zero-pad answer features
