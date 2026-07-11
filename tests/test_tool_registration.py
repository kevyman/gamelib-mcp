"""Freezes the MCP tool surface: names + parameter schemas.

This is the compatibility tripwire for the restructuring work — any later step
that changes a tool name, drops a parameter, or alters required-ness will fail
here. Description assertions stay limited to known regressions.
"""

import unittest

from gamelib_mcp import main


EXPECTED_TOOLS = {
    "search_games": {
        "params": {"query", "limit", "offset", "platform", "series", "response_format"},
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
            "tags",
            "genres",
            "series",
            "content",
        },
        "required": set(),
    },
    "get_game_detail": {"params": {"name", "appid", "game_id"}, "required": set()},
    "discover_games": {
        "params": {
            "vibes",
            "sort_by",
            "max_hltb_hours",
            "min_score",
            "unplayed_only",
            "protondb_min_tier",
            "limit",
            "offset",
            "response_format",
        },
        "required": set(),
    },
    "get_taste_profile": {"params": set(), "required": set()},
    "get_ratings": {
        "params": {"source", "min_score", "sort_by", "limit", "offset", "response_format"},
        "required": set(),
    },
    "sync_ratings": {"params": set(), "required": set()},
    "rate_game": {
        "params": {"name", "game_id", "score", "review_text"},
        "required": set(),
    },
    "get_backlog_stats": {"params": set(), "required": set()},
    "suggest_completion_status": {"params": {"limit"}, "required": set()},
    "get_series_breakdown": {
        "params": {
            "counting_mode",
            "kind",
            "min_games",
            "platform",
            "include_games",
            "limit",
            "offset",
        },
        "required": set(),
    },
    "discover_series_gaps": {
        "params": {"kind", "min_owned", "limit", "include_unreleased", "refresh_cache"},
        "required": set(),
    },
    "refresh_library": {"params": {"platforms"}, "required": set()},
    "get_sync_status": {"params": set(), "required": set()},
    "sync_wishlist": {"params": {"platforms"}, "required": set()},
    "get_integration_status": {
        "params": {"platforms", "verbose", "force_refresh"},
        "required": set(),
    },
    "detect_farmed_games": {
        "params": {"dry_run", "threshold_hours", "min_games_per_day"},
        "required": set(),
    },
    "detect_collapsed_games": {"params": set(), "required": set()},
    "detect_orphan_games": {"params": set(), "required": set()},
    "detect_stranded_duplicates": {"params": set(), "required": set()},
    "detect_cross_platform_collapses": {"params": {"limit"}, "required": set()},
    "detect_misclassified_dlc": {
        "params": {"limit", "probe_steam", "probe_offset"},
        "required": set(),
    },
    "revalidate_igdb_matches": {"params": {"dry_run", "limit"}, "required": set()},
    "split_game": {
        "params": {"source_game_id", "platform", "identifier_values", "new_name", "dry_run"},
        "required": {"source_game_id", "platform", "identifier_values"},
    },
    "get_platform_breakdown": {"params": set(), "required": set()},
    "get_wishlist": {"params": {"platform"}, "required": set()},
    "get_wishlist_deals": {
        "params": {"platform", "max_price", "min_cut_pct", "refresh", "preference_override_ratio"},
        "required": set(),
    },
    "get_play_history": {
        "params": {"days", "start_date", "end_date", "platform", "limit"},
        "required": set(),
    },
    "set_hardware_preference": {"params": {"platforms"}, "required": {"platforms"}},
    "add_game_to_platform": {
        "params": {
            "name", "platform", "identifier_type", "identifier_value",
            "playtime_minutes", "owned",
            "acquired_at", "price_paid", "price_currency", "purchase_source",
            "bundle_name",
        },
        "required": {"name", "platform"},
    },
    "set_nintendo_session": {"params": {"cookies"}, "required": {"cookies"}},
    "set_nintendo_ec_session": {"params": {"cookies"}, "required": {"cookies"}},
    "set_humble_session": {"params": {"cookies"}, "required": {"cookies"}},
    "set_steam_store_session": {"params": {"cookies"}, "required": {"cookies"}},
    "set_nintendo_pctl_session": {"params": {"response"}, "required": set()},
    "update_game": {
        "params": {
            "name",
            "game_id",
            "new_name",
            "sort_name",
            "release_date",
            "genres",
            "tags",
            "features",
            "short_description",
            "hltb_main",
            "hltb_extra",
            "hltb_complete",
            "is_farmed",
            "completion_status",
            "content_type",
            "parent_game_id",
            "parent_name",
            "clear_overrides",
        },
        "required": set(),
    },
    "set_acquisition": {
        "params": {
            "name",
            "game_id",
            "platform",
            "acquired_at",
            "price_paid",
            "price_currency",
            "purchase_source",
            "bundle_name",
            "clear",
            "create_platform_row",
        },
        "required": set(),
    },
    "set_acquisitions_batch": {
        "params": {"items", "overwrite", "create_platform_rows", "create_missing"},
        "required": {"items"},
    },
    "split_bundle_acquisition": {
        "params": {
            "bundle_name",
            "platform",
            "games",
            "total_price",
            "price_currency",
            "acquired_at",
            "purchase_source",
            "create_missing",
            "overwrite",
            "dry_run",
        },
        "required": {"bundle_name", "platform", "games"},
    },
    "import_purchases": {
        "params": {
            "sources",
            "dry_run",
            "overwrite",
            "create_platform_rows",
            "create_missing",
        },
        "required": set(),
    },
    "get_spending_stats": {
        "params": {"year", "platform", "purchase_source"},
        "required": set(),
    },
    "merge_games": {
        "params": {"source_game_id", "target_game_id", "dry_run"},
        "required": {"source_game_id", "target_game_id"},
    },
    "get_scrape_config": {"params": {"provider"}, "required": {"provider"}},
    "diagnose_scrape": {"params": {"provider"}, "required": {"provider"}},
    "propose_scrape_config": {
        "params": {"provider", "config", "note"},
        "required": {"provider", "config"},
    },
    "approve_scrape_config": {
        "params": {"provider", "version"},
        "required": {"provider", "version"},
    },
    "rollback_scrape_config": {"params": {"provider"}, "required": {"provider"}},
}

