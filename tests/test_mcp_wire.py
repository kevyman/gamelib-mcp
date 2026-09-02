"""End-to-end MCP wire tests: real server, real protocol, populated arguments.

Every other tool test in this suite awaits an implementation coroutine directly
(``tests/test_tools_*.py``) or introspects the registry in process
(``tests/test_tool_registration.py``). Neither drives an argument dict through
FastMCP's JSON-Schema validation, the pydantic response models, and the
middleware stack, so a Literal coercion regression or a response model that
stopped serializing would pass the whole suite. These tests round-trip a
handful of representative calls through the in-memory MCP transport against
``gamelib_mcp.main.mcp`` itself.

Transport: ``fastmcp.Client(main.mcp)``, which is the real protocol path
(JSON-RPC over in-memory streams, ``call_tool_mcp`` returning the wire
``CallToolResult``). Its transport enters ``server._lifespan_manager()``, so
``gamelib_mcp.lifecycle.lifespan`` — startup library refresh, background
enrichment, the periodic refresh loop — would fire on connect. It is patched
out with a no-op for the duration of each test; nothing here needs it, and
tools resolve the database from ``DATABASE_URL`` per call.

No network: every call either takes an explicit no-fetch flag (``enrich=False``
on get_game_detail, ``with_prices=False`` on get_wishlist) or selects only
offline work (check_library's offline check ids).
"""

import asyncio
import contextlib
import unittest
from contextlib import AsyncExitStack
from unittest.mock import patch

from conftest import (
    DEADLOCK_TIMEOUT,
    ToolDBTestCase,
    add_platform,
    add_rating,
    add_steam_appid,
    seed_game,
    set_tag_affinity,
)
from fastmcp import Client
from mcp.types import CallToolResult

from gamelib_mcp import main


@contextlib.asynccontextmanager
async def _noop_lifespan(server):
    """Stand-in for lifecycle.lifespan: no refresh, no enrichment, no loop."""
    yield {}


