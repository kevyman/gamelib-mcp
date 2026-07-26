"""Covers the mode-dispatch logic the merged tools added (ADR 0004).

Everything these wrappers delegate to is tested elsewhere against a real DB;
what is NEW and untested is the routing itself — which impl a mode picks, and
the defaults that differ between single and `items` mode. Those defaults exist
to preserve the behavior of the tools they replaced, so they are the part most
likely to regress silently. Impls are patched here so the tests stay fast and
offline; behavior against real data lives in the per-tool test modules.
"""

import unittest
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, seed_game
from gamelib_mcp import main


class SyncTargetDispatchTests(unittest.IsolatedAsyncioTestCase):
    def _patches(self):
        return (
            patch("gamelib_mcp.tools.admin.refresh_library", new=AsyncMock(return_value={"status": "started"})),
            patch("gamelib_mcp.tools.admin.sync_wishlist", new=AsyncMock(return_value={"steam": {}})),
            patch("gamelib_mcp.tools.ratings.sync_ratings", new=AsyncMock(return_value={"status": "done"})),
        )

    async def test_default_targets_library_only(self):
        # Preserves refresh_library()'s old ergonomics: the bare call must not
        # start a 1-2 minute Backloggd scrape as a side effect.
        lib, wish, rate = self._patches()
        with lib as m_lib, wish as m_wish, rate as m_rate:
            result = await main.sync(ctx=None)
        self.assertEqual(result["targets"], ["library"])
        self.assertIn("library", result)
        self.assertNotIn("wishlist", result)
        self.assertNotIn("ratings", result)
        m_lib.assert_awaited_once()
        m_wish.assert_not_awaited()
        m_rate.assert_not_awaited()

    async def test_each_target_routes_to_its_own_impl(self):
        lib, wish, rate = self._patches()
        with lib as m_lib, wish as m_wish, rate as m_rate:
            result = await main.sync(ctx=None, targets=["library", "wishlist", "ratings"])
        self.assertEqual(set(result) - {"targets"}, {"library", "wishlist", "ratings"})
        m_lib.assert_awaited_once()
        m_wish.assert_awaited_once()
        m_rate.assert_awaited_once()

    async def test_platforms_filter_reaches_library_and_wishlist_only(self):
        lib, wish, rate = self._patches()
        with lib as m_lib, wish as m_wish, rate as m_rate:
            await main.sync(ctx=None, targets=["library", "wishlist", "ratings"], platforms=["gog"])
        self.assertEqual(m_lib.await_args.args[0], ["gog"])
        self.assertEqual(m_wish.await_args.args[0], ["gog"])
        self.assertEqual(m_rate.await_args.args, ())  # ratings ignores platforms

    async def test_unknown_target_raises_and_syncs_nothing(self):
        lib, wish, rate = self._patches()
        with lib as m_lib, wish as m_wish, rate as m_rate:
            with self.assertRaises(Exception) as ctx:
                await main.sync(ctx=None, targets=["library", "achievements"])
        self.assertIn("achievements", str(ctx.exception))
        m_lib.assert_not_awaited()
        m_wish.assert_not_awaited()
        m_rate.assert_not_awaited()


class GetStatsReportDispatchTests(ToolDBTestCase):
    async def test_every_report_routes_and_echoes_its_name(self):
        gid = await seed_game("Dispatch Target", hltb_main=10.0)
        await add_platform(gid, "steam", playtime_minutes=600)

        expected_keys = {
            "backlog": "unplayed_spend",
            "platforms": "by_platform",
            "taste": "top_tags",
            "spending": "coverage_pct",
            "series": "counting_mode",
        }
        for report, key in expected_keys.items():
            with self.subTest(report=report):
                result = await main.get_stats(report=report)
                self.assertEqual(result["report"], report)
                self.assertIn(key, result)

    async def test_series_report_is_the_paginated_one(self):
        result = await main.get_stats(report="series", limit=5, offset=0)
        for key in ("results", "total_matches", "has_more"):
            self.assertIn(key, result)

    async def test_spending_filters_reach_the_impl(self):
        with patch(
            "gamelib_mcp.tools.acquisition.get_spending_stats", new=AsyncMock(return_value={})
        ) as m:
            await main.get_stats(
                report="spending", year=2025, platform="steam", purchase_source="humble"
            )
        self.assertEqual(m.await_args.args, (2025, "steam", "humble"))


