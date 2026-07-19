# Copyright (c) Microsoft. All rights reserved.

import asyncio
import base64
import json
import logging
import math
import os
import time
import uuid

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import QueryType

import iforest_scorer
import log_analytics_sink
from memory_provider import build_memory_provider, memory_enabled_from_env

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are a knowledgeable assistant that answers questions based on the organization's
documents and procedures.

You may also receive separate user/session memory context. Use memory only for
stable user preferences, session continuity, or workflow context. AI Search
document retrieval remains the source of truth for procedure and policy answers.
For any question about organization documents, procedures, equipment, safety,
quality, or maintenance, call search_knowledge_base before answering. Do not
answer procedural questions from memory alone.

**Answering style:**
- Synthesize and summarize information in your own words — do not quote documents verbatim.
- Preserve key procedure terms from retrieved documents for checklist items, requirements, and defect classes.
- When procedures or steps are involved, present them clearly in order.
- If the knowledge base does not contain the answer, say exactly:
  "I could not find information about that in the available documents."
  Do NOT guess, do NOT use general knowledge, and do NOT ask clarifying questions.
- Never hedge with phrases like "I cannot provide information without knowing..." or
  "Could you please clarify..." — if you don't have the answer, say so directly.

**Citations:**
- Always cite your sources at the end of your response.
- Format each citation as a markdown link: [Document Name](url)
- Only cite documents you actually used to answer the question.
- If you could not find relevant documents, do not invent citations.
"""

_credential = DefaultAzureCredential()


def _log_identity() -> None:
    try:
        token = _credential.get_token("https://search.azure.com/.default")
        payload = token.token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        logger.info("=== IDENTITY OID: %s ===", claims.get("oid", "N/A"))
    except Exception as exc:
        logger.warning("Identity log failed: %s", exc)


def search_knowledge_base(query: str) -> str:
    """Search the knowledge base documents using semantic ranking.

    Args:
        query: The search query.

    Returns:
        Relevant document excerpts ranked by semantic relevance, or a message if nothing is found.
    """
    _log_identity()
    request_id = str(uuid.uuid4())

    search_client = SearchClient(
        endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
        index_name=os.environ["AZURE_SEARCH_INDEX"],
        credential=_credential,
    )

    search_start = time.monotonic()
    try:
        # Use Azure AI Search semantic ranking — built into the search tier,
        # no separate API call needed. semantic_configuration_name matches what
        # search_setup.py creates ("my-semantic-config" or "default").
        results = list(search_client.search(
            query,
            top=8,
            query_type=QueryType.SEMANTIC,
            semantic_configuration_name=os.environ.get("AZURE_SEARCH_SEMANTIC_CONFIG", "default"),
            query_caption="extractive",
            query_answer="extractive",
        ))
    except Exception as exc:
        logger.warning("Semantic search failed, falling back to keyword: %s", exc)
        try:
            results = list(search_client.search(query, top=5))
        except Exception as exc2:
            logger.error("Search error: %s", exc2)
            return f"Search unavailable: {exc2}"
    search_latency_ms = (time.monotonic() - search_start) * 1000

    if not results:
        return "No relevant documents found."

    # ── Collect retrieval scores ──────────────────────────────────────────────
    retrieval_scores  = []
    reranker_scores   = []   # @search.reranker_score from semantic ranking
    for r in results:
        retrieval_scores.append(float(r.get("@search.score") or 0.0))
        reranker_scores.append(float(r.get("@search.reranker_score") or 0.0))

    # ── IsolationForest anomaly detection ─────────────────────────────────────
    anomaly_score, is_anomaly = iforest_scorer.score(
        retrieval_scores=retrieval_scores,
        reranker_scores=reranker_scores,
        search_latency_ms=search_latency_ms,
    )
    if is_anomaly:
        logger.warning(
            "Anomalous retrieval [request_id=%s]: iforest=%.4f chunks=%d latency=%.0fms",
            request_id, anomaly_score, len(retrieval_scores), search_latency_ms,
        )

    # ── Telemetry (non-blocking) ──────────────────────────────────────────────
    n      = len(retrieval_scores)
    mean_r = sum(retrieval_scores) / n
    std_r  = math.sqrt(sum((s - mean_r) ** 2 for s in retrieval_scores) / n)
    total  = sum(retrieval_scores) or 1e-10
    ent_r  = -sum((s / total) * math.log(s / total + 1e-10) for s in retrieval_scores)

    log_analytics_sink.log_async(
        request_id=request_id,
        query=query,
        source="runtime",
        search_latency_ms=search_latency_ms,
        retrieval_score_mean=mean_r,
        retrieval_score_std=std_r,
        retrieval_score_entropy=ent_r,
        chunk_count=n,
        reranker_score_mean=sum(reranker_scores) / len(reranker_scores) if reranker_scores else 0.0,
        anomaly_score=anomaly_score,
        is_anomaly=is_anomaly,
    )

    # ── Build excerpts from top-5 semantically ranked results ─────────────────
    excerpts = []
    for r in results[:5]:
        # Prefer semantic captions (extractive highlights) over raw content
        captions = r.get("@search.captions") or []
        text = " ".join(c.text for c in captions if c.text) if captions else ""
        if not text:
            text = (
                r.get("content") or r.get("snippet") or r.get("chunk") or r.get("text")
                or next((v for v in r.values() if isinstance(v, str) and len(v) > 50), None)
                or str(r)
            )
        source_url  = r.get("source_url") or r.get("metadata_storage_path") or ""
        source_name = r.get("metadata_storage_name") or ""
        if source_url and source_name:
            header = f"[{source_name}]({source_url})"
        elif source_name:
            header = f"[{source_name}]"
        else:
            header = ""
        excerpts.append(f"{header}\n{text}" if header else text)

    return "\n\n---\n\n".join(excerpts)


async def main() -> None:
    async_credential = AsyncDefaultAzureCredential()
    client_kwargs = {
        "project_endpoint": os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        "model": os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        "credential": async_credential,
    }
    if memory_enabled_from_env():
        client_kwargs["allow_preview"] = True

    client = FoundryChatClient(**client_kwargs)
    memory_provider = build_memory_provider(client.project_client)
    context_providers = [memory_provider] if memory_provider else []

    agent_kwargs = {
        "client": client,
        "instructions": INSTRUCTIONS,
        "tools": [search_knowledge_base],
        "default_options": {"store": False},
    }
    if context_providers:
        agent_kwargs["context_providers"] = context_providers

    agent = Agent(**agent_kwargs)

    server = ResponsesHostServer(agent)
    await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
