#!/usr/bin/env python3
"""Create or verify the Azure AI Foundry Memory Store for the hosted agent."""

from __future__ import annotations

import asyncio
import os
import uuid

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import MemoryStoreDefaultDefinition, MemoryStoreDefaultOptions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential


async def main() -> None:
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    memory_store_name = os.environ["MEMORY_STORE_NAME"]
    chat_model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    embedding_model = os.environ["AZURE_AI_EMBEDDING_MODEL_DEPLOYMENT_NAME"]

    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
    ):
        try:
            existing = await project.beta.memory_stores.get(name=memory_store_name)
            print(f"Memory store '{existing.name}' already exists (id={existing.id}); leaving as-is.")
        except ResourceNotFoundError:
            definition = MemoryStoreDefaultDefinition(
                chat_model=chat_model,
                embedding_model=embedding_model,
                options=MemoryStoreDefaultOptions(
                    chat_summary_enabled=False,
                    user_profile_enabled=True,
                    procedural_memory_enabled=False,
                    default_ttl_seconds=int(os.environ.get("MEMORY_DEFAULT_TTL_SECONDS", "2592000")),
                    user_profile_details=(
                        "Remember stable manufacturing workflow preferences and continuity context. "
                        "Avoid irrelevant or sensitive data such as credentials, financial data, "
                        "precise location, personal demographics, or secrets."
                    ),
                ),
            )
            created = await project.beta.memory_stores.create(
                name=memory_store_name,
                description="Memory store for the Azure Foundry Search Agent template",
                definition=definition,
            )
            print(f"Created memory store '{created.name}' (id={created.id}).")

        try:
            await project.beta.memory_stores.search_memories(
                name=memory_store_name,
                scope=f"deployment-check-{uuid.uuid4().hex}",
                items=[{"type": "message", "role": "user", "content": "Memory Store readiness probe"}],
            )
        except HttpResponseError as exc:
            raise SystemExit(
                f"Memory Store embedding-backed search failed (HTTP {exc.status_code}): {exc.message}"
            ) from None
        print(f"Verified embedding-backed search for memory store '{memory_store_name}'.")


if __name__ == "__main__":
    asyncio.run(main())
