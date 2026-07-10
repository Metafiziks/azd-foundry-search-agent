# Copyright (c) Microsoft. All rights reserved.

import base64
import json
import logging
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

INSTRUCTIONS = """
You are a knowledgeable assistant that answers questions based on the organization's
documents and procedures.

Answer questions clearly and directly using only information retrieved from the
knowledge base. When procedures or steps are involved, list them in order.
If the knowledge base does not contain the answer, say so — never guess or
use general knowledge.
"""

_credential = DefaultAzureCredential()


def _log_identity() -> None:
    """Log the managed identity OID on every call so it always appears in recent logs."""
    try:
        token = _credential.get_token("https://search.azure.com/.default")
        payload = token.token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        logger.info("=== IDENTITY OID: %s ===", claims.get("oid", "N/A"))
    except Exception as exc:
        logger.warning("Identity log failed: %s", exc)


def search_knowledge_base(query: str) -> str:
    """Search the knowledge base documents.

    Args:
        query: The search query.

    Returns:
        Relevant document excerpts, or a message if nothing is found.
    """
    _log_identity()

    search_client = SearchClient(
        endpoint=os.environ["AZURE_SEARCH_ENDPOINT"],
        index_name=os.environ["AZURE_SEARCH_INDEX"],
        credential=_credential,
    )
    try:
        results = list(search_client.search(query, top=5))
    except Exception as exc:
        logger.error("Search error: %s", exc)
        return f"Search unavailable: {exc}"

    if not results:
        return "No relevant documents found."

    excerpts = []
    for r in results:
        text = (
            r.get("content")
            or r.get("snippet")
            or r.get("chunk")
            or r.get("text")
            or next((v for v in r.values() if isinstance(v, str) and len(v) > 50), None)
            or str(r)
        )
        source = r.get("metadata_storage_name") or r.get("blob_url") or r.get("source") or ""
        excerpts.append(f"[{source}]\n{text}" if source else text)

    return "\n\n---\n\n".join(excerpts)


def main():
    client = FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
        credential=_credential,
    )

    agent = Agent(
        client=client,
        instructions=INSTRUCTIONS,
        tools=[search_knowledge_base],
        default_options={"store": False},
    )

    server = ResponsesHostServer(agent)
    server.run()


if __name__ == "__main__":
    main()
