"""record_assessment + the read paths and reports it feeds (ADR 0006 decision 5).

The hard constraint the ADR sets — a recorded verdict never reaches
tag_affinity or discover_games — is asserted here directly (no recompute, no
change in ranking), because it is the kind of rule that decays into a comment
otherwise.
"""

import json
import unittest
from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_platform,
    add_rating,
    add_steam_appid,
    make_steam_game,
    seed_game,
)
from fastmcp.exceptions import ToolError

from gamelib_mcp import main
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import admin, deals, platforms
from gamelib_mcp.tools.assessment import (
    get_assessment_context,
    record_assessment,
    record_assessments_batch,
)


async def _assessment_rows(game_id: int) -> list[dict]:
    async with db_module.get_db() as db:
        rows = await db.execute_fetchall(
            "SELECT * FROM game_assessments WHERE game_id = ? ORDER BY id", (game_id,)
        )
    return [dict(row) for row in rows]


class RecordAssessmentTests(ToolDBTestCase):
    async def test_links_to_an_existing_game_and_stores_components(self):
        game_id = await make_steam_game("Hollow Knight", 367520)
        anchor_id = await seed_game("Ori")

        result = await record_assessment(
            name="Hollow Knight",
            verdict="buy_now",
            summary="Buy it — the anchors all point the same way.",
            craft_adjusted=0.94,
            craft_positive_pct=97.0,
            review_count=140000,
            recent_trajectory="stable",
            opencritic_score=90.0,
            fit_call="strong fit",
            anchors_cited=[{"name": "Ori", "game_id": anchor_id}, "Dead Cells"],
            flags=["long for an evening session"],
            price_seen=14.99,
            price_currency="eur",
            price_platform="Steam",
            context="bundle: Humble Choice 2026-08",
        )

        self.assertEqual(result["game_id"], game_id)
        self.assertFalse(result["created"])
        self.assertFalse(result["replaced"])
        self.assertNotIn("repeat_ask", result)

        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["verdict"], "buy_now")
        self.assertEqual(row["craft_adjusted"], 0.94)
        self.assertEqual(row["review_count"], 140000)
        self.assertEqual(row["fit_call"], "strong fit")
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["price_platform"], "steam")
        self.assertIn("Humble Choice", row["context"])
        self.assertEqual(
            json.loads(row["anchors_cited"]),
            [{"name": "Ori", "game_id": anchor_id}, {"name": "Dead Cells"}],
        )

    async def test_unknown_candidate_mints_a_row_carrying_the_appid(self):
        result = await record_assessment(
            name="Some Unreleased Thing",
            appid=999001,
            verdict="try_demo",
        )
        self.assertTrue(result["created"])

        rows = await _assessment_rows(result["game_id"])
        self.assertEqual(rows[0]["steam_appid"], 999001)
        # A minted candidate is ownership-free by design: no platform row is
        # invented for it, which is why the appid lives on the assessment.
        async with db_module.get_db() as db:
            platform_rows = await db.execute_fetchall(
                "SELECT 1 FROM game_platforms WHERE game_id = ?", (result["game_id"],)
            )
        self.assertEqual(platform_rows, [])

    async def test_mint_respects_the_anti_collapse_guard(self):
        # The exact-name row owns Steam under a DIFFERENT appid: assessing the
        # remake must not attach onto the original (root CLAUDE.md's identity
        # rule, the same guard sync_wishlist's mint path applies).
        original = await make_steam_game("Dead Space", 17470)

        result = await record_assessment(
            name="Dead Space", appid=1693980, verdict="wishlist_for_sale"
        )

        self.assertTrue(result["created"])
        self.assertNotEqual(result["game_id"], original)

    async def test_appid_only_miss_asks_for_a_name_instead_of_minting(self):
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(appid=424242, verdict="skip")
        self.assertIn("name", str(ctx.exception))

    async def test_unknown_game_id_is_an_error_not_a_mint(self):
        with self.assertRaises(ToolError):
            await record_assessment(game_id=98765, verdict="skip")

    async def test_same_day_rerecord_replaces_that_days_row(self):
        game_id = await seed_game("Reassessed")
        first = await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-08-01T09:00:00Z",
            summary="first pass",
        )
        second = await record_assessment(
            game_id=game_id,
            verdict="buy_now",
            assessed_at="2026-08-01T18:30:00Z",
            summary="changed my mind after the anchors",
        )

        self.assertFalse(first["replaced"])
        self.assertTrue(second["replaced"])
        self.assertEqual(second["assessment_id"], first["assessment_id"])

        rows = await _assessment_rows(game_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "buy_now")
        self.assertEqual(rows[0]["summary"], "changed my mind after the anchors")
        self.assertEqual(rows[0]["assessed_at"], "2026-08-01T18:30:00+00:00")

    async def test_a_later_day_appends_and_reports_the_repeat_ask(self):
        game_id = await seed_game("Asked Twice")
        await record_assessment(
            game_id=game_id, verdict="skip", assessed_at="2026-06-01"
        )
        result = await record_assessment(
            game_id=game_id, verdict="buy_now", assessed_at="2026-08-01"
        )

        self.assertFalse(result["replaced"])
        self.assertEqual(len(await _assessment_rows(game_id)), 2)
        self.assertEqual(result["repeat_ask"]["previous_count"], 1)
        self.assertEqual(result["repeat_ask"]["last_verdict"], "skip")
        self.assertTrue(result["repeat_ask"]["last_assessed_at"].startswith("2026-06-01"))

    async def test_ownership_state_is_captured_at_write_time(self):
        game_id = await seed_game("Bought Later")
        await record_assessment(game_id=game_id, verdict="skip", assessed_at="2026-06-01")
        await add_platform(game_id, "steam", playtime_minutes=0)
        await record_assessment(game_id=game_id, verdict="buy_now", assessed_at="2026-07-01")

        rows = await _assessment_rows(game_id)
        self.assertEqual(rows[0]["owned_at_assessment"], 0)
        self.assertEqual(rows[1]["owned_at_assessment"], 1)

    async def test_wishlist_for_sale_suggests_a_promotion_without_writing_one(self):
        game_id = await seed_game("Waiting For A Sale")
        result = await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            target_price=19.99,
            price_currency="EUR",
            price_platform="steam",
        )

        self.assertEqual(result["suggested_action"]["tool"], "add_game_to_platform")
        self.assertEqual(
            result["suggested_action"]["args"]["wishlist_source"], "assessment"
        )
        async with db_module.get_db() as db:
            wishlist = await db.execute_fetchall(
                "SELECT 1 FROM game_wishlist WHERE game_id = ?", (game_id,)
            )
        self.assertEqual(wishlist, [])

    async def test_no_suggestion_when_already_wishlisted(self):
        game_id = await seed_game("Already Wishlisted")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="manual")
        result = await record_assessment(
            game_id=game_id, verdict="wishlist_for_sale", target_price=10.0
        )
        self.assertNotIn("suggested_action", result)
        rows = await _assessment_rows(game_id)
        self.assertEqual(rows[0]["wishlisted_at_assessment"], 1)

    async def test_never_recomputes_tag_affinity(self):
        # ADR 0006's hard constraint: verdicts are model output and must not
        # reach the ranking inputs. Every other write tool recomputes here.
        game_id = await seed_game("No Affinity Please", tags=["metroidvania"])
        with patch(
            "gamelib_mcp.data.db.recompute_tag_affinity", new=AsyncMock()
        ) as recompute:
            await record_assessment(game_id=game_id, verdict="buy_now")
            await record_assessments_batch(
                [{"game_id": game_id, "verdict": "skip", "assessed_at": "2026-05-05"}]
            )
        recompute.assert_not_awaited()


