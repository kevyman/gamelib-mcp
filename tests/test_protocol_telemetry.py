"""ClientEraLogMiddleware: which protocol era and capabilities each client negotiates.

The INFO line is the evidence ADR 0005's open question needs, so these pin that
each era reports its own protocol version, that a repeat tuple stays quiet at
INFO, and that a context with nothing to read degrades to ``?`` instead of
failing the request it observes.
"""

import asyncio
import contextlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from conftest import DEADLOCK_TIMEOUT, ProtocolEraMixin
from fastmcp.server.middleware import MiddlewareContext

from gamelib_mcp import main, protocol_telemetry
from gamelib_mcp.protocol_telemetry import (
    MAX_SEEN_TUPLES,
    ClientEraLogMiddleware,
    summarize_capabilities,
)

LOGGER = "gamelib_mcp.protocol_telemetry"


@contextlib.asynccontextmanager
async def _noop_lifespan(server):
    """Stand-in for lifecycle.lifespan: no refresh, no enrichment, no loop."""
    yield {}


def _registered_middleware() -> ClientEraLogMiddleware:
    found = [m for m in main.mcp.middleware if isinstance(m, ClientEraLogMiddleware)]
    assert len(found) == 1, main.mcp.middleware
    return found[0]


def _era_lines(logs) -> list[str]:
    return [
        record.getMessage()
        for record in logs.records
        if record.levelname == "INFO" and record.getMessage().startswith("client era:")
    ]


class ClientEraWireTests(ProtocolEraMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(patch.object(main.mcp, "_lifespan", _noop_lifespan))
        # Fresh first-seen memory, so an earlier test in this worker that saw
        # the same tuple cannot swallow the INFO line asserted here.
        self._stack.enter_context(patch.object(_registered_middleware(), "_seen", set()))

    async def asyncTearDown(self) -> None:
        self._stack.close()

    async def _list_tools(self, times: int = 1) -> None:
        async with self.open_client(main.mcp) as client:
            for _ in range(times):
                await asyncio.wait_for(client.list_tools_mcp(), timeout=DEADLOCK_TIMEOUT)

    async def test_first_request_logs_the_negotiated_protocol(self):
        with self.assertLogs(LOGGER, level="INFO") as logs:
            await self._list_tools()
        lines = _era_lines(logs)
        self.assertEqual(len(lines), 1, lines)
        self.assertIn(f"protocol={self.protocol_version}", lines[0])
        self.assertIn("name=mcp", lines[0])
        self.assertNotIn("version=?", lines[0])

    async def test_repeat_request_logs_no_second_info_line(self):
        with self.assertLogs(LOGGER, level="DEBUG") as logs:
            await self._list_tools(times=2)
        info = _era_lines(logs)
        self.assertEqual(len(info), 1, info)
        # DEBUG still records every request (discover/initialize + 2 lists).
        debug = [r for r in logs.records if r.levelname == "DEBUG"]
        self.assertGreaterEqual(len(debug), 3)


class LegacyClientEraWireTests(ClientEraWireTests):
    PROTOCOL_MODE = "legacy"


class ClientEraExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_without_request_data_logs_unknowns(self):
        middleware = ClientEraLogMiddleware()
        call_next = AsyncMock(return_value="result")
        for fastmcp_context in (None, SimpleNamespace(request_context=None)):
            with self.subTest(fastmcp_context=fastmcp_context):
                middleware._seen.clear()
                context = MiddlewareContext(
                    message=None, fastmcp_context=fastmcp_context, method="tools/list"
                )
                with self.assertLogs(LOGGER, level="INFO") as logs:
                    result = await middleware.on_request(context, call_next)
                self.assertEqual(result, "result")
                self.assertEqual(
                    _era_lines(logs),
                    ["client era: name=? version=? protocol=? capabilities=?"],
                )

    async def test_unreadable_pieces_degrade_independently(self):
        class Exploding:
            def __getattr__(self, name):
                raise RuntimeError(name)

        rc = SimpleNamespace(protocol_version="2025-11-25", meta=None, session=Exploding())
        context = MiddlewareContext(
            message=None,
            fastmcp_context=SimpleNamespace(request_context=rc),
            method="tools/list",
        )
        self.assertEqual(
            protocol_telemetry.extract_client_era(context),
            ("?", "?", "2025-11-25", "?"),
        )

    def test_capability_summary_expands_elicitation_and_extensions(self):
        self.assertEqual(
            summarize_capabilities(
                {
                    "roots": {"listChanged": True},
                    "elicitation": {"form": {}, "url": {}},
                    "extensions": {"io.modelcontextprotocol/tasks": {}},
                    "sampling": {},
                }
            ),
            "elicitation.form,elicitation.url,ext:io.modelcontextprotocol/tasks,roots,sampling",
        )
        self.assertEqual(summarize_capabilities({}), "none")
        self.assertEqual(summarize_capabilities(None), "?")

    async def test_first_seen_memory_is_bounded(self):
        middleware = ClientEraLogMiddleware()
        middleware._seen = {(str(i), "1", "p", "none") for i in range(MAX_SEEN_TUPLES)}
        context = MiddlewareContext(message=None, fastmcp_context=None, method="tools/list")
        with self.assertLogs(LOGGER, level="DEBUG") as logs:
            await middleware.on_request(context, AsyncMock())
        self.assertEqual(_era_lines(logs), [])
        self.assertEqual(len(middleware._seen), MAX_SEEN_TUPLES)


if __name__ == "__main__":
    unittest.main()
