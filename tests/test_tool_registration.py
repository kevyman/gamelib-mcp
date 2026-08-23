"""Freezes the MCP tool surface: names + parameter schemas.

This is the compatibility tripwire for the restructuring work — any later step
that changes a tool name, drops a parameter, or alters required-ness will fail
here. Description assertions stay limited to known regressions.
"""

import unittest

from gamelib_mcp import main

EXPECTED_TOOLS = {
    # --- reads ---------------------------------------------------------------
    "search_games": {
        "params": {
            "query", "queries", "limit", "offset", "platform", "series",
            "response_format", "limit_per_query",
        },
        "required": set(),
    },
    "get_library_stats": {
        "params": {
            "filter", "max_hltb_hours", "min_metacritic", "min_opencritic",
            "protondb_tier", "sort_by", "limit", "offset", "platform",
            "response_format", "tags", "genres", "series", "content",
        },
        "required": set(),
    },
    "get_game_detail": {
        "params": {"name", "appid", "game_id", "items", "enrich"},
        "required": set(),
    },
    "discover_games": {
        "params": {
            "vibes", "sort_by", "max_hltb_hours", "min_score", "unplayed_only",
            "protondb_min_tier", "limit", "offset", "response_format",
        },
        "required": set(),
    },
    "get_ratings": {
        "params": {"source", "min_score", "sort_by", "limit", "offset", "response_format"},
        "required": set(),
    },
    "get_stats": {
        "params": {
            "report", "platform", "year", "purchase_source", "counting_mode",
            "kind", "min_games", "include_games", "verdict", "limit", "offset",
        },
        "required": {"report"},
    },
    "get_play_history": {
        "params": {"days", "start_date", "end_date", "platform", "limit"},
        "required": set(),
    },
    "get_assessment_context": {
        "params": {
            "name", "appid", "game_id", "tags", "steam_positive_pct",
            "steam_total_reviews", "steam_recent_positive_pct",
            "steam_recent_total_reviews", "early_access",
        },
        "required": set(),
    },
    "get_skill": {
        "params": {"skill", "path"},
        "required": set(),
    },
    "get_wishlist": {
        "params": {
            "platform", "with_prices", "max_price", "min_cut_pct", "refresh",
            "preference_override_ratio", "limit", "offset",
        },
        "required": set(),
    },
    "discover_series_gaps": {
        "params": {
            "kind", "min_owned", "limit", "include_unreleased", "refresh_cache",
            "include_unavailable",
        },
        "required": set(),
    },
    "query_library": {"params": {"sql", "row_limit"}, "required": set()},
    "get_sync_status": {"params": set(), "required": set()},
    "get_integration_status": {
        "params": {"platforms", "verbose", "force_refresh"},
        "required": set(),
    },
    # --- writes: single + bulk (items=) --------------------------------------
    "rate_game": {
        "params": {"name", "game_id", "score", "review_text", "items", "dry_run"},
        "required": set(),
    },
    "add_game_to_platform": {
        "params": {
            "name", "platform", "game_id", "identifier_type", "identifier_value",
            "playtime_minutes", "owned", "acquired_at", "price_paid",
            "price_currency", "purchase_source", "bundle_name", "delisted",
            "unowned_at", "push_to_store", "wishlist_source", "items", "dry_run",
        },
        "required": set(),
    },
    "update_game": {
        "params": {
            "name", "game_id", "new_name", "sort_name", "release_date", "genres",
            "tags", "features", "short_description", "hltb_main", "hltb_extra",
            "hltb_complete", "is_farmed", "completion_status", "content_type",
            "parent_game_id", "parent_name", "cover_image_id", "igdb_id",
            "igdb_platforms", "clear_overrides", "items", "dry_run",
        },
        "required": set(),
    },
    "set_playtime": {
        "params": {
            "name", "game_id", "platform", "playtime_minutes", "last_played",
            "clear", "create_platform_row", "items", "dry_run",
        },
        "required": set(),
    },
    "set_acquisition": {
        "params": {
            "name", "game_id", "platform", "acquired_at", "price_paid",
            "price_currency", "purchase_source", "bundle_name", "clear",
            "create_platform_row", "items", "overwrite", "create_missing", "dry_run",
        },
        "required": set(),
    },
    "record_assessment": {
        "params": {
            "name", "appid", "game_id", "verdict", "assessed_at", "summary",
            "craft_adjusted", "craft_positive_pct", "review_count",
            "recent_trajectory", "opencritic_score", "fit_call",
            "anchors_cited", "flags", "price_seen", "price_currency",
            "price_platform", "target_price", "instead_game_id", "steam_appid",
            "context", "void_assessment_id", "items",
        },
        "required": set(),
    },
    "merge_games": {
        "params": {"source_game_id", "target_game_id", "items", "dry_run"},
        "required": set(),
    },
    "delete_game": {"params": {"name", "game_id", "confirm", "items"}, "required": set()},
    # --- writes: single-purpose ----------------------------------------------
    "sync": {"params": {"targets", "platforms"}, "required": set()},
    "check_library": {
        "params": {
            "checks", "include_network", "limit_per_check", "apply", "options",
            "list_checks", "suppress", "unsuppress",
        },
        "required": set(),
    },
    "split_game": {
        "params": {"source_game_id", "platform", "identifier_values", "new_name", "dry_run"},
        "required": {"source_game_id", "platform", "identifier_values"},
    },
    "set_hardware_preference": {"params": {"platforms"}, "required": {"platforms"}},
    "set_switch2_playtime_baseline": {
        "params": {"name", "game_id", "total_hours", "application_id", "dry_run"},
        "required": set(),
    },
    "split_bundle_acquisition": {
        "params": {
            "bundle_name", "platform", "games", "total_price", "price_currency",
            "acquired_at", "purchase_source", "create_missing", "overwrite", "dry_run",
        },
        "required": {"bundle_name", "platform", "games"},
    },
    "import_purchases": {
        "params": {
            "sources", "dry_run", "overwrite", "create_platform_rows", "create_missing",
        },
        "required": set(),
    },
    "create_session_ingest_link": {"params": {"provider"}, "required": {"provider"}},
    # --- scrape-config admin --------------------------------------------------
    "get_scrape_config": {"params": {"provider", "diagnose"}, "required": {"provider"}},
    "manage_scrape_config": {
        "params": {"provider", "action", "config", "note", "version"},
        "required": {"provider", "action"},
    },
}

