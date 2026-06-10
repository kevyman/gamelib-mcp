"""Freezes the MCP tool surface: names + parameter schemas.

This is the compatibility tripwire for the restructuring work — any later step
that changes a tool name, drops a parameter, or alters required-ness will fail
here. Descriptions are intentionally NOT asserted (they may be improved).
"""

import unittest

from gamelib_mcp import main


EXPECTED_TOOLS = {
    "search_games": {"params": {"query", "limit", "platform"}, "required": {"query"}},
    "search_games_batch": {"params": {"queries", "limit_per_query"}, "required": {"queries"}},
    "get_library_stats": {
        "params": {
            "filter",
            "max_hltb_hours",
            "min_metacritic",
            "protondb_tier",
            "sort_by",
            "limit",
            "platform",
        },
        "required": set(),
    },
    "get_game_detail": {"params": {"name", "appid", "game_id"}, "required": set()},
    "find_games_by_vibe": {
        "params": {"vibe", "max_hltb_hours", "unplayed_only", "protondb_min_tier", "limit"},
        "required": {"vibe"},
    },
    "get_recommendations": {
        "params": {"max_hltb_hours", "unplayed_only", "limit"},
        "required": set(),
    },
    "get_taste_profile": {"params": set(), "required": set()},
    "get_ratings": {"params": {"source", "min_score", "sort_by", "limit"}, "required": set()},
    "sync_ratings": {"params": set(), "required": set()},
    "get_backlog_stats": {"params": set(), "required": set()},
    "refresh_library": {"params": {"platforms"}, "required": set()},
    "get_integration_status": {"params": {"platforms", "verbose"}, "required": set()},
    "detect_farmed_games": {
        "params": {"dry_run", "threshold_hours", "min_games_per_day"},
        "required": set(),
    },
    "get_platform_breakdown": {"params": set(), "required": set()},
    "sync_platform": {"params": {"platform"}, "required": {"platform"}},
    "set_hardware_preference": {"params": {"platforms"}, "required": {"platforms"}},
    "add_game_to_platform": {
        "params": {"name", "platform", "identifier_type", "identifier_value", "playtime_minutes"},
        "required": {"name", "platform"},
    },
    "set_nintendo_session": {"params": {"cookies"}, "required": {"cookies"}},
}


class ToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def _tools(self):
        tools = await main.mcp.list_tools()
        return {t.name: t for t in tools}

    async def test_exact_tool_names(self):
        tools = await self._tools()
        self.assertEqual(set(tools), set(EXPECTED_TOOLS))

    async def test_tool_count_is_18(self):
        tools = await self._tools()
        self.assertEqual(len(tools), 18)

    async def test_parameter_names_and_required(self):
        tools = await self._tools()
        for name, expected in EXPECTED_TOOLS.items():
            with self.subTest(tool=name):
                schema = tools[name].parameters
                props = set(schema.get("properties", {}))
                required = set(schema.get("required", []))
                self.assertEqual(props, expected["params"])
                self.assertEqual(required, expected["required"])


if __name__ == "__main__":
    unittest.main()