class ModeDependentDefaultTests(ToolDBTestCase):
    """The three params whose default flips between single and `items` mode.

    Each one preserves the behavior of a tool pair that ADR 0004 merged; a
    regression here silently changes what a write does, which is why they are
    pinned rather than left to the docstring.
    """

    async def test_get_game_detail_enriches_single_but_not_items(self):
        gid = await seed_game("Enrichment Probe")
        await add_platform(gid, "steam")

        with patch(
            "gamelib_mcp.tools.detail.get_game_detail", new=AsyncMock(return_value={})
        ) as m:
            await main.get_game_detail(game_id=gid)
        self.assertIs(m.await_args.kwargs["enrich"], True)

        with patch(
            "gamelib_mcp.tools.detail.get_game_details_batch", new=AsyncMock(return_value={})
        ) as m:
            await main.get_game_detail(items=[{"game_id": gid}])
        m.assert_awaited_once()  # the bulk impl hardcodes enrich=False

    async def test_get_game_detail_refuses_bulk_enrich(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_game_detail(items=[{"game_id": 1}], enrich=True)
        self.assertIn("not supported with items", str(ctx.exception))

    async def test_set_acquisition_overwrite_flips_by_mode(self):
        with patch(
            "gamelib_mcp.tools.acquisition.set_acquisitions_batch", new=AsyncMock(return_value={})
        ) as m:
            await main.set_acquisition(items=[{"game_id": 1, "platform": "steam"}])
        # An import must never clobber a hand-set value, and must not silently
        # mint platform rows.
        self.assertIs(m.await_args.kwargs["overwrite"], False)
        self.assertIs(m.await_args.kwargs["create_platform_rows"], False)

        with patch(
            "gamelib_mcp.tools.acquisition.set_acquisitions_batch", new=AsyncMock(return_value={})
        ) as m:
            await main.set_acquisition(
                items=[{"game_id": 1, "platform": "steam"}],
                overwrite=True,
                create_platform_row=True,
            )
        self.assertIs(m.await_args.kwargs["overwrite"], True)
        self.assertIs(m.await_args.kwargs["create_platform_rows"], True)

    async def test_set_acquisition_single_creates_platform_row_by_default(self):
        gid = await seed_game("No Platform Row Yet")
        result = await main.set_acquisition(game_id=gid, platform="steam", price_paid=1.0)
        self.assertTrue(result["platform_row_created"])

    async def test_set_acquisition_rejects_single_mode_fill_only(self):
        # overwrite=False has no meaning for a single call — it would make the
        # write a silent no-op on any column that already has a value.
        with self.assertRaises(Exception) as ctx:
            await main.set_acquisition(game_id=1, platform="steam", overwrite=False)
        self.assertIn("only supported with items", str(ctx.exception))


class BulkModeRoutingTests(ToolDBTestCase):
    """`items` must reach the bulk impl, and its absence the single impl."""

    async def test_search_queries_mode_wraps_results_by_query(self):
        gid = await seed_game("Routed Game")
        await add_platform(gid, "steam")
        result = await main.search_games(queries=["routed"])
        self.assertIn("results_by_query", result)
        self.assertEqual(list(result["results_by_query"]), ["routed"])

    async def test_search_requires_query_or_queries(self):
        with self.assertRaises(Exception) as ctx:
            await main.search_games()
        self.assertIn("queries", str(ctx.exception))

    async def test_merge_games_requires_both_ids_without_items(self):
        with self.assertRaises(Exception) as ctx:
            await main.merge_games(source_game_id=1)
        self.assertIn("items", str(ctx.exception))

    async def test_each_write_tool_routes_items_to_its_bulk_impl(self):
        cases = [
            ("gamelib_mcp.tools.ratings.rate_games_batch", main.rate_game),
            ("gamelib_mcp.tools.platforms.update_games_batch", main.update_game),
            ("gamelib_mcp.tools.platforms.set_playtime_batch", main.set_playtime),
            (
                "gamelib_mcp.tools.platforms.add_games_to_platform_batch",
                main.add_game_to_platform,
            ),
            ("gamelib_mcp.tools.admin.merge_games_batch", main.merge_games),
        ]
        for target, tool in cases:
            with self.subTest(tool=tool.__name__):
                with patch(target, new=AsyncMock(return_value={})) as m:
                    await tool(items=[{"game_id": 1}])
                m.assert_awaited_once()

    async def test_delete_game_items_routes_and_keeps_confirm(self):
        with patch(
            "gamelib_mcp.tools.admin.delete_games_batch", new=AsyncMock(return_value={})
        ) as m:
            await main.delete_game(items=[{"game_id": 1}], confirm=True)
        self.assertEqual(m.await_args.args[1], True)


class ScrapeConfigActionDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnose_flag_picks_the_live_probe(self):
        with patch(
            "gamelib_mcp.tools.scrape_admin.get_scrape_config", new=AsyncMock(return_value={})
        ) as m_get, patch(
            "gamelib_mcp.tools.scrape_admin.diagnose_scrape", new=AsyncMock(return_value={})
        ) as m_diag:
            await main.get_scrape_config("backloggd")
            m_get.assert_awaited_once()
            m_diag.assert_not_awaited()

            await main.get_scrape_config("backloggd", diagnose=True)
            m_diag.assert_awaited_once()

    async def test_each_action_routes_to_its_impl(self):
        with patch(
            "gamelib_mcp.tools.scrape_admin.propose_scrape_config", new=AsyncMock(return_value={})
        ) as m_prop, patch(
            "gamelib_mcp.tools.scrape_admin.approve_scrape_config", new=AsyncMock(return_value={})
        ) as m_app, patch(
            "gamelib_mcp.tools.scrape_admin.rollback_scrape_config", new=AsyncMock(return_value={})
        ) as m_roll:
            await main.manage_scrape_config("backloggd", "propose", config={"a": 1}, note="why")
            self.assertEqual(m_prop.await_args.args, ("backloggd", {"a": 1}, "why"))

            await main.manage_scrape_config("backloggd", "approve", version=3)
            self.assertEqual(m_app.await_args.args, ("backloggd", 3))

            await main.manage_scrape_config("backloggd", "rollback")
            self.assertEqual(m_roll.await_args.args, ("backloggd",))

    async def test_missing_action_payload_names_what_is_needed(self):
        with self.assertRaises(Exception) as ctx:
            await main.manage_scrape_config("backloggd", "propose")
        self.assertIn("config", str(ctx.exception))

        with self.assertRaises(Exception) as ctx:
            await main.manage_scrape_config("backloggd", "approve")
        self.assertIn("version", str(ctx.exception))


class QueryLibrarySchemaModeTests(ToolDBTestCase):
    async def test_no_sql_returns_the_schema(self):
        result = await main.query_library()
        self.assertIn("tables", result)
        self.assertIn("example_queries", result)
        self.assertNotIn("rows", result)

    async def test_sql_runs_the_query(self):
        await seed_game("Countable")
        result = await main.query_library(sql="SELECT COUNT(*) AS n FROM games")
        self.assertEqual(result["rows"][0][0], 1)


class WishlistPriceModeTests(ToolDBTestCase):
    async def test_with_prices_routes_to_the_deals_impl(self):
        with patch(
            "gamelib_mcp.tools.deals.get_wishlist_deals", new=AsyncMock(return_value={})
        ) as m:
            await main.get_wishlist(with_prices=True, max_price=10.0)
        self.assertEqual(m.await_args.args[1], 10.0)

    async def test_default_mode_reads_stored_rows(self):
        result = await main.get_wishlist()
        self.assertIn("items", result)
        self.assertNotIn("deals", result)


if __name__ == "__main__":
    unittest.main()


class GetStatsParamMismatchTests(ToolDBTestCase):
    """A param belonging to another report is an error, not a silent no-op.

    Dropping it would answer a question the caller didn't ask while looking
    like it honored the filter.
    """

    async def test_spending_param_on_series_report_raises(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_stats(report="series", year=2025)
        self.assertIn("year", str(ctx.exception))
        self.assertIn("series", str(ctx.exception))

    async def test_series_param_on_spending_report_raises(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_stats(report="spending", counting_mode="entries")
        self.assertIn("counting_mode", str(ctx.exception))

    async def test_any_param_on_a_zero_arg_report_raises(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_stats(report="backlog", platform="steam")
        self.assertIn("no parameters", str(ctx.exception))

    async def test_platform_is_shared_by_spending_and_series(self):
        await main.get_stats(report="spending", platform="steam")
        await main.get_stats(report="series", platform="steam")

    async def test_defaults_never_trip_the_guard(self):
        for report in ("backlog", "platforms", "taste", "spending", "series"):
            with self.subTest(report=report):
                await main.get_stats(report=report)


class SetAcquisitionSingleModeGuardTests(ToolDBTestCase):
    async def test_create_missing_is_refused_outside_items(self):
        # Silently ignoring it would report success while minting nothing.
        with self.assertRaises(Exception) as ctx:
            await main.set_acquisition(name="Nonexistent", platform="steam", create_missing=True)
        self.assertIn("only supported with items", str(ctx.exception))

    async def test_dry_run_is_refused_outside_items(self):
        with self.assertRaises(Exception) as ctx:
            await main.set_acquisition(game_id=1, platform="steam", dry_run=True)
        self.assertIn("only supported with items", str(ctx.exception))