EXPECTED_ANNOTATIONS = {
    "search_games": {"readOnlyHint": True, "idempotentHint": True},
    "get_library_stats": {"readOnlyHint": True, "idempotentHint": True},
    "get_game_detail": {"readOnlyHint": True, "idempotentHint": True},
    "discover_games": {"readOnlyHint": True, "idempotentHint": True},
    "get_ratings": {"readOnlyHint": True, "idempotentHint": True},
    "get_stats": {"readOnlyHint": True, "idempotentHint": True},
    "get_play_history": {"readOnlyHint": True, "idempotentHint": True},
    # Pure DB read — craft/fit inputs the caller can't compute locally come in
    # as parameters (web-searched review counts), never fetched here.
    "get_assessment_context": {"readOnlyHint": True, "idempotentHint": True},
    # Serves the skills/ text from disk — the tool twin of the skill://
    # resources for hosts whose model can't call resources/read (claude.ai).
    "get_skill": {"readOnlyHint": True, "idempotentHint": True},
    # Absorbing get_wishlist_deals made this open-world: with_prices=True
    # live-fetches ITAD/DekuDeals. Still read-only.
    "get_wishlist": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    "discover_series_gaps": {
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "query_library": {"readOnlyHint": True, "idempotentHint": True},
    "get_sync_status": {"readOnlyHint": True, "idempotentHint": True},
    "get_integration_status": {"readOnlyHint": True, "idempotentHint": True},
    "rate_game": {"readOnlyHint": False, "idempotentHint": True},
    "add_game_to_platform": {"readOnlyHint": False, "idempotentHint": True},
    "update_game": {"readOnlyHint": False, "idempotentHint": True},
    "set_playtime": {"readOnlyHint": False, "idempotentHint": True},
    "set_acquisition": {"readOnlyHint": False, "idempotentHint": True},
    # A same-day re-record replaces that day's row, so repeating the call is a
    # no-op rather than a second verdict — idempotent, and it destroys nothing.
    "record_assessment": {"readOnlyHint": False, "idempotentHint": True},
    "merge_games": {"readOnlyHint": False, "idempotentHint": False, "destructiveHint": True},
    "delete_game": {"readOnlyHint": False, "idempotentHint": False, "destructiveHint": True},
    "sync": {"readOnlyHint": False, "idempotentHint": True, "openWorldHint": True},
    "check_library": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    "split_game": {"readOnlyHint": False, "idempotentHint": False, "destructiveHint": True},
    "set_hardware_preference": {"readOnlyHint": False, "idempotentHint": True},
    "set_switch2_playtime_baseline": {"readOnlyHint": False, "idempotentHint": True},
    "split_bundle_acquisition": {"readOnlyHint": False, "idempotentHint": True},
    "import_purchases": {
        "readOnlyHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
    # Non-idempotent (every call mints a fresh nonce) but destroys nothing:
    # outstanding links die by TTL, never by a later mint.
    "create_session_ingest_link": {
        "readOnlyHint": False,
        "idempotentHint": False,
        "destructiveHint": False,
    },
    # diagnose=True live-fetches the provider page, so the merged read tool is
    # open-world; it stays read-only because neither mode writes.
    "get_scrape_config": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": True},
    # Takes the strictest of the three actions it absorbs: each rollback walks
    # back one more version, so a retry is not a no-op.
    "manage_scrape_config": {
        "readOnlyHint": False,
        "idempotentHint": False,
        "destructiveHint": True,
    },
}

# search_games keeps the paginated envelope in `query` mode (its `queries` mode
# answers under results_by_query); get_stats carries it for report="series".
PAGINATED_OUTPUTS = {
    "search_games",
    "get_library_stats",
    "discover_games",
    "get_ratings",
    "get_stats",
}


class ToolRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def _tools(self):
        tools = await main.mcp.list_tools()
        return {t.name: t for t in tools}

    async def test_exact_tool_names(self):
        tools = await self._tools()
        self.assertEqual(set(tools), set(EXPECTED_TOOLS))

    async def test_tool_count_is_32(self):
        tools = await self._tools()
        self.assertEqual(len(tools), 32)

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

    async def test_server_identity_metadata(self):
        # Implementation metadata (spec 2025-11-25): hosts label the connector
        # from version/website_url/icons in the initialize result. The icon
        # must stay a self-contained data: URI — the same no-external-fetch
        # rule as the apps.py widget.
        self.assertTrue(main.mcp.version)
        self.assertEqual(str(main.mcp.website_url), "https://github.com/kevyman/gamelib-mcp")
        icons = main.mcp.icons
        self.assertEqual(len(icons), 1)
        self.assertTrue(icons[0].src.startswith("data:image/svg+xml;base64,"))
        self.assertEqual(icons[0].mimeType, "image/svg+xml")

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

    async def test_check_library_description_lists_every_check_id(self):
        from gamelib_mcp.tools.checks import CHECKS

        tools = await self._tools()
        description = tools["check_library"].description
        for check_id in CHECKS:
            with self.subTest(check_id=check_id):
                self.assertIn(check_id, description)

    def test_server_instructions_include_discovery_workflow(self):
        instructions = main.mcp.instructions
        self.assertIn('sync(targets=["ratings"])', instructions)
        self.assertIn("rate_game", instructions)
        self.assertIn("discover_games", instructions)
        self.assertIn("concise", instructions)
        self.assertIn("offset", instructions)

    def test_server_instructions_advertise_the_bulk_items_convention(self):
        # The merged tools are only a win if a client knows to reach for
        # items=[...] instead of looping single calls (ADR 0004).
        self.assertIn("items=[...]", main.mcp.instructions)


if __name__ == "__main__":
    unittest.main()