class MCPWireTestCase(ToolDBTestCase):
    """A migrated temp DB plus a connected in-memory MCP client."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self._seed()
        self._stack = AsyncExitStack()
        self._stack.enter_context(patch.object(main.mcp, "_lifespan", _noop_lifespan))
        self.client = await self._stack.enter_async_context(Client(main.mcp))

    async def asyncTearDown(self) -> None:
        await self._stack.aclose()
        await super().asyncTearDown()

    async def _seed(self) -> None:
        self.alpha = await seed_game(
            "Wire Probe Alpha", tags=["roguelike", "indie"], hltb_main=12.0
        )
        gp = await add_platform(self.alpha, "steam", playtime_minutes=300)
        await add_steam_appid(gp, 900001)
        await add_rating(self.alpha, "manual", 8.0, 8.0)

        self.beta = await seed_game(
            "Wire Probe Beta", tags=["roguelike", "horror"], hltb_main=6.0
        )
        gp = await add_platform(self.beta, "steam", playtime_minutes=0)
        await add_steam_appid(gp, 900002)

        self.gamma = await seed_game("Wire Probe Gamma", tags=["puzzle"], hltb_main=4.0)
        await add_platform(self.gamma, "epic", playtime_minutes=0)
        # Alpha is owned twice so get_stats(report="platforms") has an overlap.
        await add_platform(self.alpha, "epic", playtime_minutes=0)

        await set_tag_affinity("roguelike", 1.4, 8.5, 2)
        await set_tag_affinity("horror", 0.6, 7.5, 1)
        await set_tag_affinity("puzzle", -0.4, 5.0, 1)

    # ── wire helpers ─────────────────────────────────────────────────────────

    async def call(self, name: str, arguments: dict | None = None) -> CallToolResult:
        """One tool call over the protocol, returning the raw wire result."""
        return await asyncio.wait_for(
            self.client.call_tool_mcp(name, arguments or {}),
            timeout=DEADLOCK_TIMEOUT,
        )

    async def ok(self, name: str, arguments: dict | None = None) -> dict:
        """Call, assert the wire result is a success, return structuredContent."""
        result = await self.call(name, arguments)
        self.assertFalse(
            result.isError,
            f"{name} returned isError with content {result.content!r}",
        )
        self.assertIsInstance(
            result.structuredContent, dict, f"{name} returned no structuredContent"
        )
        return result.structuredContent

    async def failure_text(self, name: str, arguments: dict) -> str:
        """Call, assert the wire result is an error, return its joined text."""
        result = await self.call(name, arguments)
        self.assertTrue(result.isError, f"{name} unexpectedly succeeded: {result!r}")
        return " ".join(
            getattr(block, "text", "") for block in (result.content or [])
        )


class ReadToolWireTests(MCPWireTestCase):
    async def test_search_games(self):
        payload = await self.ok("search_games", {"query": "Wire Probe"})
        self.assertIsInstance(payload["results"], list)
        self.assertIsInstance(payload["total_matches"], int)
        self.assertIsInstance(payload["has_more"], bool)
        self.assertIn(
            "Wire Probe Alpha", [row["name"] for row in payload["results"]]
        )

    async def test_get_game_detail(self):
        payload = await self.ok(
            "get_game_detail", {"game_id": self.alpha, "enrich": False}
        )
        self.assertEqual(payload["game_id"], self.alpha)
        self.assertEqual(payload["name"], "Wire Probe Alpha")
        self.assertIsInstance(payload["platforms"], list)

    async def test_get_library_stats_with_tag_filter(self):
        payload = await self.ok(
            "get_library_stats", {"tags": ["roguelike"], "limit": 5}
        )
        self.assertIsInstance(payload["total_games"], int)
        self.assertIsInstance(payload["results"], list)
        self.assertEqual(payload["total_matches"], 2)
        self.assertIsInstance(payload["spending"], dict)

    async def test_discover_games(self):
        payload = await self.ok(
            "discover_games", {"sort_by": "match", "limit": 5}
        )
        self.assertIsInstance(payload["results"], list)
        self.assertIsInstance(payload["total_matches"], int)
        self.assertIsInstance(payload["has_more"], bool)

    async def test_get_stats_platforms(self):
        payload = await self.ok("get_stats", {"report": "platforms"})
        self.assertEqual(payload["report"], "platforms")
        self.assertIsInstance(payload["by_platform"], list)
        self.assertIsInstance(payload["overlap_count"], int)
        self.assertIsInstance(payload["overlap_truncated"], bool)
        # Alpha is on steam and epic, so the overlap list is non-empty.
        self.assertGreaterEqual(payload["overlap_count"], 1)

    async def test_get_stats_backlog(self):
        payload = await self.ok("get_stats", {"report": "backlog"})
        self.assertEqual(payload["report"], "backlog")
        self.assertIsInstance(payload["playing"], int)
        self.assertIsInstance(payload["completed"], int)
        self.assertIsInstance(payload["unplayed_spend"], dict)

    async def test_get_wishlist(self):
        payload = await self.ok("get_wishlist", {})
        self.assertIsInstance(payload["items"], list)
        self.assertIsInstance(payload["count"], int)
        self.assertIsInstance(payload["total_matches"], int)

    async def test_get_play_history(self):
        payload = await self.ok("get_play_history", {"days": 30})
        self.assertIsInstance(payload["window"], dict)
        self.assertIsInstance(payload["games"], list)
        self.assertIsInstance(payload["by_platform"], dict)
        self.assertIsInstance(payload["total_minutes"], int)

    async def test_query_library_select(self):
        payload = await self.ok(
            "query_library",
            {"sql": "SELECT id, name FROM games ORDER BY id", "row_limit": 10},
        )
        self.assertEqual(payload["columns"], ["id", "name"])
        self.assertIsInstance(payload["rows"], list)
        self.assertEqual(payload["row_count"], 3)
        self.assertFalse(payload["truncated"])

    async def test_get_skill_index(self):
        payload = await self.ok("get_skill", {})
        self.assertIsInstance(payload["skills"], list)
        for entry in payload["skills"]:
            self.assertIsInstance(entry["name"], str)
            self.assertIsInstance(entry["files"], list)

    async def test_check_library_single_check(self):
        payload = await self.ok(
            "check_library", {"checks": ["ownership.orphan"], "limit_per_check": 5}
        )
        self.assertEqual(payload["checks_run"], ["ownership.orphan"])
        self.assertIsInstance(payload["findings"], list)
        self.assertIsInstance(payload["summary"], dict)


class MutationToolWireTests(MCPWireTestCase):
    async def test_rate_game_then_get_ratings(self):
        rated = await self.ok("rate_game", {"game_id": self.beta, "score": 7.5})
        self.assertEqual(rated["game_id"], self.beta)
        self.assertEqual(rated["score"], 7.5)
        self.assertEqual(rated["source"], "manual")

        ratings = await self.ok("get_ratings", {"source": "manual"})
        scored = {row["name"]: row["normalized_score"] for row in ratings["results"]}
        self.assertEqual(scored.get("Wire Probe Beta"), 7.5)

    async def test_update_game_completion_status(self):
        payload = await self.ok(
            "update_game", {"game_id": self.alpha, "completion_status": "playing"}
        )
        self.assertEqual(payload["game_id"], self.alpha)
        self.assertEqual(payload["updated"]["completion_status"], "playing")
        self.assertIn("completion_status", payload["manual_overrides"])

        detail = await self.ok(
            "get_game_detail", {"game_id": self.alpha, "enrich": False}
        )
        self.assertEqual(detail["completion_status"], "playing")

    async def test_add_game_to_platform_wishlist_entry(self):
        payload = await self.ok(
            "add_game_to_platform",
            {"name": "Wire Probe Delta", "platform": "steam", "owned": False},
        )
        self.assertFalse(payload["owned"])
        self.assertIsInstance(payload["wishlist_id"], int)
        self.assertEqual(payload["platform"], "steam")

        wishlist = await self.ok("get_wishlist", {})
        self.assertIn(
            "Wire Probe Delta", [item["name"] for item in wishlist["items"]]
        )


class WireEdgeCaseTests(MCPWireTestCase):
    async def test_unknown_game_id_is_an_error_result(self):
        message = await self.failure_text("get_game_detail", {"game_id": 999999})
        self.assertRegex(message.lower(), r"not found|999999")

    async def test_invalid_literal_is_rejected_at_the_wire(self):
        # The Literal on get_stats(report=...) is enforced by the tool's input
        # schema, so a bad value must come back as a validation error result —
        # not as an unhandled exception from the report dispatch below it.
        message = await self.failure_text("get_stats", {"report": "not_a_report"})
        lowered = message.lower()
        self.assertIn("report", lowered)
        self.assertTrue(
            "valid" in lowered or "literal" in lowered or "input" in lowered,
            f"expected a validation message, got {message!r}",
        )

    async def test_missing_required_argument_is_rejected_at_the_wire(self):
        message = await self.failure_text("get_stats", {})
        self.assertIn("report", message.lower())

    async def test_list_of_dicts_argument_survives_json_coercion(self):
        # items=[...] is the bulk convention (ADR 0004). Its list[dict] shape
        # crosses the wire as JSON, so this pins that each item's fields arrive
        # with usable types rather than as strings or a flattened blob.
        payload = await self.ok(
            "rate_game",
            {
                "items": [
                    {"game_id": self.alpha, "score": 6.0, "review_text": "fine"},
                    {"name": "Wire Probe Beta", "score": 9},
                ]
            },
        )
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["ok"], 2)
        by_name = {row["name"]: row for row in payload["results"]}
        self.assertEqual(
            [row["status"] for row in payload["results"]], ["ok", "ok"]
        )
        self.assertEqual(by_name["Wire Probe Alpha"]["score"], 6.0)
        self.assertEqual(by_name["Wire Probe Alpha"]["review_text"], "fine")
        self.assertEqual(by_name["Wire Probe Beta"]["score"], 9.0)
        self.assertEqual(by_name["Wire Probe Alpha"]["game_id"], self.alpha)


if __name__ == "__main__":
    unittest.main()