class RecordAssessmentValidationTests(ToolDBTestCase):
    async def test_identity_is_required(self):
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(verdict="skip")
        self.assertIn("identity", str(ctx.exception))

    async def test_verdict_is_required_and_enumerated(self):
        game_id = await seed_game("Enum Probe")
        with self.assertRaises(ToolError):
            await record_assessment(game_id=game_id)
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(game_id=game_id, verdict="maybe_later")
        self.assertIn("maybe_later", str(ctx.exception))

    async def test_enum_columns_reject_unknown_values(self):
        game_id = await seed_game("Enum Probe 2")
        for kwargs in (
            {"recent_trajectory": "collapsing"},
            {"fit_call": "great fit"},
        ):
            with self.subTest(**kwargs), self.assertRaises(ToolError):
                await record_assessment(game_id=game_id, verdict="skip", **kwargs)

    async def test_craft_scales_are_not_interchangeable(self):
        game_id = await seed_game("Scale Probe")
        # 88 is a valid percentage but not a valid 0-1 adjusted score.
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(game_id=game_id, verdict="skip", craft_adjusted=88)
        self.assertIn("craft_adjusted", str(ctx.exception))
        with self.assertRaises(ToolError):
            await record_assessment(
                game_id=game_id, verdict="skip", craft_positive_pct=880
            )

    async def test_negative_prices_and_bad_ids_rejected(self):
        game_id = await seed_game("Price Probe")
        with self.assertRaises(ToolError):
            await record_assessment(game_id=game_id, verdict="skip", price_seen=-1)
        with self.assertRaises(ToolError):
            await record_assessment(game_id=game_id, verdict="skip", target_price=-0.5)
        with self.assertRaises(ToolError):
            await record_assessment(game_id=game_id, verdict="skip", steam_appid=0)
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id, verdict="skip", instead_game_id=4242
            )
        self.assertIn("instead_game_id", str(ctx.exception))

    async def test_list_caps_are_rejected_and_text_is_truncated(self):
        game_id = await seed_game("Cap Probe")
        with self.assertRaises(ToolError):
            await record_assessment(
                game_id=game_id, verdict="skip", anchors_cited=[f"A{i}" for i in range(9)]
            )
        with self.assertRaises(ToolError):
            await record_assessment(
                game_id=game_id, verdict="skip", flags=[f"f{i}" for i in range(9)]
            )
        await record_assessment(
            game_id=game_id, verdict="skip", summary="x" * 400, context="y" * 300
        )
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(len(row["summary"]), 300)
        self.assertEqual(len(row["context"]), 200)

    async def test_validation_runs_before_any_write(self):
        game_id = await seed_game("Atomic Probe")
        with self.assertRaises(ToolError):
            await record_assessment(
                game_id=game_id, verdict="skip", craft_adjusted=5, instead_game_id=1
            )
        self.assertEqual(await _assessment_rows(game_id), [])

    async def test_bad_assessed_at_is_named(self):
        game_id = await seed_game("Date Probe")
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id, verdict="skip", assessed_at="last tuesday"
            )
        self.assertIn("assessed_at", str(ctx.exception))


