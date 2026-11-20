"""
Hallucination scoring using Vectara's HHEM-2.1 model
(vectara/hallucination_evaluation_model on HuggingFace).

HHEM is a cross-encoder fine-tuned to predict whether a generated answer
is consistent with source evidence.

Used in the eval runner — NOT in the agent container (avoids ~500MB download).
Install deps: pip install 'transformers>=4.51,<5' 'torch>=2.6'
"""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)

MODEL_NAME = "vectara/hallucination_evaluation_model"
MODEL_REVISION = "8e4a2e6e96c708cc76c2344f7e4757df2515292c"


@functools.lru_cache(maxsize=1)
def _model():
    from transformers import AutoModelForSequenceClassification
    logger.info("Loading HHEM model %s (first call only)...", MODEL_NAME)
    return AutoModelForSequenceClassification.from_pretrained(
        MODEL_NAME, revision=MODEL_REVISION, trust_remote_code=True,
    ).eval()


def score_hallucination(evidence: str, answer: str) -> float:
    """
    Returns hallucination probability in [0.0, 1.0].
      0.0 → answer is consistent with the source evidence (not hallucinated)
      1.0 → answer is likely hallucinated

    The model returns a consistency score; invert it for hallucination scoring.
    """
    import torch

    with torch.inference_mode():
        return 1.0 - float(_model().predict([(evidence, answer)])[0])


class HHEMScorer:
    """Score an answer against its source evidence."""
    def score(self, evidence: str, answer: str) -> float:
        return score_hallucination(evidence, answer)
