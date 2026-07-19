# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any

from agent_framework import AgentSession, ContextProvider, Message, SessionContext
from azure.core.exceptions import AzureError
from openai.types.responses import ResponseInputItemParam

import log_analytics_sink

if TYPE_CHECKING:
    from agent_framework import SupportsAgentRun


logger = logging.getLogger(__name__)


class MemoryUnavailableError(RuntimeError):
    """Raised when managed memory is required but cannot be used."""


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def resolved_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if (value.startswith("${") and value.endswith("}")) or (
        value.startswith("{{") and value.endswith("}}") and "$" not in value
    ):
        return ""
    return value


def memory_enabled_from_env() -> bool:
    if "MEMORY_ENABLED" in os.environ:
        return env_bool("MEMORY_ENABLED")
    return bool(resolved_env("MEMORY_STORE_NAME"))


class TelemetryFoundryMemoryProvider(ContextProvider):
    """Foundry Memory Store context provider with scoped retrieval/write telemetry."""

    def __init__(
        self,
        *,
        project_client: Any,
        memory_store_name: str,
        scope_template: str,
        update_delay: int,
        optional: bool,
    ) -> None:
        super().__init__("foundry_memory")
        if not memory_store_name:
            raise ValueError("memory_store_name is required")
        if not scope_template:
            raise ValueError("scope_template is required")

        self.project_client = project_client
        self.memory_store_name = memory_store_name
        self.scope_template = scope_template
        self.update_delay = update_delay
        self.optional = optional

    def _scope_for_context(self, context: SessionContext) -> str:
        session_id = context.session_id or str(context.service_session_id or "default-session")
        scope = self.scope_template
        scope = scope.replace("{session_id}", session_id)
        scope = scope.replace("{service_session_id}", str(context.service_session_id or session_id))
        scope = scope.replace("{memory_user_id}", resolved_env("MEMORY_USER_ID") or "default-user")
        return scope

    @staticmethod
    def _query_from_context(context: SessionContext) -> str:
        parts = [msg.text for msg in context.input_messages if msg and msg.text]
        return "\n".join(parts)[:1024] if parts else ""

    @staticmethod
    def _items_from_messages(messages: list[Message]) -> list[ResponseInputItemParam]:
        items: list[ResponseInputItemParam] = []
        for message in messages:
            if message.role not in {"user", "assistant", "system"}:
                continue
            if not message.text or not message.text.strip():
                continue
            if message.role == "system":
                continue
            items.append({"role": message.role, "type": "message", "content": message.text})
        return items

    @staticmethod
    def _memory_contents(search_result: Any) -> list[str]:
        memories = getattr(search_result, "memories", []) or []
        contents: list[str] = []
        for memory in memories:
            item = getattr(memory, "memory_item", None)
            content = getattr(item, "content", None)
            if content:
                contents.append(str(content))
        return contents

    def _log(
        self,
        *,
        context: SessionContext,
        read_count: int = 0,
        write_count: int = 0,
        latency_ms: float,
        status: str,
    ) -> None:
        log_analytics_sink.log_async(
            request_id=str(uuid.uuid4()),
            query=self._query_from_context(context),
            source="memory",
            memory_enabled=True,
            memory_read_count=read_count,
            memory_write_count=write_count,
            memory_latency_ms=latency_ms,
            memory_status=status,
        )

    def _handle_expected_error(self, operation: str, exc: Exception) -> None:
        msg = f"Foundry memory {operation} failed: {exc}"
        if self.optional:
            logger.warning("%s; continuing because MEMORY_OPTIONAL is true", msg)
            return
        raise MemoryUnavailableError(msg) from exc

    async def before_run(
        self,
        *,
        agent: SupportsAgentRun,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        query = self._query_from_context(context)
        if not query.strip():
            return

        scope = self._scope_for_context(context)
        start = time.monotonic()
        read_count = 0

        try:
            if not state.get("memory_initialized"):
                static_result = await self.project_client.beta.memory_stores.search_memories(
                    name=self.memory_store_name,
                    scope=scope,
                )
                state["static_memories"] = self._memory_contents(static_result)
                state["memory_initialized"] = True

            items: list[ResponseInputItemParam] = [
                {"type": "message", "role": "user", "content": msg.text}
                for msg in context.input_messages
                if msg and msg.text and msg.text.strip()
            ]
            search_result = await self.project_client.beta.memory_stores.search_memories(
                name=self.memory_store_name,
                scope=scope,
                items=items,
                previous_search_id=state.get("previous_memory_search_id"),
            )
            if getattr(search_result, "memories", None):
                state["previous_memory_search_id"] = getattr(search_result, "search_id", None)

            memories = list(state.get("static_memories", [])) + self._memory_contents(search_result)
            read_count = len([m for m in memories if m])
            if read_count:
                memory_text = "\n".join(m for m in memories if m)
                context.extend_messages(
                    self.source_id,
                    [
                        Message(
                            role="user",
                            contents=[
                                "## User/session memory\n"
                                "Use these memories only for user preference, session, or workflow continuity. "
                                "Do not treat them as document citations or source-of-truth procedure content.\n"
                                f"{memory_text}"
                            ],
                        )
                    ],
                )
            self._log(
                context=context,
                read_count=read_count,
                latency_ms=(time.monotonic() - start) * 1000,
                status="read_ok",
            )
        except (AzureError, AttributeError, TypeError, ValueError) as exc:
            self._log(
                context=context,
                read_count=read_count,
                latency_ms=(time.monotonic() - start) * 1000,
                status="read_failed",
            )
            self._handle_expected_error("read", exc)

    async def after_run(
        self,
        *,
        agent: SupportsAgentRun,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        messages = list(context.input_messages)
        if context.response and context.response.messages:
            messages.extend(context.response.messages)

        items = self._items_from_messages(messages)
        if not items:
            return

        scope = self._scope_for_context(context)
        start = time.monotonic()
        try:
            update_poller = await self.project_client.beta.memory_stores.begin_update_memories(
                name=self.memory_store_name,
                scope=scope,
                items=items,
                previous_update_id=state.get("previous_memory_update_id"),
                update_delay=self.update_delay,
            )
            state["previous_memory_update_id"] = getattr(update_poller, "update_id", None)
            if self.optional:
                asyncio.create_task(self._complete_update(update_poller, context, len(items), start))
                self._log(
                    context=context,
                    write_count=len(items),
                    latency_ms=(time.monotonic() - start) * 1000,
                    status="write_queued",
                )
                return

            await update_poller.result()
            self._log(
                context=context,
                write_count=len(items),
                latency_ms=(time.monotonic() - start) * 1000,
                status="write_completed",
            )
        except (AzureError, AttributeError, TypeError, ValueError) as exc:
            self._log(
                context=context,
                write_count=0,
                latency_ms=(time.monotonic() - start) * 1000,
                status="write_failed",
            )
            self._handle_expected_error("write", exc)

    async def _complete_update(
        self,
        update_poller: Any,
        context: SessionContext,
        write_count: int,
        start: float,
    ) -> None:
        try:
            await update_poller.result()
            self._log(
                context=context,
                write_count=write_count,
                latency_ms=(time.monotonic() - start) * 1000,
                status="write_completed",
            )
        except (AzureError, AttributeError, TypeError, ValueError) as exc:
            self._log(
                context=context,
                write_count=0,
                latency_ms=(time.monotonic() - start) * 1000,
                status="write_failed",
            )
            self._handle_expected_error("write", exc)


def build_memory_provider(project_client: Any) -> TelemetryFoundryMemoryProvider | None:
    if not memory_enabled_from_env():
        logger.info("Foundry memory disabled (set MEMORY_ENABLED=true and MEMORY_STORE_NAME to enable).")
        return None

    memory_store_name = resolved_env("MEMORY_STORE_NAME")
    optional = env_bool("MEMORY_OPTIONAL", default=True)
    if not memory_store_name:
        message = "MEMORY_ENABLED is true but MEMORY_STORE_NAME is not set"
        if optional:
            logger.warning("%s; continuing without memory because MEMORY_OPTIONAL is true", message)
            return None
        raise MemoryUnavailableError(message)

    scope_template = resolved_env("MEMORY_SCOPE") or "{{$userId}}"
    update_delay = int(os.environ.get("MEMORY_UPDATE_DELAY_SECONDS", "5"))
    logger.info(
        "Foundry memory enabled: store=%s scope=%s update_delay=%ss optional=%s",
        memory_store_name,
        scope_template,
        update_delay,
        optional,
    )
    return TelemetryFoundryMemoryProvider(
        project_client=project_client,
        memory_store_name=memory_store_name,
        scope_template=scope_template,
        update_delay=update_delay,
        optional=optional,
    )