class RecordAssessmentBulkTests(ToolDBTestCase):
    async def test_items_isolate_failures_and_preserve_order(self):
        first = await seed_game("Bulk One")
        second = await seed_game("Bulk Two")
        result = await record_assessments_batch(
            [
                {"game_id": first, "verdict": "buy_now"},
                {"game_id": second, "verdict": "not_a_verdict"},
                {"game_id": second, "verdict": "skip"},
            ]
        )
        self.assertEqual([r["status"] for r in result["results"]], ["ok", "error", "ok"])
        self.assertEqual(result["ok"], 2)
        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["results"][1]["item"]["verdict"], "not_a_verdict")

    async def test_empty_items_raises(self):
        with self.assertRaises(ToolError):
            await record_assessments_batch([])

    async def test_unknown_item_key_is_a_per_item_error(self):
        game_id = await seed_game("Bulk Keys")
        result = await record_assessments_batch(
            [{"game_id": game_id, "verdict": "skip", "playtime_minutes": 10}]
        )
        self.assertEqual(result["results"][0]["status"], "error")
        self.assertIn("playtime_minutes", result["results"][0]["error"])

    async def test_items_route_through_the_tool(self):
        game_id = await seed_game("Routed Assessment")
        result = await main.record_assessment(
            items=[{"game_id": game_id, "verdict": "skip"}]
        )
        self.assertEqual(result["ok"], 1)

    async def test_single_mode_passes_every_component_to_the_right_column(self):
        # The tool hands 21 positional arguments to the impl; a mis-ordering
        # would quietly store the price as the review count.
        game_id = await seed_game("Wire Order")
        alternative = await seed_game("The Alternative")
        await main.record_assessment(
            game_id=game_id,
            verdict="play_what_you_own",
            assessed_at="2026-08-09",
            summary="you already own a better one",
            craft_adjusted=0.81,
            craft_positive_pct=85.0,
            review_count=4200,
            recent_trajectory="regressing",
            opencritic_score=77.0,
            fit_call="probable miss",
            anchors_cited=[{"name": "The Alternative", "game_id": alternative}],
            flags=["live service"],
            price_seen=49.99,
            price_currency="EUR",
            price_platform="steam",
            target_price=24.99,
            instead_game_id=alternative,
            steam_appid=555,
            context="summer sale",
        )
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["assessed_at"], "2026-08-09T00:00:00+00:00")
        self.assertEqual(row["summary"], "you already own a better one")
        self.assertEqual(row["craft_adjusted"], 0.81)
        self.assertEqual(row["craft_positive_pct"], 85.0)
        self.assertEqual(row["review_count"], 4200)
        self.assertEqual(row["recent_trajectory"], "regressing")
        self.assertEqual(row["opencritic_score"], 77.0)
        self.assertEqual(row["fit_call"], "probable miss")
        self.assertEqual(row["flags"], '["live service"]')
        self.assertEqual(row["price_seen"], 49.99)
        self.assertEqual(row["price_currency"], "EUR")
        self.assertEqual(row["price_platform"], "steam")
        self.assertEqual(row["target_price"], 24.99)
        self.assertEqual(row["instead_game_id"], alternative)
        self.assertEqual(row["steam_appid"], 555)
        self.assertEqual(row["context"], "summer sale")