EXPECTED_ANNOTATIONS = {
    "search_games": {"readOnlyHint": True, "idempotentHint": True},
    "search_games_batch": {"readOnlyHint": True, "idempotentHint": True},
    "get_library_stats": {"readOnlyHint": True, "idempotentHint": True},
    "get_game_detail": {"readOnlyHint": True, "idempotentHint": True},
    "discover_games": {"readOnlyHint": True, "idempotentHint": True},
    "get_taste_profile": {"readOnlyHint": True, "idempotentHint": True},
    "get_ratings": {"readOnlyHint": True, "idempotentHint": True},
    "get_backlog_stats": {"readOnlyHint": True, "idempotentHint": True},
    "suggest_completion_status": {"readOnlyHint": True, "idempotentHint": True},
    "get_series_breakdown": {"readOnlyHint": True, "idempotentHint": True},
    "discover_series_gaps": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "get_integration_status": {"readOnlyHint": True, "idempotentHint": True},
    "get_platform_breakdown": {"readOnlyHint": True, "idempotentHint": True},
    "detect_farmed_games": {"destructiveHint": False, "idempotentHint": True},
    "detect_collapsed_games": {"readOnlyHint": True, "idempotentHint": True},
    "detect_orphan_games": {"readOnlyHint": True, "idempotentHint": True},
    "detect_stranded_duplicates": {"readOnlyHint": True, "idempotentHint": True},
    "detect_cross_platform_collapses": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "detect_misclassified_dlc": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "revalidate_igdb_matches": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "split_game": {"readOnlyHint": False, "idempotentHint": False},
    "sync_ratings": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "rate_game": {"readOnlyHint": False, "idempotentHint": True},
    "refresh_library": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "get_sync_status": {"readOnlyHint": True, "idempotentHint": True},
    "sync_wishlist": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "get_wishlist": {"readOnlyHint": True, "idempotentHint": True},
    "get_wishlist_deals": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    "get_play_history": {"readOnlyHint": True, "idempotentHint": True},
    "set_hardware_preference": {"readOnlyHint": False, "idempotentHint": True},
    "add_game_to_platform": {"readOnlyHint": False, "idempotentHint": True},
    "set_nintendo_session": {"readOnlyHint": False, "idempotentHint": True},
    "set_nintendo_ec_session": {"readOnlyHint": False, "idempotentHint": True},
    "set_humble_session": {"readOnlyHint": False, "idempotentHint": True},
    "set_steam_store_session": {"readOnlyHint": False, "idempotentHint": True},
    "set_nintendo_pctl_session": {"readOnlyHint": False, "idempotentHint": True},
    "update_game": {"readOnlyHint": False, "idempotentHint": True},
    "set_acquisition": {"readOnlyHint": False, "idempotentHint": True},
    "set_acquisitions_batch": {"readOnlyHint": False, "idempotentHint": True},
    "split_bundle_acquisition": {"readOnlyHint": False, "idempotentHint": True},
    "import_purchases": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "get_spending_stats": {"readOnlyHint": True, "idempotentHint": True},
    "merge_games": {"readOnlyHint": False, "idempotentHint": False},
    "get_scrape_config": {"readOnlyHint": True, "idempotentHint": True},
    "diagnose_scrape": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    "propose_scrape_config": {"readOnlyHint": False, "idempotentHint": True},
    "approve_scrape_config": {"readOnlyHint": False, "idempotentHint": True},
    # Each call walks back one more version — a retry is not a no-op.
    "rollback_scrape_config": {"readOnlyHint": False, "idempotentHint": False},
}

PAGINATED_OUTPUTS = {
    "search_games",
    "get_library_stats",
    "discover_games",
    "get_ratings",
    "get_series_breakdown",
}


class ToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def _tools(self):
        tools = await main.mcp.list_tools()
        return {t.name: t for t in tools}

    async def test_exact_tool_names(self):
        tools = await self._tools()
        self.assertEqual(set(tools), set(EXPECTED_TOOLS))

    async def test_tool_count_is_48(self):
        tools = await self._tools()
        self.assertEqual(len(tools), 48)

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
        self.assertIn("rate_game", instructions)
        self.assertIn("discover_games", instructions)
        self.assertIn("concise", instructions)
        self.assertIn("offset", instructions)


if __name__ == "__main__":
    unittest.main()
