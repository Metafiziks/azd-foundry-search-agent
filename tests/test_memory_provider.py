import asyncio
import sys
import time
import types
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


def _stub_unavailable_modules() -> None:
    """The hosted-agent SDKs are only installed in the agent image; stub what the provider imports."""
    try:
        import agent_framework  # noqa: F401
    except ImportError:
        stub = types.ModuleType("agent_framework")
        stub.AgentSession = stub.SessionContext = stub.Message = object
        stub.ContextProvider = type("ContextProvider", (), {"__init__": lambda self, source_id: setattr(self, "source_id", source_id)})
        sys.modules["agent_framework"] = stub
    try:
        import openai.types.responses  # noqa: F401
    except ImportError:
        for name in ("openai", "openai.types", "openai.types.responses"):
            sys.modules.setdefault(name, types.ModuleType(name))
        sys.modules["openai.types.responses"].ResponseInputItemParam = dict


_stub_unavailable_modules()

import memory_provider  # noqa: E402
from azure.core.exceptions import HttpResponseError  # noqa: E402


class SlowPoller:
    update_id = "update-1"

    def __init__(self, seconds: float, error: Exception | None = None) -> None:
        self.seconds, self.error = seconds, error

    async def result(self):
        await asyncio.sleep(self.seconds)
        if self.error:
            raise self.error


def make_provider(poller: SlowPoller, optional: bool) -> memory_provider.TelemetryFoundryMemoryProvider:
    stores = SimpleNamespace(begin_update_memories=AsyncMock(return_value=poller), search_memories=AsyncMock())
    client = SimpleNamespace(beta=SimpleNamespace(memory_stores=stores))
    return memory_provider.TelemetryFoundryMemoryProvider(
        project_client=client, memory_store_name="store", scope_template="user", update_delay=0, optional=optional
    )


def make_context():
    message = SimpleNamespace(role="user", text="I prefer short bullets")
    reply = SimpleNamespace(role="assistant", text="Noted")
    return SimpleNamespace(
        session_id="s", service_session_id=None, input_messages=[message], response=SimpleNamespace(messages=[reply])
    )


class MemoryWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_does_not_block_the_response_in_either_mode(self):
        for optional in (True, False):
            with self.subTest(optional=optional), patch.object(memory_provider.log_analytics_sink, "log_async"):
                provider = make_provider(SlowPoller(5), optional)
                start = time.monotonic()
                await provider.after_run(agent=None, session=None, context=make_context(), state={})
                self.assertLess(time.monotonic() - start, 1.0)
                for task in list(provider._background_writes):
                    task.cancel()

    async def test_strict_mode_fails_the_next_request_after_a_background_write_error(self):
        error = HttpResponseError(message="forbidden")
        with patch.object(memory_provider.log_analytics_sink, "log_async"):
            provider = make_provider(SlowPoller(0, error), optional=False)
            await provider.after_run(agent=None, session=None, context=make_context(), state={})
            await asyncio.gather(*provider._background_writes)
            with self.assertRaises(memory_provider.MemoryUnavailableError):
                await provider.before_run(agent=None, session=None, context=make_context(), state={})

    async def test_optional_mode_swallows_a_background_write_error(self):
        provider = make_provider(SlowPoller(0, HttpResponseError(message="forbidden")), optional=True)
        provider.project_client.beta.memory_stores.search_memories = AsyncMock(return_value=SimpleNamespace(memories=[]))
        with patch.object(memory_provider.log_analytics_sink, "log_async"):
            await provider.after_run(agent=None, session=None, context=make_context(), state={})
            await asyncio.gather(*provider._background_writes)
            await provider.before_run(agent=None, session=None, context=make_context(), state={})


if __name__ == "__main__":
    unittest.main()