class AssessmentReadBlockTests(ToolDBTestCase):
    async def test_detail_carries_the_newest_verdicts(self):
        game_id = await make_steam_game("Detailed", 1000)
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-06-01",
            summary="not now",
            fit_call="coin flip",
            craft_adjusted=0.8,
            price_seen=30.0,
            price_currency="EUR",
            target_price=15.0,
        )
        await record_assessment(
            game_id=game_id, verdict="buy_now", assessed_at="2026-07-01"
        )

        detail = await main.get_game_detail(game_id=game_id)
        self.assertEqual(detail["assessment_count"], 2)
        self.assertFalse(detail["assessments_truncated"])
        self.assertEqual(detail["assessments"][0]["verdict"], "buy_now")
        self.assertEqual(detail["assessments"][1]["target_price"], 15.0)
        self.assertEqual(detail["assessments"][1]["fit_call"], "coin flip")

    async def test_bulk_detail_skips_the_block(self):
        game_id = await seed_game("Bulk Detail")
        await record_assessment(game_id=game_id, verdict="skip")
        result = await main.get_game_detail(items=[{"game_id": game_id}])
        self.assertNotIn("assessments", result["results"][0])

    async def test_detail_omits_the_block_when_never_assessed(self):
        game_id = await seed_game("Never Assessed")
        detail = await main.get_game_detail(game_id=game_id)
        self.assertNotIn("assessments", detail)

    async def test_context_reports_past_assessments(self):
        game_id = await seed_game("Repeat Ask", tags=["metroidvania"])
        await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            assessed_at="2026-05-01",
            target_price=20.0,
        )
        context = await get_assessment_context(game_id=game_id)
        self.assertEqual(context["past_assessment_count"], 1)
        self.assertFalse(context["past_assessments_truncated"])
        self.assertEqual(context["past_assessments"][0]["verdict"], "wishlist_for_sale")

    async def test_context_omits_the_block_for_an_unassessed_game(self):
        game_id = await seed_game("Fresh Candidate", tags=["metroidvania"])
        context = await get_assessment_context(game_id=game_id)
        self.assertNotIn("past_assessments", context)


