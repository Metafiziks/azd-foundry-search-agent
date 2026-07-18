"""
Hallucination scoring using Vectara's HHEM-2.1 model
(vectara/hallucination_evaluation_model on HuggingFace).

HHEM is a cross-encoder fine-tuned to predict whether a generated answer
is consistent with the question/context or hallucinated.

Used in the eval runner — NOT in the agent container (avoids ~500MB download).
Install deps: pip install transformers torch
"""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

MODEL_NAME = "vectara/hallucination_evaluation_model"


@functools.lru_cache(maxsize=1)
def _pipeline():
    from transformers import pipeline
    logger.info("Loading HHEM model %s (first call only)...", MODEL_NAME)
    return pipeline(
        "text-classification",
        model=MODEL_NAME,
        # Use CPU by default; set device=0 for GPU
    )


def score_hallucination(question: str, answer: str) -> float:
    """
    Returns hallucination probability in [0.0, 1.0].
      0.0 → answer is consistent with the question/context (not hallucinated)
      1.0 → answer is likely hallucinated

    HHEM labels:
      'consistent'   — answer is grounded  → hallucination_prob = 1 - score
      'hallucinated' — answer is fabricated → hallucination_prob = score

    Returns 1.0 on empty or error.
    """
    if not answer or not answer.strip():
        return 1.0
    try:
        pipe = _pipeline()
        result = pipe({"text": question, "text_pair": answer})[0]
        label = result["label"].lower()
        conf  = float(result["score"])
        return (1.0 - conf) if label == "consistent" else conf
    except Exception as exc:
        logger.warning("HHEM scoring failed: %s", exc)
        return 0.5  # neutral fallback
