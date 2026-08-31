"""Covers the mode-dispatch logic the merged tools added (ADR 0004).

Everything these wrappers delegate to is tested elsewhere against a real DB;
what is NEW and untested is the routing itself — which impl a mode picks, and
the defaults that differ between single and `items` mode. Those defaults exist
to preserve the behavior of the tools they replaced, so they are the part most
likely to regress silently. Impls are patched here so the tests stay fast and
offline; behavior against real data lives in the per-tool test modules.
"""

import unittest
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp import main
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools.assessment import record_assessment

# record_assessment assembles an evaluation package whose media step is the
# only provider call in this module; neutralized module-wide so nothing here
# reaches the network. The package test below patches it with real payloads.
_MEDIA_PATCH = patch(
    "gamelib_mcp.tools.assessment.get_game_media", AsyncMock(return_value=None)
)


def setUpModule():
    _MEDIA_PATCH.start()


def tearDownModule():
    _MEDIA_PATCH.stop()


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
        # steam, because the filter must be syncable for EVERY selected target
        # (see test_platform_unsupported_by_a_later_target_syncs_nothing).
        lib, wish, rate = self._patches()
        with lib as m_lib, wish as m_wish, rate as m_rate:
            await main.sync(ctx=None, targets=["library", "wishlist", "ratings"], platforms=["steam"])
        self.assertEqual(m_lib.await_args.args[0], ["steam"])
        self.assertEqual(m_wish.await_args.args[0], ["steam"])
        self.assertEqual(m_rate.await_args.args, ())  # ratings ignores platforms

    async def test_unknown_target_raises_and_syncs_nothing(self):
        lib, wish, rate = self._patches()
        with (
            lib as m_lib,
            wish as m_wish,
            rate as m_rate,
            self.assertRaises(Exception) as ctx,
        ):
            await main.sync(ctx=None, targets=["library", "achievements"])
        self.assertIn("achievements", str(ctx.exception))
        m_lib.assert_not_awaited()
        m_wish.assert_not_awaited()
        m_rate.assert_not_awaited()

    async def test_platform_unsupported_by_a_later_target_syncs_nothing(self):
        # GOG has a library sync but no wishlist sync. Library runs FIRST and is
        # fire-and-forget, so validating only inside sync_wishlist would have
        # launched the background sync and then errored — reporting a failure for
        # a sync that is actually running, with the retry saying already_running.
        lib, wish, rate = self._patches()
        with (
            lib as m_lib,
            wish as m_wish,
            rate as m_rate,
            self.assertRaises(Exception) as ctx,
        ):
            await main.sync(ctx=None, targets=["library", "wishlist"], platforms=["gog"])
        message = str(ctx.exception)
        self.assertIn("gog", message)
        self.assertIn("wishlist", message)
        m_lib.assert_not_awaited()
        m_wish.assert_not_awaited()
        m_rate.assert_not_awaited()

    async def test_platform_valid_for_every_target_still_dispatches(self):
        # Steam syncs both, and an alias must resolve before the check rejects it.
        lib, wish, _ = self._patches()
        for platforms in (["steam"], ["nintendo"]):
            with self.subTest(platforms=platforms):
                lib, wish, rate = self._patches()
                with lib as m_lib, wish as m_wish, rate:
                    await main.sync(ctx=None, targets=["library", "wishlist"], platforms=platforms)
                m_lib.assert_awaited_once()
                m_wish.assert_awaited_once()

    async def test_ratings_ignores_an_unwishlistable_platform(self):
        # ratings takes no platform filter, so it must not be what rejects one.
        lib, wish, rate = self._patches()
        with lib as m_lib, wish, rate as m_rate:
            await main.sync(ctx=None, targets=["library", "ratings"], platforms=["gog"])
        m_lib.assert_awaited_once()
        m_rate.assert_awaited_once()


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

    async def test_get_game_detail_refuses_bulk_media(self):
        # Same reason as enrich: one provider round trip per game. Rejected
        # before any impl runs, like every other multi-mode validation.
        with self.assertRaises(Exception) as ctx:
            await main.get_game_detail(items=[{"game_id": 1}], media=True)
        self.assertIn("media=True is not supported with items", str(ctx.exception))

    async def test_get_game_detail_passes_media_through_in_single_mode(self):
        gid = await seed_game("Media Passthrough")
        with patch(
            "gamelib_mcp.tools.detail.get_game_detail", new=AsyncMock(return_value={})
        ) as m:
            await main.get_game_detail(game_id=gid)
        self.assertIs(m.await_args.kwargs["media"], False)  # off by default

        with patch(
            "gamelib_mcp.tools.detail.get_game_detail", new=AsyncMock(return_value={})
        ) as m:
            await main.get_game_detail(game_id=gid, media=True)
        self.assertIs(m.await_args.kwargs["media"], True)

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