class WishlistAnnotationTests(ToolDBTestCase):
    async def test_wishlist_items_carry_the_latest_verdict(self):
        game_id = await seed_game("Wishlisted Candidate")
        await db_module.upsert_wishlist_entry(game_id, "steam", source="assessment")
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-04-01",
            target_price=5.0,
        )
        await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            assessed_at="2026-06-01",
            target_price=19.99,
            price_currency="EUR",
        )
        plain = await seed_game("Unassessed Wishlist Item")
        await db_module.upsert_wishlist_entry(plain, "steam", source="manual")

        result = await platforms.get_wishlist()
        by_id = {item["game_id"]: item for item in result["items"]}
        self.assertEqual(by_id[game_id]["assessment"]["verdict"], "wishlist_for_sale")
        self.assertEqual(by_id[game_id]["assessment"]["target_price"], 19.99)
        self.assertNotIn("assessment", by_id[plain])

    async def _priced_wishlist_game(self, price: float, currency: str = "EUR") -> int:
        game_id = await seed_game("Priced Candidate")
        gpid = await add_platform(game_id, "steam", owned=0)
        await add_steam_appid(gpid, 4242)
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="manual", store_identifier="4242"
        )
        await db_module.upsert_game_prices(
            [
                {
                    "game_id": game_id,
                    "platform": "steam",
                    "shop": "steam",
                    "price": price,
                    "regular_price": 39.99,
                    "cut_pct": 50,
                    "currency": currency,
                    "deal_url": "https://example.com/deal",
                }
            ]
        )
        return game_id

    async def _deals(self) -> dict:
        with patch(
            "gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})
        ), patch(
            "gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})
        ), patch(
            "gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock(return_value={})
        ), patch(
            "gamelib_mcp.tools.deals.is_itad_configured", return_value=True
        ):
            return await deals.get_wishlist_deals()

    async def test_deal_below_target_is_flagged(self):
        game_id = await self._priced_wishlist_game(14.99)
        await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            target_price=19.99,
            price_currency="EUR",
        )
        result = await self._deals()
        entry = result["deals"][0]
        self.assertEqual(entry["assessment"]["target_price"], 19.99)
        self.assertTrue(entry["below_assessed_target"])

    async def test_deal_above_target_is_not_flagged(self):
        game_id = await self._priced_wishlist_game(29.99)
        await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            target_price=19.99,
            price_currency="EUR",
        )
        result = await self._deals()
        self.assertNotIn("below_assessed_target", result["deals"][0])

    async def test_a_price_in_another_currency_is_not_evidence(self):
        game_id = await self._priced_wishlist_game(14.99, currency="USD")
        await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            target_price=19.99,
            price_currency="EUR",
        )
        result = await self._deals()
        self.assertNotIn("below_assessed_target", result["deals"][0])


