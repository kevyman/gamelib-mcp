"""Freezes the MCP tool surface: names + parameter schemas.

This is the compatibility tripwire for the restructuring work — any later step
that changes a tool name, drops a parameter, or alters required-ness will fail
here. Description assertions stay limited to known regressions.
"""

import unittest

from gamelib_mcp import main


EXPECTED_TOOLS = {
    "search_games": {
        "params": {"query", "limit", "offset", "platform", "response_format"},
        "required": {"query"},
    },
    "search_games_batch": {"params": {"queries", "limit_per_query"}, "required": {"queries"}},
    "get_library_stats": {
        "params": {
            "filter",
            "max_hltb_hours",
            "min_metacritic",
            "min_opencritic",
            "protondb_tier",
            "sort_by",
            "limit",
            "offset",
            "platform",
            "response_format",
        },
        "required": set(),
    },
    "get_game_detail": {"params": {"name", "appid", "game_id"}, "required": set()},
    "find_games_by_vibe": {
        "params": {
            "vibe",
            "max_hltb_hours",
            "unplayed_only",
            "protondb_min_tier",
            "limit",
            "offset",
            "response_format",
        },
        "required": {"vibe"},
    },
    "get_recommendations": {
        "params": {"max_hltb_hours", "unplayed_only", "limit", "offset", "response_format"},
        "required": set(),
    },
    "get_taste_profile": {"params": set(), "required": set()},
    "get_ratings": {
        "params": {"source", "min_score", "sort_by", "limit", "offset", "response_format"},
        "required": set(),
    },
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

EXPECTED_ANNOTATIONS = {
    "search_games": {"readOnlyHint": True, "idempotentHint": True},
    "search_games_batch": {"readOnlyHint": True, "idempotentHint": True},
    "get_library_stats": {"readOnlyHint": True, "idempotentHint": True},
    "get_game_detail": {"readOnlyHint": True, "idempotentHint": True},
    "find_games_by_vibe": {"readOnlyHint": True, "idempotentHint": True},
    "get_recommendations": {"readOnlyHint": True, "idempotentHint": True},
    "get_taste_profile": {"readOnlyHint": True, "idempotentHint": True},
    "get_ratings": {"readOnlyHint": True, "idempotentHint": True},
    "get_backlog_stats": {"readOnlyHint": True, "idempotentHint": True},
    "get_integration_status": {"readOnlyHint": True, "idempotentHint": True},
    "get_platform_breakdown": {"readOnlyHint": True, "idempotentHint": True},
    "detect_farmed_games": {"destructiveHint": False, "idempotentHint": True},
    "sync_ratings": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "refresh_library": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "sync_platform": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "set_hardware_preference": {"readOnlyHint": False, "idempotentHint": True},
    "add_game_to_platform": {"readOnlyHint": False, "idempotentHint": True},
    "set_nintendo_session": {"readOnlyHint": False, "idempotentHint": True},
}

PAGINATED_OUTPUTS = {
    "search_games",
    "get_library_stats",
    "find_games_by_vibe",
    "get_recommendations",
    "get_ratings",
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

    async def test_all_tools_have_expected_annotations(self):
        tools = await self._tools()
        for name, expected in EXPECTED_ANNOTATIONS.items():
            with self.subTest(tool=name):
                annotations = tools[name].annotations
                self.assertIsNotNone(annotations)
                for field, value in expected.items():
                    self.assertEqual(getattr(annotations, field), value)

    async def test_all_tools_have_output_schema(self):
        tools = await self._tools()
        for name, tool in tools.items():
            with self.subTest(tool=name):
                self.assertIsNotNone(tool.output_schema)
                self.assertNotEqual(tool.output_schema, {})

    async def test_paginated_tools_have_named_output_properties(self):
        tools = await self._tools()
        for name in PAGINATED_OUTPUTS:
            with self.subTest(tool=name):
                props = set(tools[name].output_schema.get("properties", {}))
                self.assertIn("results", props)
                self.assertIn("total_matches", props)
                self.assertIn("has_more", props)

    async def test_detect_farmed_games_description_matches_defaults(self):
        tools = await self._tools()
        description = tools["detect_farmed_games"].description
        self.assertIn("default 8.0h", description)
        self.assertIn("default 8", description)
        self.assertNotIn("default 4h", description)
        self.assertNotIn("default 20", description)

    def test_server_instructions_include_discovery_workflow(self):
        instructions = main.mcp.instructions
        self.assertIn("sync_ratings", instructions)
        self.assertIn("get_recommendations", instructions)
        self.assertIn("find_games_by_vibe", instructions)
        self.assertIn("concise", instructions)
        self.assertIn("offset", instructions)


if __name__ == "__main__":
    unittest.main()
