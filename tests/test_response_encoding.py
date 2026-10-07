"""StructuredOnlyMiddleware: drop the duplicate text block, keep everything else.

The saving is real (48% of response bytes) but the failure mode is severe — a
client that reads content blocks would see empty results — so these pin the
narrow cases the middleware must never get wrong: a result with no structured
content, a non-text block, an error result, and the env escape hatch.
"""

import asyncio
import contextlib
import json
import os
import unittest
from unittest.mock import patch

from conftest import DEADLOCK_TIMEOUT, ProtocolEraMixin
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from mcp.types import CallToolResult, TextContent

from gamelib_mcp.response_encoding import (
    ENV_VAR,
    StructuredOnlyMiddleware,
    duplicate_text_content_enabled,
)


def _server() -> FastMCP:
    mcp = FastMCP(name="probe", middleware=[StructuredOnlyMiddleware()])

    @mcp.tool()
    async def structured() -> dict:
        """Normal tool: FastMCP fills both channels."""
        return {"a": 1, "b": [1, 2, 3]}

    @mcp.tool()
    async def text_only() -> str:
        """Returns a bare string — no structured content to fall back on."""
        return "just text"

    @mcp.tool()
    async def mixed() -> dict:
        """Structured content alongside a non-text block."""
        from mcp.types import ImageContent

        return ToolResult(
            content=[
                TextContent(type="text", text='{"a": 1}'),
                ImageContent(type="image", data="aGk=", mime_type="image/png"),
            ],
            structured_content={"a": 1},
        )

    @mcp.tool()
    async def boom() -> dict:
        """Raises, so the client gets an error result."""
        raise ValueError("kaboom")

    return mcp


@contextlib.asynccontextmanager
async def _noop_lifespan(server):
    """Stand-in for lifecycle.lifespan when connecting to the real app."""
    yield {}


class WireMixin(ProtocolEraMixin):
    async def _call(self, mcp: FastMCP, name: str) -> CallToolResult:
        """One tools/call over the in-memory protocol, as the client sees it."""
        with patch.object(mcp, "_lifespan", _noop_lifespan):
            async with self.open_client(mcp) as client:
                return await asyncio.wait_for(
                    client.call_tool_mcp(name, {}), timeout=DEADLOCK_TIMEOUT
                )

    async def _wire(self, mcp: FastMCP, name: str):
        """(content blocks, structured content) of a successful call."""
        result = await self._call(mcp, name)
        self.assertFalse(result.is_error, f"{name} failed: {result.content!r}")
        return (result.content or []), result.structured_content


class StructuredOnlyMiddlewareTests(WireMixin, unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_text_block_is_dropped(self):
        content, structured = await self._wire(_server(), "structured")
        self.assertEqual(content, [])
        self.assertEqual(structured, {"a": 1, "b": [1, 2, 3]})

    async def test_scalar_return_still_delivers_its_payload(self):
        # FastMCP wraps a bare scalar as {"result": ...} in structured content,
        # so dropping its text block still leaves the value reachable. Verified
        # against a middleware-free server: content=1 block, structured=
        # {"result": "just text"}. None of this server's 30 tools return a
        # scalar, but a future one must not silently lose its payload.
        content, structured = await self._wire(_server(), "text_only")
        self.assertEqual(content, [])
        self.assertEqual(structured, {"result": "just text"})

    async def test_non_text_blocks_survive(self):
        content, structured = await self._wire(_server(), "mixed")
        self.assertEqual([c.type for c in content], ["image"])
        self.assertEqual(structured, {"a": 1})

    async def test_errors_propagate_untouched(self):
        # A failing tool RAISES through call_next rather than returning a
        # ToolResult, so the middleware never post-processes it — the error
        # result keeps its text block and the message reaches the model intact.
        # This asserts that property at the wire rather than assuming it.
        result = await self._call(_server(), "boom")
        self.assertTrue(result.is_error)
        text = " ".join(getattr(block, "text", "") for block in result.content or [])
        self.assertIn("kaboom", text)

    async def test_payload_is_unchanged_apart_from_the_dropped_block(self):
        plain = FastMCP(name="plain")

        @plain.tool()
        async def structured() -> dict:
            """Same tool, no middleware."""
            return {"a": 1, "b": [1, 2, 3]}

        _, baseline = await self._wire(plain, "structured")
        _, stripped = await self._wire(_server(), "structured")
        self.assertEqual(baseline, stripped)

    async def test_output_schema_is_unaffected(self):
        tools = {t.name: t for t in await _server().list_tools()}
        self.assertTrue(tools["structured"].output_schema)


class LegacyStructuredOnlyMiddlewareTests(StructuredOnlyMiddlewareTests):
    PROTOCOL_MODE = "legacy"


class EscapeHatchTests(unittest.TestCase):
    def test_env_var_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ENV_VAR, None)
            self.assertFalse(duplicate_text_content_enabled())

    def test_env_var_accepts_common_truthy_spellings(self):
        for value in ("1", "true", "TRUE", "yes", "on"):
            with self.subTest(value=value), patch.dict(os.environ, {ENV_VAR: value}):
                self.assertTrue(duplicate_text_content_enabled())

    def test_env_var_ignores_other_values(self):
        for value in ("0", "false", "", "no"):
            with self.subTest(value=value), patch.dict(os.environ, {ENV_VAR: value}):
                self.assertFalse(duplicate_text_content_enabled())


class ServerWiringTests(WireMixin, unittest.IsolatedAsyncioTestCase):
    async def test_the_real_server_strips_duplicates(self):
        from gamelib_mcp import main

        content, structured = await self._wire(main.mcp, "get_sync_status")
        self.assertEqual(content, [])
        self.assertIsNotNone(structured)
        # and the payload is still complete
        self.assertIn("status", structured)

    async def test_widget_payload_still_reaches_structured_content(self):
        # apps.py's game-cards bridge reads result.structuredContent first; if
        # this ever returned None the widget would fall back to a text block
        # that is no longer there.
        from gamelib_mcp import main

        _, structured = await self._wire(main.mcp, "get_sync_status")
        self.assertIsInstance(json.loads(json.dumps(structured)), dict)


class LegacyServerWiringTests(ServerWiringTests):
    PROTOCOL_MODE = "legacy"

if __name__ == "__main__":
    unittest.main()