class AssessmentsReportTests(ToolDBTestCase):
    async def test_page_is_newest_first_with_totals(self):
        first = await seed_game("Report One")
        second = await seed_game("Report Two")
        await record_assessment(game_id=first, verdict="skip", assessed_at="2026-01-01")
        await record_assessment(
            game_id=second,
            verdict="buy_now",
            assessed_at="2026-02-01",
            summary="clear buy",
        )

        result = await main.get_stats(report="assessments", limit=1)
        self.assertEqual(result["report"], "assessments")
        self.assertEqual(len(result["assessments"]), 1)
        self.assertEqual(result["assessments"][0]["name"], "Report Two")
        self.assertEqual(result["assessments"][0]["summary"], "clear buy")
        self.assertEqual(result["total_matches"], 2)
        self.assertTrue(result["has_more"])

    async def test_verdict_filter(self):
        first = await seed_game("Filtered One")
        second = await seed_game("Filtered Two")
        await record_assessment(game_id=first, verdict="skip")
        await record_assessment(game_id=second, verdict="buy_now")

        result = await main.get_stats(report="assessments", verdict="skip")
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["assessments"][0]["game_id"], first)

    async def test_verdict_does_not_apply_to_other_reports(self):
        with self.assertRaises(ToolError) as ctx:
            await main.get_stats(report="spending", verdict="skip")
        self.assertIn("verdict", str(ctx.exception))

    async def test_reports_ownership_state(self):
        game_id = await seed_game("Owned Since")
        await record_assessment(game_id=game_id, verdict="skip")
        await add_platform(game_id, "steam", playtime_minutes=5)
        result = await main.get_stats(report="assessments")
        self.assertTrue(result["assessments"][0]["owned"])


