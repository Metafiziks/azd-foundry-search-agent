#!/usr/bin/env python3
"""Create the Azure AI Foundry Memory Store and prove it works before memory is enabled.

Creating a store succeeds even when the project identity cannot call the model
deployments, so a store that merely exists proves nothing. This script:

1. creates (or reuses) the store;
2. runs an embedding-backed *search* and a chat+embedding-backed *write* in a
   throwaway scope, retrying HTTP 401/403 while freshly granted RBAC roles propagate
   (typically 2-10 minutes); and
3. deletes the throwaway scope.

Anything other than a propagation-shaped 401/403 fails immediately.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

from azure.ai.projects.aio import AIProjectClient
from azure.ai.projects.models import MemoryStoreDefaultDefinition, MemoryStoreDefaultOptions
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential

RBAC_RETRY_SECONDS = int(os.environ.get("MEMORY_PROBE_RETRY_SECONDS", "900"))
RBAC_RETRY_INTERVAL = 20
WRITE_TIMEOUT_SECONDS = int(os.environ.get("MEMORY_PROBE_WRITE_TIMEOUT_SECONDS", "240"))


async def with_rbac_retry(label: str, call):
    """Run ``call`` and retry only while the failure looks like RBAC propagation."""
    deadline = time.monotonic() + RBAC_RETRY_SECONDS
    attempt = 0
    while True:
        attempt += 1
        try:
            return await call()
        except HttpResponseError as exc:
            if exc.status_code not in (401, 403) or time.monotonic() >= deadline:
                raise SystemExit(
                    f"Memory Store {label} failed (HTTP {exc.status_code}) after {attempt} attempt(s): {exc.message}\n"
                    "Memory Store calls run as the CALLER, so the identity running this script needs the "
                    "'Foundry User' data-plane role on the Foundry account ('Owner'/'Contributor' do not include it). "
                    "At runtime the caller is the agent's hosting identity (granted in postdeploy). "
                    "New role assignments take ~4 minutes to take effect."
                ) from None
            print(f"  {label}: HTTP {exc.status_code}, waiting for RBAC propagation (attempt {attempt})...", flush=True)
            await asyncio.sleep(RBAC_RETRY_INTERVAL)


async def main() -> None:
    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    memory_store_name = os.environ["MEMORY_STORE_NAME"]
    chat_model = os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"]
    embedding_model = os.environ["AZURE_AI_EMBEDDING_MODEL_DEPLOYMENT_NAME"]

    async with (
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential, allow_preview=True) as project,
    ):
        stores = project.beta.memory_stores
        try:
            existing = await stores.get(name=memory_store_name)
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
            created = await with_rbac_retry(
                "create",
                lambda: stores.create(
                    name=memory_store_name,
                    description="Memory store for the Azure Foundry Search Agent template",
                    definition=definition,
                ),
            )
            print(f"Created memory store '{created.name}' (id={created.id}).")

        scope = f"deployment-check-{uuid.uuid4().hex}"

        await with_rbac_retry(
            "embedding-backed search",
            lambda: stores.search_memories(
                name=memory_store_name,
                scope=scope,
                items=[{"type": "message", "role": "user", "content": "Memory Store readiness probe"}],
            ),
        )
        print(f"Verified embedding-backed search for memory store '{memory_store_name}'.")

        async def write_probe():
            poller = await stores.begin_update_memories(
                name=memory_store_name,
                scope=scope,
                update_delay=0,
                items=[{"type": "message", "role": "user", "content": "My readiness probe prefers short bullet lists."}],
            )
            await asyncio.wait_for(poller.result(), timeout=WRITE_TIMEOUT_SECONDS)

        try:
            await with_rbac_retry("chat+embedding-backed write", write_probe)
        except TimeoutError:
            raise SystemExit(f"Memory Store write did not finish within {WRITE_TIMEOUT_SECONDS}s.") from None
        print(f"Verified chat+embedding-backed write for memory store '{memory_store_name}'.")

        try:
            await stores.delete_scope(name=memory_store_name, scope=scope)
        except Exception as exc:  # cleanup only; the probe already passed
            print(f"  (could not delete probe scope: {type(exc).__name__})")


if __name__ == "__main__":
    asyncio.run(main())