class ResponseSizeGuardTests(ToolDBTestCase):
    """No read response may contain a list that grows without bound.

    ADR 0004's amendment: `overlap_games` reached 428 entries / 98% of its
    response on a 3k-row library before anyone noticed, and `get_wishlist` had
    the same shape hidden behind an empty table. This walks real responses and
    fails if any list exceeds what its documented cap allows, so the next
    uncapped field is caught here rather than in production.
    """

    # tool -> (args, {list path: max entries the contract allows})
    CONTRACTS: ClassVar[list[tuple]] = [
        ("get_stats", {"report": "platforms"}, {"overlap_games": 25}),
        ("get_stats", {"report": "platforms", "limit": 5}, {"overlap_games": 5}),
        ("get_wishlist", {}, {"items": 100}),
        ("get_wishlist", {"limit": 3}, {"items": 3}),
        ("get_library_stats", {"limit": 7}, {"results": 7}),
        ("get_ratings", {"limit": 4}, {"results": 4}),
        ("get_play_history", {"limit": 6}, {"games": 6}),
        # by_bundle only showed up as unbounded once a DB with real purchase
        # history was used — the dev copy had zero priced rows.
        ("get_stats", {"report": "spending"}, {"by_bundle": 25}),
        # anchors scales with how many owned games share the candidate's core
        # tags — capped at ANCHOR_CAP (8) with anchor_count/anchors_truncated.
        ("get_assessment_context", {"tags": ["bulk tag"]}, {"anchors": 8}),
        # Recorded verdicts grow per game (detail/context blocks) and library-
        # wide (the reports) — every one of those lists carries a cap.
        ("get_stats", {"report": "assessments"}, {"assessments": 25}),
        ("get_stats", {"report": "assessments", "limit": 4}, {"assessments": 4}),
    ]

    # Nested list caps: (tool, args, {dotted path: cap}).
    NESTED_CONTRACTS: ClassVar[list[tuple]] = [
        (
            "get_stats",
            {"report": "calibration", "limit": 5},
            {
                "mismatches.skip_but_acquired.items": 5,
                "mismatches.buy_now_still_unplayed.items": 5,
                "mismatches.wishlist_still_waiting.items": 5,
                # Provenance grows with every distinct declared skill version
                # / model, so both blocks carry the same cap.
                "by_methodology.items": 5,
                "by_model.items": 5,
            },
        ),
    ]

    async def _seed_bulk(self, n=40):
        for i in range(n):
            gid = await seed_game(f"Bulk {i}", hltb_main=5.0, tags=["bulk tag"])
            # Assessed as "skip" BEFORE the ownership rows below, so
            # owned_at_assessment is 0 and every one lands in the
            # skip_but_acquired mismatch list.
            await record_assessment(
                game_id=gid,
                verdict="skip",
                assessed_at=f"2026-0{1 + i % 9}-0{1 + i % 9}",
                # Distinct declared provenance per row, so calibration's
                # by_methodology/by_model blocks really do exceed their cap
                # here instead of collapsing into one bucket.
                skill="game-quality",
                skill_version=f"1.{i}.0",
                model=f"model-{i}",
            )
            # owned on two platforms so it lands in the overlap list
            await add_platform(gid, "steam", playtime_minutes=100 + i)
            await add_platform(gid, "gog", playtime_minutes=50)
            await db_module.upsert_wishlist_entry(gid, "ps5", source="manual")

    async def test_no_response_list_exceeds_its_documented_cap(self):
        await self._seed_bulk()
        for tool, args, caps in self.CONTRACTS:
            with self.subTest(tool=tool, args=args):
                result = await getattr(main, tool)(**args)
                for path, cap in caps.items():
                    # A renamed key must fail here, not silently skip — that is
                    # how a guard like this rots into a no-op.
                    self.assertIn(
                        path, result,
                        f"{tool}{args} has no '{path}' key; the contract above is "
                        f"stale (keys: {sorted(result)})",
                    )
                    got = result[path]
                    self.assertLessEqual(
                        len(got), cap,
                        f"{tool}{args} returned {len(got)} entries at '{path}' "
                        f"but the contract caps it at {cap}",
                    )

    async def test_no_nested_response_list_exceeds_its_documented_cap(self):
        await self._seed_bulk()
        for tool, args, caps in self.NESTED_CONTRACTS:
            with self.subTest(tool=tool, args=args):
                result = await getattr(main, tool)(**args)
                for path, cap in caps.items():
                    node = result
                    for key in path.split("."):
                        self.assertIn(
                            key, node,
                            f"{tool}{args} has no '{path}'; the contract is stale",
                        )
                        node = node[key]
                    self.assertLessEqual(
                        len(node), cap,
                        f"{tool}{args} returned {len(node)} entries at '{path}' "
                        f"but the contract caps it at {cap}",
                    )

    async def test_per_game_assessment_blocks_are_capped(self):
        # Both blocks grow with how often ONE game was re-assessed (one row per
        # UTC day), which is exactly the shape the guard exists for.
        gid = await seed_game("Repeatedly Assessed", tags=["bulk tag"])
        await add_platform(gid, "steam", playtime_minutes=10)
        for day in range(1, 9):
            await record_assessment(
                game_id=gid, verdict="skip", assessed_at=f"2026-03-0{day}"
            )

        detail = await main.get_game_detail(game_id=gid)
        self.assertEqual(len(detail["assessments"]), 5)
        self.assertEqual(detail["assessment_count"], 8)
        self.assertTrue(detail["assessments_truncated"])

        context = await main.get_assessment_context(game_id=gid)
        self.assertEqual(len(context["past_assessments"]), 5)
        self.assertEqual(context["past_assessment_count"], 8)
        self.assertTrue(context["past_assessments_truncated"])

    async def test_capped_lists_still_report_the_true_total(self):
        await self._seed_bulk()

        platforms_report = await main.get_stats(report="platforms", limit=5)
        self.assertEqual(len(platforms_report["overlap_games"]), 5)
        self.assertEqual(platforms_report["overlap_count"], 40)
        self.assertTrue(platforms_report["overlap_truncated"])

        wishlist = await main.get_wishlist(limit=5)
        self.assertEqual(wishlist["count"], 5)
        self.assertEqual(wishlist["total_matches"], 40)
        self.assertTrue(wishlist["has_more"])

        assessment = await main.get_assessment_context(tags=["bulk tag"])
        self.assertEqual(len(assessment["anchors"]), 8)
        self.assertEqual(assessment["anchor_count"], 40)
        self.assertTrue(assessment["anchors_truncated"])

    async def test_detail_media_lists_are_capped(self):
        # get_game_detail(media=True) serves the same two growing lists the
        # evaluation package does — screenshots capped in data/media.py,
        # similar games in tools/game_media.py — each with its true total and
        # a truncation flag.
        gid = await seed_game("Media Detail", tags=["bulk tag"])
        await add_platform(gid, "steam", playtime_minutes=30)
        media = {
            "media": {
                "source": "igdb",
                "trailer": None,
                "screenshots": [{"thumb": f"t{i}", "full": f"f{i}"} for i in range(8)],
                "screenshot_count": 20,
                "screenshots_truncated": True,
                "short_description": "x",
            },
            "similar_raw": [
                {
                    "igdb_id": 900 + i,
                    "name": f"Similar {i}",
                    "release_year": 2020,
                    "cover_image_id": None,
                }
                for i in range(12)
            ],
            "similar_count": 12,
            # The studio's previous games are capped in data/media.py and again
            # in tools/game_media.py; a raw block over the cap proves the second
            # gate holds for a payload that arrived over it.
            "pedigree_raw": {
                "developer": {
                    "name": "Prolific Studio",
                    "igdb_company_id": 77,
                    "founded_year": 2001,
                    "country": 36,
                },
                "developer_names": ["Prolific Studio"],
                "publisher_name": None,
                "previous_games": [
                    {
                        "igdb_id": 800 + i,
                        "name": f"Earlier {i}",
                        "release_year": 2015 - i,
                        "cover_image_id": None,
                        "critic_score": 70,
                    }
                    for i in range(10)
                ],
                "previous_count": 10,
                "previous_truncated": True,
                "catalog_size": 12,
                "catalog_truncated": False,
                "big_catalog": False,
                "hypes": None,
            },
            "igdb_id": 5,
        }
        with patch(
            "gamelib_mcp.tools.game_media.get_game_media",
            new=AsyncMock(return_value=media),
        ):
            result = await main.get_game_detail(game_id=gid, media=True)

        for path, cap in {
            "media.screenshots": 8,
            "similar.items": 8,
            "pedigree.previous_games": 6,
        }.items():
            node = result
            for key in path.split("."):
                self.assertIn(key, node, f"detail has no '{path}'; the contract is stale")
                node = node[key]
            self.assertLessEqual(
                len(node), cap,
                f"get_game_detail(media=True) returned {len(node)} entries at "
                f"'{path}' but the contract caps it at {cap}",
            )
        self.assertEqual(result["media"]["screenshot_count"], 20)
        self.assertTrue(result["media"]["screenshots_truncated"])
        self.assertEqual(result["similar"]["count"], 12)
        self.assertTrue(result["similar"]["truncated"])
        self.assertEqual(result["pedigree"]["previous_count"], 10)
        self.assertTrue(result["pedigree"]["previous_truncated"])

    async def test_evaluation_package_lists_are_capped(self):
        # record_assessment's package is a WRITE response, but it carries the
        # same shapes: media and similar games grow with the source, past
        # verdicts with how often the game was re-assessed, anchors and
        # comparisons with what the caller cited.
        gid = await seed_game("Packaged", tags=["bulk tag"])
        await add_platform(gid, "steam", playtime_minutes=30)
        anchors = [await seed_game(f"Anchor {i}") for i in range(8)]
        for day in range(1, 9):
            await record_assessment(
                game_id=gid, verdict="skip", assessed_at=f"2026-03-0{day}"
            )

        media = {
            "media": {
                "source": "igdb",
                "trailer": None,
                # Already capped by data/media.py; asserted here so a raised
                # cap has to be raised deliberately in both places.
                "screenshots": [{"thumb": f"t{i}", "full": f"f{i}"} for i in range(8)],
                "screenshot_count": 20,
                "screenshots_truncated": True,
                "short_description": "x",
            },
            "similar_raw": [
                {
                    "igdb_id": 900 + i,
                    "name": f"Similar {i}",
                    "release_year": 2020,
                    "cover_image_id": None,
                }
                for i in range(12)
            ],
            "similar_count": 12,
            # The studio's previous games are capped in data/media.py and again
            # in tools/game_media.py; a raw block over the cap proves the second
            # gate holds for a payload that arrived over it.
            "pedigree_raw": {
                "developer": {
                    "name": "Prolific Studio",
                    "igdb_company_id": 77,
                    "founded_year": 2001,
                    "country": 36,
                },
                "developer_names": ["Prolific Studio"],
                "publisher_name": None,
                "previous_games": [
                    {
                        "igdb_id": 800 + i,
                        "name": f"Earlier {i}",
                        "release_year": 2015 - i,
                        "cover_image_id": None,
                        "critic_score": 70,
                    }
                    for i in range(10)
                ],
                "previous_count": 10,
                "previous_truncated": True,
                "catalog_size": 12,
                "catalog_truncated": False,
                "big_catalog": False,
                "hypes": None,
            },
            "igdb_id": 5,
        }
        with patch(
            "gamelib_mcp.tools.assessment.get_game_media",
            new=AsyncMock(return_value=media),
        ):
            result = await main.record_assessment(
                game_id=gid,
                verdict="buy_now",
                assessed_at="2026-04-01",
                anchors_cited=[
                    {"name": f"Anchor {i}", "game_id": anchor_id}
                    for i, anchor_id in enumerate(anchors)
                ],
                flags=[f"flag {i}" for i in range(8)],
                for_you_if=[f"bullet {i}" for i in range(4)],
                comparisons=[
                    {"name": f"Anchor {i}", "relation": "similar"} for i in range(6)
                ],
            )

        package = result["package"]
        caps = {
            "anchors": 8,
            "comparisons": 6,
            "flags": 8,
            "media.screenshots": 8,
            "similar.items": 8,
            "pedigree.previous_games": 6,
            "past.items": 5,
            "presentation.for_you_if": 4,
        }
        for path, cap in caps.items():
            node = package
            for key in path.split("."):
                self.assertIn(key, node, f"package has no '{path}'; the contract is stale")
                node = node[key]
            self.assertLessEqual(
                len(node), cap,
                f"package returned {len(node)} entries at '{path}' "
                f"but the contract caps it at {cap}",
            )
        # Capped lists still report the true totals.
        self.assertEqual(package["similar"]["count"], 12)
        self.assertTrue(package["similar"]["truncated"])
        self.assertEqual(package["pedigree"]["previous_count"], 10)
        self.assertTrue(package["pedigree"]["previous_truncated"])
        self.assertEqual(package["past"]["count"], 8)
        self.assertTrue(package["past"]["truncated"])