class CalibrationReportTests(ToolDBTestCase):
    async def test_verdict_rates_and_mismatches(self):
        skipped_bought = await seed_game("Skipped But Bought")
        await record_assessment(
            game_id=skipped_bought, verdict="skip", assessed_at="2026-01-01"
        )
        await add_platform(skipped_bought, "steam", playtime_minutes=600)
        await add_rating(skipped_bought, "manual", 8.0, 8.0)

        skipped_clean = await seed_game("Skipped And Stayed Skipped")
        await record_assessment(
            game_id=skipped_clean, verdict="skip", assessed_at="2026-01-02"
        )

        bought_unplayed = await seed_game("Bought And Shelved")
        await record_assessment(
            game_id=bought_unplayed, verdict="buy_now", assessed_at="2026-02-01"
        )
        await add_platform(bought_unplayed, "steam", playtime_minutes=10)

        result = await main.get_stats(report="calibration")
        self.assertEqual(result["report"], "calibration")
        self.assertEqual(result["overall"]["total_assessments"], 3)
        self.assertEqual(result["overall"]["distinct_games"], 3)
        self.assertEqual(result["overall"]["by_verdict"], {"skip": 2, "buy_now": 1})

        by_verdict = {row["verdict"]: row for row in result["by_verdict"]}
        self.assertEqual(by_verdict["skip"]["unowned_at_assessment"], 2)
        self.assertEqual(by_verdict["skip"]["acquired_count"], 1)
        self.assertEqual(by_verdict["skip"]["acquired_pct"], 50.0)
        self.assertEqual(by_verdict["skip"]["played_count"], 1)
        self.assertEqual(by_verdict["skip"]["rated_count"], 1)
        self.assertEqual(by_verdict["skip"]["avg_rating"], 8.0)

        skip_mismatch = result["mismatches"]["skip_but_acquired"]
        self.assertEqual(skip_mismatch["count"], 1)
        self.assertFalse(skip_mismatch["truncated"])
        self.assertEqual(skip_mismatch["items"][0]["game_id"], skipped_bought)
        self.assertEqual(
            result["mismatches"]["buy_now_still_unplayed"]["items"][0]["game_id"],
            bought_unplayed,
        )

    async def test_a_game_reassessed_with_the_same_verdict_counts_once(self):
        game_id = await seed_game("Assessed Twice Same Call")
        await record_assessment(game_id=game_id, verdict="skip", assessed_at="2026-01-01")
        await record_assessment(game_id=game_id, verdict="skip", assessed_at="2026-02-01")

        result = await main.get_stats(report="calibration")
        by_verdict = {row["verdict"]: row for row in result["by_verdict"]}
        self.assertEqual(by_verdict["skip"]["games"], 1)
        self.assertEqual(by_verdict["skip"]["assessments"], 2)

    async def test_money_is_grouped_per_currency_and_never_summed(self):
        euro = await seed_game("Euro Candidate")
        await record_assessment(
            game_id=euro,
            verdict="wishlist_for_sale",
            price_seen=39.99,
            target_price=19.99,
            price_currency="EUR",
        )
        dollar = await seed_game("Dollar Candidate")
        await record_assessment(
            game_id=dollar,
            verdict="wishlist_for_sale",
            price_seen=29.99,
            target_price=9.99,
            price_currency="USD",
        )

        result = await main.get_stats(report="calibration")
        seen = {row["currency"]: row for row in result["wishlist_for_sale"]["price_seen"]}
        self.assertEqual(set(seen), {"EUR", "USD"})
        self.assertEqual(seen["EUR"]["average"], 39.99)
        self.assertEqual(seen["USD"]["average"], 29.99)

    async def test_within_target_counts_only_matching_currencies(self):
        hit = await seed_game("Bought Under Target")
        await record_assessment(
            game_id=hit,
            verdict="wishlist_for_sale",
            target_price=20.0,
            price_currency="EUR",
        )
        gpid = await add_platform(hit, "steam", playtime_minutes=0)
        await db_module.set_platform_acquisition(
            gpid, {"price_paid": 12.0, "price_currency": "EUR", "acquired_at": "2026-03-01"}
        )

        mismatch_currency = await seed_game("Bought In Another Currency")
        await record_assessment(
            game_id=mismatch_currency,
            verdict="wishlist_for_sale",
            target_price=20.0,
            price_currency="EUR",
        )
        other_gpid = await add_platform(mismatch_currency, "steam", playtime_minutes=0)
        await db_module.set_platform_acquisition(
            other_gpid,
            {"price_paid": 12.0, "price_currency": "USD", "acquired_at": "2026-03-01"},
        )

        result = await main.get_stats(report="calibration")
        acquired = {row["currency"]: row for row in result["wishlist_for_sale"]["acquired"]}
        self.assertEqual(acquired["EUR"]["within_target_count"], 1)
        self.assertEqual(acquired["USD"]["within_target_count"], 0)

    async def test_play_what_you_own_follow_through_treats_null_as_unknown(self):
        played_instead = await seed_game("The Owned Alternative")
        await add_platform(played_instead, "steam", playtime_minutes=300)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET last_played = '2026-07-01' WHERE game_id = ?",
                (played_instead,),
            )
            await db.commit()
        unknown_instead = await seed_game("Alternative With No Date")
        await add_platform(unknown_instead, "gog", playtime_minutes=None)

        followed = await seed_game("Pointed Elsewhere")
        await record_assessment(
            game_id=followed,
            verdict="play_what_you_own",
            assessed_at="2026-06-01",
            instead_game_id=played_instead,
        )
        unknown = await seed_game("Pointed Elsewhere Too")
        await record_assessment(
            game_id=unknown,
            verdict="play_what_you_own",
            assessed_at="2026-06-01",
            instead_game_id=unknown_instead,
        )

        follow_through = (await main.get_stats(report="calibration"))[
            "play_what_you_own_follow_through"
        ]
        self.assertEqual(follow_through["total"], 2)
        self.assertEqual(follow_through["followed_up_count"], 1)
        self.assertEqual(follow_through["unknown_count"], 1)
        self.assertEqual(follow_through["not_yet_count"], 0)

    async def test_empty_library_reports_zeros(self):
        result = await main.get_stats(report="calibration")
        self.assertEqual(result["overall"]["total_assessments"], 0)
        self.assertEqual(result["by_verdict"], [])
        self.assertEqual(result["mismatches"]["skip_but_acquired"]["count"], 0)


class AssessmentIntegrityTests(ToolDBTestCase):
    async def test_assessment_linked_row_is_not_an_orphan(self):
        assessed = await seed_game("Assessed But Unowned")
        await record_assessment(game_id=assessed, verdict="skip")
        true_orphan = await seed_game("Truly Dangling")

        result = await admin.detect_orphan_games()
        self.assertEqual([o["game_id"] for o in result["orphans"]], [true_orphan])
        self.assertEqual(result["assessment_only_count"], 1)

    async def test_check_library_surfaces_the_count(self):
        assessed = await seed_game("Assessed But Unowned")
        await record_assessment(game_id=assessed, verdict="skip")
        report = await main.check_library(checks=["ownership.orphan"])
        self.assertEqual(
            report["summary"]["ownership.orphan"]["assessment_only_count"], 1
        )

    async def test_merge_transfers_assessments_and_repoints_instead_links(self):
        source = await seed_game("Localized Row")
        target = await seed_game("English Row")
        pointer = await seed_game("Pointed At Source")

        await record_assessment(
            game_id=source, verdict="skip", assessed_at="2026-01-01"
        )
        await record_assessment(
            game_id=pointer,
            verdict="play_what_you_own",
            assessed_at="2026-02-01",
            instead_game_id=source,
        )

        result = await admin.merge_games(source, target)
        self.assertEqual(result["assessments_transferred"], 1)
        self.assertEqual(result["assessments_dropped"], 0)
        self.assertEqual(result["assessment_instead_links_repointed"], 1)

        moved = await _assessment_rows(target)
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["verdict"], "skip")
        pointer_rows = await _assessment_rows(pointer)
        self.assertEqual(pointer_rows[0]["instead_game_id"], target)

    async def test_merge_keeps_the_targets_row_on_a_same_day_collision(self):
        source = await seed_game("Source Row")
        target = await seed_game("Target Row")
        await record_assessment(
            game_id=source, verdict="skip", assessed_at="2026-01-01T08:00:00Z"
        )
        await record_assessment(
            game_id=target, verdict="buy_now", assessed_at="2026-01-01T20:00:00Z"
        )

        result = await admin.merge_games(source, target)
        self.assertEqual(result["assessments_transferred"], 0)
        self.assertEqual(result["assessments_dropped"], 1)

        rows = await _assessment_rows(target)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "buy_now")

    async def test_merge_dry_run_previews_the_same_counts(self):
        source = await seed_game("Preview Source")
        target = await seed_game("Preview Target")
        await record_assessment(game_id=source, verdict="skip", assessed_at="2026-01-01")

        preview = await admin.merge_games(source, target, dry_run=True)
        self.assertEqual(preview["assessments_transferred"], 1)
        self.assertEqual(len(await _assessment_rows(source)), 1)

        wet = await admin.merge_games(source, target)
        self.assertEqual(wet["assessments_transferred"], 1)

    async def test_delete_preview_counts_assessments_and_confirm_removes_them(self):
        game_id = await seed_game("Deleted With History")
        await record_assessment(game_id=game_id, verdict="skip")

        preview = await admin.delete_game(game_id=game_id)
        self.assertEqual(preview["would_delete"]["assessments"], 1)

        deleted = await admin.delete_game(game_id=game_id, confirm=True)
        self.assertEqual(deleted["deleted_counts"]["assessments"], 1)
        async with db_module.get_db() as db:
            remaining = await db.execute_fetchall("SELECT 1 FROM game_assessments")
        self.assertEqual(remaining, [])

    async def test_deleting_the_instead_target_keeps_the_assessment(self):
        alternative = await seed_game("Alternative")
        assessed = await seed_game("Assessed")
        await record_assessment(
            game_id=assessed, verdict="play_what_you_own", instead_game_id=alternative
        )
        await admin.delete_game(game_id=alternative, confirm=True)

        rows = await _assessment_rows(assessed)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["instead_game_id"])


if __name__ == "__main__":
    unittest.main()
