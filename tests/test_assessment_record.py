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
    _ordinal_near_miss,
    _sequel_near_miss,
    get_assessment_context,
    record_assessment,
    record_assessments_batch,
)

# Every single-item recording now assembles a package, whose one network step
# is the media fetch. Neutralized for the whole module so no test reaches a
# provider; the package tests below patch it again with what they need.
_MEDIA_PATCH = patch(
    "gamelib_mcp.tools.assessment.get_game_media", AsyncMock(return_value=None)
)


def setUpModule():
    _MEDIA_PATCH.start()


def tearDownModule():
    _MEDIA_PATCH.stop()


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
        self.assertEqual(
            result["resolution"],
            {
                "mode": "exact",
                "query": "Hollow Knight",
                "matched_name": "Hollow Knight",
            },
        )

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
        self.assertEqual(result["resolution"]["mode"], "minted")
        self.assertEqual(result["resolution"]["query"], "Some Unreleased Thing")

        rows = await _assessment_rows(result["game_id"])
        self.assertEqual(rows[0]["steam_appid"], 999001)
        # A minted candidate is ownership-free by design: no platform row is
        # invented for it, which is why the appid lives on the assessment.
        async with db_module.get_db() as db:
            platform_rows = await db.execute_fetchall(
                "SELECT 1 FROM game_platforms WHERE game_id = ?", (result["game_id"],)
            )
        self.assertEqual(platform_rows, [])

    async def test_minted_candidate_resolves_by_appid_on_a_repeat_ask(self):
        # get_game_by_appid searches only platform identifier rows, which a
        # minted (unowned) candidate never has — resolution falls back to the
        # appid the assessment itself carries, the same shape as the
        # wishlist's store_identifier path. Without it, the repeat ask below
        # reported not_found and re-asked for a name it had already been given.
        first = await record_assessment(
            name="Some Unreleased Thing",
            appid=999001,
            verdict="try_demo",
            assessed_at="2026-01-01",
        )

        again = await record_assessment(
            appid=999001, verdict="skip", assessed_at="2026-02-01"
        )
        self.assertFalse(again["created"])
        self.assertEqual(again["game_id"], first["game_id"])
        self.assertEqual(again["repeat_ask"]["previous_count"], 1)
        self.assertEqual(again["resolution"]["mode"], "by_assessed_appid")
        self.assertEqual(again["resolution"]["query"], "999001")
        self.assertEqual(again["resolution"]["matched_name"], "Some Unreleased Thing")

        context = await get_assessment_context(appid=999001)
        self.assertEqual(context["game_resolution"], "resolved")
        self.assertEqual(
            [a["verdict"] for a in context["past_assessments"]],
            ["skip", "try_demo"],
        )

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


class OrdinalNearMissTests(unittest.TestCase):
    """The pure guard behind get_assessment_context's sequel rejection (#150)."""

    def test_trailing_arabic_ordinal_in_both_directions(self):
        self.assertTrue(_ordinal_near_miss("alan wake 2", "alan wake"))
        self.assertTrue(_ordinal_near_miss("alan wake", "alan wake 2"))

    def test_a_lone_trailing_character_counts(self):
        self.assertTrue(_ordinal_near_miss("silent hill f", "silent hill"))

    def test_trailing_roman_numeral_counts(self):
        self.assertTrue(_ordinal_near_miss("diablo iv", "diablo"))

    def test_two_different_ordinals_are_not_a_near_miss_by_this_rule(self):
        # Neither token list is the other plus a trailing token, so this
        # function honestly returns False — and the tiers MISS the pair, which
        # is what exposes it to the fuzzy fallback. _sequel_near_miss unions
        # in titles_conflict_on_identity to reject exactly that shape.
        self.assertFalse(_ordinal_near_miss("final fantasy vii", "final fantasy viii"))

    def test_a_genuine_typo_is_not_an_ordinal_difference(self):
        self.assertFalse(_ordinal_near_miss("hollow knigt", "hollow knight"))

    def test_a_multi_token_subtitle_is_not_an_ordinal(self):
        self.assertFalse(_ordinal_near_miss("the witcher 3", "the witcher 3 wild hunt"))


class SequelNearMissTests(unittest.TestCase):
    """The union guard: _ordinal_near_miss OR titles_conflict_on_identity."""

    def test_differing_ordinals_in_place_are_rejected(self):
        # The Codex-review case: equal-length titles, disagreeing ordinals —
        # tiers miss, fuzzy scores ~97, identity comparison rejects.
        self.assertTrue(_sequel_near_miss("final fantasy viii", "final fantasy vii"))
        self.assertTrue(_sequel_near_miss("final fantasy vii", "final fantasy viii"))

    def test_added_trailing_ordinal_still_rejected(self):
        self.assertTrue(_sequel_near_miss("alan wake 2", "alan wake"))

    def test_lone_trailing_character_still_rejected(self):
        # No digit, no roman numeral — only _ordinal_near_miss sees this one.
        self.assertTrue(_sequel_near_miss("silent hill f", "silent hill"))

    def test_a_genuine_typo_still_passes(self):
        self.assertFalse(_sequel_near_miss("hollow knigt", "hollow knight"))

    def test_a_subtitle_with_the_same_ordinal_still_passes(self):
        self.assertFalse(_sequel_near_miss("the witcher 3", "the witcher 3 wild hunt"))


class AssessmentNameResolutionTests(ToolDBTestCase):
    """Issue #150: writes never resolve a name loosely; reads guard the sequel."""

    async def test_a_sequel_name_mints_instead_of_attaching_to_the_predecessor(self):
        predecessor = await seed_game("Alan Wake")

        result = await record_assessment(name="Alan Wake 2", verdict="buy_now")

        self.assertTrue(result["created"])
        self.assertNotEqual(result["game_id"], predecessor)
        self.assertEqual(result["resolution"]["mode"], "minted")
        self.assertEqual(result["resolution"]["matched_name"], "Alan Wake 2")
        # The predecessor must carry no verdict at all — the silent misfile.
        self.assertEqual(await _assessment_rows(predecessor), [])

    async def test_a_near_miss_name_is_never_adopted_by_the_write_path(self):
        # Not an ordinal difference, just close: the write path mints anyway,
        # because ANY loose match on a write is unverifiable by the caller.
        existing = await seed_game("Hollow Knight")
        result = await record_assessment(name="Hollow Knigt", verdict="skip")
        self.assertTrue(result["created"])
        self.assertNotEqual(result["game_id"], existing)

    async def test_context_rejects_the_sequel_shaped_match(self):
        await seed_game("Alan Wake")
        context = await get_assessment_context(name="Alan Wake 2")

        self.assertEqual(context["game_resolution"], "not_found")
        self.assertNotIn("game", context)
        self.assertEqual(context["resolution"]["mode"], "none")
        self.assertEqual(context["resolution"]["query"], "Alan Wake 2")
        self.assertEqual(context["resolution"]["rejected_near_miss"], "Alan Wake")
        self.assertNotIn("matched_name", context["resolution"])

    async def test_context_rejects_the_reverse_direction_too(self):
        await seed_game("Alan Wake 2")
        context = await get_assessment_context(name="Alan Wake")

        self.assertEqual(context["game_resolution"], "not_found")
        self.assertEqual(context["resolution"]["rejected_near_miss"], "Alan Wake 2")

    async def test_context_rejects_disagreeing_ordinals_in_place(self):
        # Equal-length sequels: the tiers miss ("viii" is no token-AND hit for
        # "vii"), the fuzzy fallback scores ~97, and the identity comparison
        # in _sequel_near_miss refuses the wrong sequel (Codex review, PR #152).
        await seed_game("Final Fantasy VII")
        context = await get_assessment_context(name="Final Fantasy VIII")

        self.assertEqual(context["game_resolution"], "not_found")
        self.assertEqual(context["resolution"]["mode"], "none")
        self.assertEqual(
            context["resolution"]["rejected_near_miss"], "Final Fantasy VII"
        )

    async def test_context_still_resolves_a_genuine_typo_fuzzily(self):
        game_id = await seed_game("Hollow Knight")
        context = await get_assessment_context(name="Hollow Knigt")

        self.assertEqual(context["game_resolution"], "resolved")
        self.assertEqual(context["game"]["game_id"], game_id)
        self.assertEqual(context["resolution"]["mode"], "fuzzy")
        self.assertEqual(context["resolution"]["matched_name"], "Hollow Knight")

    async def test_context_reports_exact_and_by_id_modes(self):
        game_id = await seed_game("Celeste")

        exact = await get_assessment_context(name="Celeste")
        self.assertEqual(exact["resolution"]["mode"], "exact")
        self.assertEqual(exact["resolution"]["matched_name"], "Celeste")

        by_id = await get_assessment_context(game_id=game_id)
        self.assertEqual(by_id["resolution"]["mode"], "by_id")
        self.assertEqual(by_id["resolution"]["query"], str(game_id))

    async def test_context_reports_a_partial_match(self):
        await seed_game("Sekiro Shadows Die Twice")
        context = await get_assessment_context(name="Sekiro")
        self.assertEqual(context["resolution"]["mode"], "partial")
        self.assertEqual(
            context["resolution"]["matched_name"], "Sekiro Shadows Die Twice"
        )

    async def test_context_reports_none_for_an_unknown_game_id(self):
        context = await get_assessment_context(game_id=424242)
        self.assertEqual(context["game_resolution"], "not_found")
        self.assertEqual(context["resolution"], {"mode": "none", "query": "424242"})


class VoidAssessmentTests(ToolDBTestCase):
    async def test_void_deletes_the_row_and_reports_it(self):
        game_id = await seed_game("Misfiled Onto Me")
        await add_platform(game_id, "steam", playtime_minutes=30)
        recorded = await record_assessment(
            game_id=game_id, verdict="buy_now", assessed_at="2026-03-01"
        )

        result = await record_assessment(
            void_assessment_id=recorded["assessment_id"]
        )

        self.assertTrue(result["voided"])
        self.assertEqual(result["assessment_id"], recorded["assessment_id"])
        self.assertEqual(result["game_id"], game_id)
        self.assertEqual(result["name"], "Misfiled Onto Me")
        self.assertEqual(result["verdict"], "buy_now")
        self.assertTrue(result["assessed_at"].startswith("2026-03-01"))
        self.assertEqual(await _assessment_rows(game_id), [])
        # An owned row is not stranded by the void.
        self.assertNotIn("suggested_action", result)

    async def test_voiding_the_last_assessment_of_a_minted_row_suggests_delete(self):
        minted = await record_assessment(name="Phantom Candidate", verdict="skip")

        result = await record_assessment(void_assessment_id=minted["assessment_id"])

        self.assertEqual(result["suggested_action"]["tool"], "delete_game")
        self.assertEqual(
            result["suggested_action"]["args"],
            {"game_id": minted["game_id"], "confirm": False},
        )

    async def test_one_of_several_assessments_leaves_the_row_alone(self):
        minted = await record_assessment(
            name="Assessed Twice", verdict="skip", assessed_at="2026-01-01"
        )
        second = await record_assessment(
            game_id=minted["game_id"], verdict="buy_now", assessed_at="2026-02-01"
        )

        result = await record_assessment(void_assessment_id=second["assessment_id"])
        self.assertNotIn("suggested_action", result)
        rows = await _assessment_rows(minted["game_id"])
        self.assertEqual([row["verdict"] for row in rows], ["skip"])

    async def test_unknown_assessment_id_errors(self):
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(void_assessment_id=987654)
        self.assertIn("987654", str(ctx.exception))

    async def test_void_is_exclusive_of_every_other_parameter(self):
        game_id = await seed_game("Exclusive Probe")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        for kwargs in (
            {"verdict": "skip"},
            {"game_id": game_id},
            {"name": "Exclusive Probe"},
            {"appid": 4242},
            {"summary": "leftover"},
            # The presentation params are refused alongside every other one:
            # a void deletes a row, it does not re-author one.
            {"elevator_pitch": "leftover pitch"},
            {"for_you_if": ["leftover"]},
            {"not_for_you_if": ["leftover"]},
            {"comparisons": [{"name": "X", "relation": "similar"}]},
        ):
            with self.subTest(**kwargs), self.assertRaises(ToolError) as ctx:
                await record_assessment(
                    void_assessment_id=recorded["assessment_id"], **kwargs
                )
            self.assertIn(next(iter(kwargs)), str(ctx.exception))
        # Nothing was deleted by any of the refused calls.
        self.assertEqual(len(await _assessment_rows(game_id)), 1)

    async def test_void_cannot_be_combined_with_items(self):
        game_id = await seed_game("Bulk And Void")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        with self.assertRaises(ToolError) as ctx:
            await main.record_assessment(
                void_assessment_id=recorded["assessment_id"],
                items=[{"game_id": game_id, "verdict": "buy_now"}],
            )
        self.assertIn("items", str(ctx.exception))
        self.assertEqual(len(await _assessment_rows(game_id)), 1)

    async def test_void_routes_through_the_tool(self):
        game_id = await seed_game("Routed Void")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        result = await main.record_assessment(
            void_assessment_id=recorded["assessment_id"]
        )
        self.assertTrue(result["voided"])
        self.assertEqual(await _assessment_rows(game_id), [])

    async def test_void_returns_no_package(self):
        game_id = await seed_game("Void Package")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        result = await record_assessment(void_assessment_id=recorded["assessment_id"])
        self.assertNotIn("package", result)


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


class AssessmentProvenanceTests(ToolDBTestCase):
    """Declared-only methodology provenance (issue #153).

    The rule under test everywhere here: the server records what the client
    CLAIMED and nothing else. It never stamps a skill version or a model of
    its own, so an omitted value stays NULL — "unknown", not "no skill".
    """

    async def test_round_trip_normalizes_without_inventing_a_vocabulary(self):
        game_id = await seed_game("Provenance Round Trip")
        await main.record_assessment(
            game_id=game_id,
            verdict="skip",
            skill="  Game-Quality  ",
            # Version case is preserved: a version string is not a name.
            skill_version=" 2.4.0-RC1 ",
            model=" Claude-Opus-5 ",
        )
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["skill"], "game-quality")
        self.assertEqual(row["skill_version"], "2.4.0-RC1")
        self.assertEqual(row["model"], "claude-opus-5")

    async def test_nothing_is_stamped_when_the_caller_declares_nothing(self):
        game_id = await seed_game("Silent Recorder")
        await record_assessment(game_id=game_id, verdict="skip")
        (row,) = await _assessment_rows(game_id)
        self.assertIsNone(row["skill"])
        self.assertIsNone(row["skill_version"])
        self.assertIsNone(row["model"])

    async def test_blank_declarations_are_null_not_empty_strings(self):
        game_id = await seed_game("Blank Declaration")
        await record_assessment(
            game_id=game_id, verdict="skip", skill="", skill_version="   ", model=""
        )
        (row,) = await _assessment_rows(game_id)
        self.assertIsNone(row["skill"])
        self.assertIsNone(row["skill_version"])
        self.assertIsNone(row["model"])

    async def test_over_cap_values_are_rejected_not_truncated(self):
        # Identifiers, not prose: a silently shortened model id would group
        # calibration under a name nothing ever declared.
        game_id = await seed_game("Cap Declaration")
        for kwargs in (
            {"model": "m" * 65},
            {"skill": "s" * 65},
            {"skill_version": "v" * 33},
        ):
            with self.subTest(**kwargs), self.assertRaises(ToolError) as ctx:
                await record_assessment(game_id=game_id, verdict="skip", **kwargs)
            self.assertIn("at most", str(ctx.exception))
        self.assertEqual(await _assessment_rows(game_id), [])

    async def test_same_day_rerecord_updates_the_declared_methodology(self):
        game_id = await seed_game("Version Bump")
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-08-10",
            skill="game-quality",
            skill_version="2.3.1",
            model="claude-opus-5",
        )
        result = await record_assessment(
            game_id=game_id,
            verdict="buy_now",
            assessed_at="2026-08-10",
            skill="game-quality",
            skill_version="2.4.0",
            model="gpt-5",
        )
        self.assertTrue(result["replaced"])
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["skill_version"], "2.4.0")
        self.assertEqual(row["model"], "gpt-5")

    async def test_bulk_items_accept_the_provenance_keys(self):
        game_id = await seed_game("Bulk Provenance")
        result = await record_assessments_batch(
            [
                {
                    "game_id": game_id,
                    "verdict": "skip",
                    "skill": "game-quality",
                    "skill_version": "2.4.0",
                    "model": "claude-opus-5",
                }
            ]
        )
        self.assertEqual(result["ok"], 1)
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["skill"], "game-quality")

    async def test_void_mode_rejects_provenance_like_every_other_param(self):
        game_id = await seed_game("Void Provenance")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        for kwargs in (
            {"skill": "game-quality"},
            {"skill_version": "2.4.0"},
            {"model": "claude-opus-5"},
        ):
            with self.subTest(**kwargs), self.assertRaises(ToolError) as ctx:
                await record_assessment(
                    void_assessment_id=recorded["assessment_id"], **kwargs
                )
            self.assertIn("exclusive", str(ctx.exception))
        # The row survived every refusal.
        self.assertEqual(len(await _assessment_rows(game_id)), 1)

    async def test_read_blocks_carry_the_declared_methodology(self):
        game_id = await make_steam_game("Provenance Reader", 4242)
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            skill="game-quality",
            skill_version="2.4.0",
            model="claude-opus-5",
        )

        detail = await main.get_game_detail(game_id=game_id)
        self.assertEqual(detail["assessments"][0]["skill"], "game-quality")
        self.assertEqual(detail["assessments"][0]["skill_version"], "2.4.0")
        self.assertEqual(detail["assessments"][0]["model"], "claude-opus-5")

        context = await get_assessment_context(game_id=game_id)
        self.assertEqual(context["past_assessments"][0]["skill_version"], "2.4.0")

        report = await main.get_stats(report="assessments")
        self.assertEqual(report["assessments"][0]["skill"], "game-quality")
        self.assertEqual(report["assessments"][0]["model"], "claude-opus-5")

    async def test_unstated_methodology_reads_back_as_null(self):
        game_id = await seed_game("Unstated Reader")
        await record_assessment(game_id=game_id, verdict="skip")
        report = await main.get_stats(report="assessments")
        self.assertIsNone(report["assessments"][0]["skill"])
        self.assertIsNone(report["assessments"][0]["model"])


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
        # assessment_id makes the block a usable source for
        # record_assessment(void_assessment_id=…) on a historical misfile.
        self.assertIsInstance(detail["assessments"][0]["assessment_id"], int)

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
        recorded = await record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            assessed_at="2026-05-01",
            target_price=20.0,
        )
        context = await get_assessment_context(game_id=game_id)
        self.assertEqual(context["past_assessment_count"], 1)
        self.assertFalse(context["past_assessments_truncated"])
        self.assertEqual(context["past_assessments"][0]["verdict"], "wishlist_for_sale")
        self.assertEqual(
            context["past_assessments"][0]["assessment_id"],
            recorded["assessment_id"],
        )

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

    async def test_a_price_with_unknown_currency_is_not_evidence_either(self):
        # A cached/provider row without currency metadata can't prove a EUR
        # target was reached — only an exact currency match counts once the
        # assessment names one.
        game_id = await self._priced_wishlist_game(14.99, currency=None)
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

    async def test_report_carries_the_assessment_id_for_void(self):
        # The browse report is the advertised way to recover the id a
        # historical misfile needs for record_assessment(void_assessment_id=…).
        game_id = await seed_game("Voidable Later")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        result = await main.get_stats(report="assessments")
        self.assertEqual(
            result["assessments"][0]["assessment_id"], recorded["assessment_id"]
        )

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
        self.assertEqual(result["by_methodology"]["items"], [])
        self.assertEqual(result["by_model"]["count"], 0)


class CalibrationProvenanceTests(ToolDBTestCase):
    """by_methodology / by_model — how each declared methodology's calls held up."""

    async def _seed_two_versions_and_models(self) -> None:
        # Old skill version, said "buy" — bought and played.
        old_hit = await seed_game("Old Version Hit")
        await record_assessment(
            game_id=old_hit,
            verdict="buy_now",
            assessed_at="2026-01-01",
            skill="game-quality",
            skill_version="2.3.1",
            model="claude-opus-5",
        )
        await add_platform(old_hit, "steam", playtime_minutes=600)
        await add_rating(old_hit, "manual", 9.0, 9.0)

        # Same old version, said "buy" — still not bought.
        old_miss = await seed_game("Old Version Miss")
        await record_assessment(
            game_id=old_miss,
            verdict="buy_now",
            assessed_at="2026-01-02",
            skill="game-quality",
            skill_version="2.3.1",
            model="claude-opus-5",
        )

        # New skill version, a different model.
        new_call = await seed_game("New Version Call")
        await record_assessment(
            game_id=new_call,
            verdict="skip",
            assessed_at="2026-03-01",
            skill="game-quality",
            skill_version="2.4.0",
            model="gpt-5",
        )

        # Declared nothing at all — the unknown bucket.
        unversioned = await seed_game("Unversioned History")
        await record_assessment(
            game_id=unversioned, verdict="skip", assessed_at="2026-02-01"
        )

    async def test_groups_by_skill_version_and_keeps_the_null_bucket(self):
        await self._seed_two_versions_and_models()
        result = await main.get_stats(report="calibration")

        block = result["by_methodology"]
        self.assertEqual(block["count"], 3)
        self.assertFalse(block["truncated"])
        # Newest last-assessed first: 2.4.0 (March), unknown (Feb), 2.3.1 (Jan).
        self.assertEqual(
            [entry["skill_version"] for entry in block["items"]],
            ["2.4.0", None, "2.3.1"],
        )

        by_version = {entry["skill_version"]: entry for entry in block["items"]}
        old = by_version["2.3.1"]
        self.assertEqual(old["skill"], "game-quality")
        self.assertEqual(old["assessments"], 2)
        self.assertEqual(old["distinct_games"], 2)
        self.assertEqual(old["first_assessed_at"][:10], "2026-01-01")
        self.assertEqual(old["last_assessed_at"][:10], "2026-01-02")
        # Denominator + funnel rates (Codex review, PR #154): without
        # unowned_at_assessment, acquired_count=0 can't distinguish "ignored
        # every recommendation" from "nothing was unowned to begin with".
        self.assertEqual(old["unowned_at_assessment"], 2)
        self.assertEqual(old["acquired_count"], 1)
        self.assertEqual(old["acquired_pct"], 50.0)
        self.assertEqual(old["played_count"], 1)
        self.assertEqual(old["played_pct"], 100.0)
        self.assertEqual(old["rated_count"], 1)
        self.assertEqual(old["rated_pct"], 100.0)
        self.assertEqual(old["avg_rating"], 9.0)

        # The unknown bucket is reported with explicit nulls, never dropped —
        # unversioned history is still history.
        unknown = by_version[None]
        self.assertIsNone(unknown["skill"])
        self.assertEqual(unknown["assessments"], 1)
        self.assertEqual(unknown["unowned_at_assessment"], 1)
        self.assertEqual(unknown["acquired_count"], 0)
        self.assertEqual(unknown["acquired_pct"], 0.0)
        # Nothing acquired → the played/rated funnel steps have no
        # denominator, and None (not 0.0) is the honest value.
        self.assertIsNone(unknown["played_pct"])
        self.assertIsNone(unknown["rated_pct"])
        self.assertIsNone(unknown["avg_rating"])

    async def test_groups_by_model_including_the_unknown_bucket(self):
        await self._seed_two_versions_and_models()
        result = await main.get_stats(report="calibration")

        block = result["by_model"]
        self.assertEqual(block["count"], 3)
        self.assertEqual(
            [entry["model"] for entry in block["items"]],
            ["gpt-5", None, "claude-opus-5"],
        )
        by_model = {entry["model"]: entry for entry in block["items"]}
        self.assertEqual(by_model["claude-opus-5"]["assessments"], 2)
        self.assertEqual(by_model["claude-opus-5"]["played_count"], 1)
        self.assertEqual(by_model["gpt-5"]["distinct_games"], 1)

    async def test_a_game_reassessed_with_the_same_verdict_counts_once(self):
        # Same dedupe the rest of the report uses: one row per (game, verdict),
        # the most recent — so provenance follows the surviving row.
        game_id = await seed_game("Reassessed Same Call")
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-01-01",
            skill_version="2.3.1",
        )
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-02-01",
            skill_version="2.4.0",
        )
        result = await main.get_stats(report="calibration")
        block = result["by_methodology"]
        self.assertEqual(block["count"], 1)
        self.assertEqual(block["items"][0]["skill_version"], "2.4.0")
        self.assertEqual(block["items"][0]["assessments"], 1)

    async def test_both_blocks_are_capped_with_a_true_count_and_flag(self):
        for index in range(4):
            game_id = await seed_game(f"Capped Provenance {index}")
            await record_assessment(
                game_id=game_id,
                verdict="skip",
                assessed_at=f"2026-0{index + 1}-01",
                skill="game-quality",
                skill_version=f"2.{index}.0",
                model=f"model-{index}",
            )
        result = await main.get_stats(report="calibration", limit=2)
        for key in ("by_methodology", "by_model"):
            with self.subTest(block=key):
                block = result[key]
                self.assertEqual(len(block["items"]), 2)
                self.assertEqual(block["count"], 4)
                self.assertTrue(block["truncated"])
        # The cap keeps the newest-first head, not an arbitrary slice.
        self.assertEqual(
            [entry["skill_version"] for entry in result["by_methodology"]["items"]],
            ["2.3.0", "2.2.0"],
        )


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


class PresentationFieldTests(ToolDBTestCase):
    """The model-authored half of a verdict: validated, capped, stored as one JSON."""

    async def test_round_trip_into_one_presentation_column(self):
        game_id = await seed_game("Presented")
        result = await main.record_assessment(
            game_id=game_id,
            verdict="buy_now",
            elevator_pitch="A nail, a kingdom, and no map.",
            for_you_if=["you put 244h into Hollow Knight"],
            not_for_you_if=["you abandoned both metroidvanias you tried"],
            comparisons=[
                {
                    "name": "Ori and the Blind Forest",
                    "relation": "similar",
                    "note": "gentler, prettier",
                }
            ],
        )

        (row,) = await _assessment_rows(game_id)
        self.assertEqual(
            json.loads(row["presentation"]),
            {
                "elevator_pitch": "A nail, a kingdom, and no map.",
                "for_you_if": ["you put 244h into Hollow Knight"],
                "not_for_you_if": ["you abandoned both metroidvanias you tried"],
                "comparisons": [
                    {
                        "name": "Ori and the Blind Forest",
                        "relation": "similar",
                        "note": "gentler, prettier",
                    }
                ],
            },
        )
        # The package echoes back what was stored (comparisons get their own,
        # library-annotated block).
        self.assertEqual(
            result["package"]["presentation"],
            {
                "elevator_pitch": "A nail, a kingdom, and no map.",
                "for_you_if": ["you put 244h into Hollow Knight"],
                "not_for_you_if": ["you abandoned both metroidvanias you tried"],
                # Stable shape: the echo carries every authored member, null
                # for the ones this recording didn't write.
                "why_care": None,
                "craft_note": None,
            },
        )

    async def test_column_stays_null_when_nothing_was_authored(self):
        game_id = await seed_game("Unpresented")
        result = await record_assessment(game_id=game_id, verdict="skip")
        (row,) = await _assessment_rows(game_id)
        self.assertIsNone(row["presentation"])
        self.assertIsNone(result["package"]["presentation"])

    async def test_only_authored_members_are_stored(self):
        game_id = await seed_game("Partial Presentation")
        await record_assessment(
            game_id=game_id, verdict="skip", for_you_if=["you like short games"]
        )
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(
            json.loads(row["presentation"]), {"for_you_if": ["you like short games"]}
        )

    async def test_a_same_day_rerecord_replaces_the_presentation(self):
        game_id = await seed_game("Revised Pitch")
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-08-20",
            elevator_pitch="first take",
        )
        await record_assessment(
            game_id=game_id,
            verdict="buy_now",
            assessed_at="2026-08-20",
            elevator_pitch="second take",
        )
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(json.loads(row["presentation"])["elevator_pitch"], "second take")

    async def test_a_same_day_rerecord_without_presentation_clears_it(self):
        # The day's row describes the verdict that now stands, exactly as the
        # provenance columns do.
        game_id = await seed_game("Dropped Pitch")
        await record_assessment(
            game_id=game_id,
            verdict="skip",
            assessed_at="2026-08-20",
            elevator_pitch="first take",
        )
        await record_assessment(
            game_id=game_id, verdict="skip", assessed_at="2026-08-20"
        )
        (row,) = await _assessment_rows(game_id)
        self.assertIsNone(row["presentation"])

    async def test_long_prose_is_truncated_and_over_cap_lists_rejected(self):
        game_id = await seed_game("Presentation Caps")
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id, verdict="skip", for_you_if=[f"b{i}" for i in range(5)]
            )
        self.assertIn("at most 4", str(ctx.exception))
        with self.assertRaises(ToolError):
            await record_assessment(
                game_id=game_id,
                verdict="skip",
                not_for_you_if=[f"b{i}" for i in range(5)],
            )
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id,
                verdict="skip",
                comparisons=[
                    {"name": f"G{i}", "relation": "similar"} for i in range(7)
                ],
            )
        self.assertIn("at most 6", str(ctx.exception))
        # Nothing was written by any of the refused calls.
        self.assertEqual(await _assessment_rows(game_id), [])

        await record_assessment(
            game_id=game_id,
            verdict="skip",
            elevator_pitch="p" * 500,
            for_you_if=["b" * 300],
            comparisons=[{"name": "n" * 200, "relation": "similar", "note": "x" * 300}],
        )
        stored = json.loads((await _assessment_rows(game_id))[0]["presentation"])
        self.assertEqual(len(stored["elevator_pitch"]), 420)
        self.assertEqual(len(stored["for_you_if"][0]), 200)
        self.assertEqual(len(stored["comparisons"][0]["name"]), 120)
        self.assertEqual(len(stored["comparisons"][0]["note"]), 200)

    async def test_malformed_presentation_entries_are_named(self):
        game_id = await seed_game("Presentation Shapes")
        cases = [
            ({"elevator_pitch": ["not a string"]}, "elevator_pitch"),
            ({"for_you_if": "not a list"}, "for_you_if"),
            ({"for_you_if": [""]}, "for_you_if"),
            ({"comparisons": ["Ori"]}, "comparisons"),
            ({"comparisons": [{"relation": "similar"}]}, "name"),
            ({"comparisons": [{"name": "Ori", "relation": "inspired_by"}]}, "relation"),
            ({"comparisons": [{"name": "Ori", "relation": "similar", "why": "x"}]}, "why"),
            (
                {"comparisons": [{"name": "Ori", "relation": "similar", "game_id": "3"}]},
                "game_id",
            ),
        ]
        for kwargs, expected in cases:
            with self.subTest(**kwargs), self.assertRaises(ToolError) as ctx:
                await record_assessment(game_id=game_id, verdict="skip", **kwargs)
            self.assertIn(expected, str(ctx.exception))
        self.assertEqual(await _assessment_rows(game_id), [])

    async def test_presentation_params_are_accepted_as_item_keys(self):
        game_id = await seed_game("Bulk Presentation")
        result = await main.record_assessment(
            items=[
                {
                    "game_id": game_id,
                    "verdict": "skip",
                    "elevator_pitch": "bulk pitch",
                    "comparisons": [{"name": "Ori", "relation": "ancestor"}],
                }
            ]
        )
        self.assertEqual(result["ok"], 1)
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(json.loads(row["presentation"])["elevator_pitch"], "bulk pitch")


class WhyCareTests(ToolDBTestCase):
    """why_care: the editorial half of the pedigree pair (issue #159).

    It rides inside the same `presentation` JSON column as the rest of the
    authored half — the column is free-form by design, so this needed no
    migration — and is validated exactly like `comparisons`: closed kind
    vocabulary, list rejected over its cap, text truncated.
    """

    async def test_round_trip_and_package_echo(self):
        game_id = await seed_game("Why Care")
        result = await main.record_assessment(
            game_id=game_id,
            verdict="buy_now",
            elevator_pitch="A nail, a kingdom, and no map.",
            why_care=[
                {"kind": "people", "text": "The Hollow Knight combat lead directs it"},
                {"kind": "anticipation", "text": "Seven years after the last one"},
            ],
        )

        (row,) = await _assessment_rows(game_id)
        stored = json.loads(row["presentation"])
        self.assertEqual(
            stored["why_care"],
            [
                {"kind": "people", "text": "The Hollow Knight combat lead directs it"},
                {"kind": "anticipation", "text": "Seven years after the last one"},
            ],
        )
        self.assertEqual(
            result["package"]["presentation"]["why_care"], stored["why_care"]
        )

    async def test_the_echo_is_null_rather_than_absent_when_unauthored(self):
        # Stable shape: a card reading package.presentation.why_care must not
        # have to tell "not authored" from "this server is older".
        game_id = await seed_game("No Why Care")
        result = await record_assessment(
            game_id=game_id, verdict="skip", elevator_pitch="just a pitch"
        )
        self.assertIsNone(result["package"]["presentation"]["why_care"])
        self.assertNotIn("why_care", json.loads((await _assessment_rows(game_id))[0]["presentation"]))

    async def test_kinds_are_a_closed_vocabulary(self):
        game_id = await seed_game("Why Care Kinds")
        for day, kind in enumerate(("people", "studio", "anticipation", "moment"), 1):
            with self.subTest(kind=kind):
                await record_assessment(
                    game_id=game_id,
                    verdict="skip",
                    assessed_at=f"2026-07-{day:02d}",
                    why_care=[{"kind": kind, "text": "a sourceable line"}],
                )
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id,
                verdict="skip",
                why_care=[{"kind": "vibes", "text": "trust me"}],
            )
        self.assertIn("vibes", str(ctx.exception))

    async def test_malformed_entries_are_named_and_nothing_is_written(self):
        game_id = await seed_game("Why Care Shapes")
        cases = [
            ({"why_care": "not a list"}, "why_care"),
            ({"why_care": ["a line"]}, "why_care"),
            ({"why_care": [{"text": "no kind"}]}, "kind"),
            ({"why_care": [{"kind": "studio"}]}, "text"),
            ({"why_care": [{"kind": "studio", "text": "  "}]}, "text"),
            ({"why_care": [{"kind": "studio", "text": "x", "source": "wiki"}]}, "source"),
        ]
        for kwargs, expected in cases:
            with self.subTest(**kwargs), self.assertRaises(ToolError) as ctx:
                await record_assessment(game_id=game_id, verdict="skip", **kwargs)
            self.assertIn(expected, str(ctx.exception))
        self.assertEqual(await _assessment_rows(game_id), [])

    async def test_over_cap_is_rejected_and_long_text_truncated(self):
        game_id = await seed_game("Why Care Caps")
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id,
                verdict="skip",
                why_care=[{"kind": "moment", "text": f"line {i}"} for i in range(4)],
            )
        self.assertIn("at most 3", str(ctx.exception))
        self.assertEqual(await _assessment_rows(game_id), [])

        await record_assessment(
            game_id=game_id, verdict="skip", why_care=[{"kind": "studio", "text": "t" * 400}]
        )
        stored = json.loads((await _assessment_rows(game_id))[0]["presentation"])
        self.assertEqual(len(stored["why_care"][0]["text"]), 160)

    async def test_void_refuses_to_carry_why_care(self):
        game_id = await seed_game("Why Care Void")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                void_assessment_id=recorded["assessment_id"],
                why_care=[{"kind": "studio", "text": "leftover"}],
            )
        self.assertIn("why_care", str(ctx.exception))
        self.assertEqual(len(await _assessment_rows(game_id)), 1)

    async def test_why_care_is_an_item_key(self):
        game_id = await seed_game("Bulk Why Care")
        result = await main.record_assessment(
            items=[
                {
                    "game_id": game_id,
                    "verdict": "skip",
                    "why_care": [{"kind": "studio", "text": "their first in a decade"}],
                }
            ]
        )
        self.assertEqual(result["ok"], 1)
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(
            json.loads(row["presentation"])["why_care"],
            [{"kind": "studio", "text": "their first in a decade"}],
        )


class CraftNoteTests(ToolDBTestCase):
    """craft_note: one line of craft context the score chips can't carry.

    Same member of the same free-form `presentation` JSON column as the pitch,
    and validated the same way — prose, so truncated rather than rejected.
    """

    async def test_round_trip_and_package_echo(self):
        game_id = await seed_game("Craft Noted")
        result = await main.record_assessment(
            game_id=game_id,
            verdict="wishlist_for_sale",
            craft_note="Wide critic spread (IGN 9, Game Informer 7); the knock is filler.",
        )

        (row,) = await _assessment_rows(game_id)
        self.assertEqual(
            json.loads(row["presentation"])["craft_note"],
            "Wide critic spread (IGN 9, Game Informer 7); the knock is filler.",
        )
        self.assertEqual(
            result["package"]["presentation"]["craft_note"],
            "Wide critic spread (IGN 9, Game Informer 7); the knock is filler.",
        )

    async def test_the_echo_is_null_rather_than_absent_when_unauthored(self):
        game_id = await seed_game("No Craft Note")
        result = await record_assessment(
            game_id=game_id, verdict="skip", elevator_pitch="just a pitch"
        )
        self.assertIsNone(result["package"]["presentation"]["craft_note"])
        self.assertNotIn(
            "craft_note", json.loads((await _assessment_rows(game_id))[0]["presentation"])
        )

    async def test_long_prose_is_truncated_and_a_non_string_rejected(self):
        game_id = await seed_game("Craft Note Caps")
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                game_id=game_id, verdict="skip", craft_note=["not a string"]
            )
        self.assertIn("craft_note", str(ctx.exception))
        self.assertEqual(await _assessment_rows(game_id), [])

        await record_assessment(game_id=game_id, verdict="skip", craft_note="c" * 400)
        stored = json.loads((await _assessment_rows(game_id))[0]["presentation"])
        self.assertEqual(len(stored["craft_note"]), 200)

    async def test_void_refuses_to_carry_a_craft_note(self):
        game_id = await seed_game("Craft Note Void")
        recorded = await record_assessment(game_id=game_id, verdict="skip")
        with self.assertRaises(ToolError) as ctx:
            await record_assessment(
                void_assessment_id=recorded["assessment_id"], craft_note="leftover"
            )
        self.assertIn("craft_note", str(ctx.exception))
        self.assertEqual(len(await _assessment_rows(game_id)), 1)

    async def test_craft_note_is_an_item_key(self):
        game_id = await seed_game("Bulk Craft Note")
        result = await main.record_assessment(
            items=[
                {
                    "game_id": game_id,
                    "verdict": "skip",
                    "craft_note": "Review-bombed over a launcher change, not the game.",
                }
            ]
        )
        self.assertEqual(result["ok"], 1)
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(
            json.loads(row["presentation"])["craft_note"],
            "Review-bombed over a launcher change, not the game.",
        )


_MEDIA_PAYLOAD = {
    "media": {
        "source": "steam",
        "trailer": {
            "kind": "mp4",
            "url": "https://cdn.cloudflare.steamstatic.com/steam/apps/1/movie480.mp4",
            "hq_url": "https://cdn.cloudflare.steamstatic.com/steam/apps/1/movie_max.mp4",
            "poster": "https://shared.akamai.steamstatic.com/poster.jpg",
            "name": "Trailer",
        },
        "screenshots": [{"thumb": "t1", "full": "f1"}],
        "screenshot_count": 1,
        "screenshots_truncated": False,
        "short_description": "A tiny bug with a nail.",
    },
    "similar_raw": None,
    "similar_count": None,
    "igdb_id": None,
}

_PACKAGE_KEYS = {
    "game",
    "verdict",
    "summary",
    "presentation",
    "comparisons",
    "craft",
    "fit_call",
    "flags",
    "anchors",
    "ownership",
    "time",
    "price",
    "media",
    "similar",
    "pedigree",
    "past",
    "errors",
}


class EvaluationPackageTests(ToolDBTestCase):
    """The card payload record_assessment answers with (single mode only).

    The rule under test throughout: the package is decoration over a verdict
    that is ALREADY committed. Nothing here may fail the recording.
    """

    def _media(self, payload=_MEDIA_PAYLOAD, **kwargs):
        return patch(
            "gamelib_mcp.tools.assessment.get_game_media",
            AsyncMock(return_value=payload, **kwargs),
        )

    async def test_the_frozen_shape_comes_back_whole(self):
        game_id = await make_steam_game("Hollow Knight", 367520, playtime_minutes=180)
        await add_rating(game_id, "manual", 9.0, 9.0)
        anchor_id = await seed_game("Ori")
        await add_platform(anchor_id, "steam", playtime_minutes=600)
        await add_rating(anchor_id, "manual", 8.0, 8.0)

        with self._media():
            result = await main.record_assessment(
                game_id=game_id,
                verdict="buy_now",
                summary="buy it",
                craft_adjusted=0.94,
                craft_positive_pct=97.0,
                review_count=140000,
                recent_trajectory="stable",
                opencritic_score=90.0,
                fit_call="strong fit",
                anchors_cited=[{"name": "Ori", "game_id": anchor_id}, "Dead Cells"],
                flags=["long for an evening"],
                price_seen=14.99,
                price_currency="EUR",
                price_platform="steam",
                target_price=9.99,
                elevator_pitch="A nail, a kingdom, and no map.",
                comparisons=[{"name": "Ori", "relation": "similar"}],
            )

        package = result["package"]
        self.assertEqual(set(package), _PACKAGE_KEYS)
        self.assertEqual(package["errors"], [])
        self.assertEqual(
            package["game"],
            {
                "game_id": game_id,
                "name": "Hollow Knight",
                "release_year": None,
                "cover_url": (
                    "https://cdn.cloudflare.steamstatic.com/steam/apps/"
                    "367520/library_600x900.jpg"
                ),
            },
        )
        self.assertEqual(package["verdict"], "buy_now")
        self.assertEqual(package["media"], _MEDIA_PAYLOAD["media"])
        self.assertEqual(
            package["craft"],
            {
                "adjusted": 0.94,
                "positive_pct": 97.0,
                "review_count": 140000,
                "trajectory": "stable",
                "opencritic_score": 90.0,
                # Read off the library, not declared by the caller — null here
                # because this seeded game carries no enrichment row.
                "metacritic_score": None,
            },
        )
        self.assertEqual(package["fit_call"], "strong fit")
        self.assertEqual(package["flags"], ["long for an evening"])
        self.assertEqual(
            package["price"],
            {"seen": 14.99, "currency": "EUR", "platform": "steam", "target": 9.99},
        )
        self.assertEqual(package["ownership"]["owned"], True)
        self.assertEqual(package["ownership"]["platforms"], ["steam"])
        self.assertEqual(package["ownership"]["playtime_hours"], 3.0)
        self.assertEqual(package["ownership"]["my_rating"], 9.0)
        self.assertEqual(package["time"]["recent_weekly_minutes"], 0)
        self.assertIsNone(package["similar"])
        self.assertIsNone(package["past"])

        # Anchors resolve by game_id; a name-only citation passes through.
        resolved, name_only = package["anchors"]
        self.assertEqual(resolved["game_id"], anchor_id)
        self.assertEqual(resolved["rating"], 8.0)
        self.assertEqual(resolved["playtime_hours"], 10.0)
        self.assertEqual(name_only, {
            "game_id": None,
            "name": "Dead Cells",
            "rating": None,
            "playtime_hours": None,
            "completion_status": None,
            "cover_url": None,
        })

        # A comparison resolves by EXACT name only, and is annotated with what
        # he owns of it.
        (comparison,) = package["comparisons"]
        self.assertEqual(comparison["game_id"], anchor_id)
        self.assertEqual(comparison["owned"], True)
        self.assertEqual(comparison["my_rating"], 8.0)
        self.assertEqual(comparison["playtime_hours"], 10.0)

    async def test_an_unowned_candidate_falls_back_to_the_steam_capsule(self):
        # A minted candidate has no IGDB cover slug and no identifier row, so
        # the games row yields no cover at all — the appid the media lookup
        # resolved does, and the card would otherwise render its gradient
        # placeholder beside real screenshots.
        with self._media(None):
            result = await record_assessment(
                name="Unowned Candidate", appid=424242, verdict="wishlist_for_sale"
            )

        self.assertTrue(result["created"])
        self.assertEqual(
            result["package"]["game"]["cover_url"],
            "https://cdn.cloudflare.steamstatic.com/steam/apps/424242/library_600x900.jpg",
        )

    async def test_a_candidate_with_no_appid_anywhere_keeps_a_null_cover(self):
        game_id = await seed_game("Coverless")
        with self._media(None):
            result = await record_assessment(game_id=game_id, verdict="skip")
        self.assertIsNone(result["package"]["game"]["cover_url"])

    async def test_the_stored_metacritic_score_rides_in_the_craft_block(self):
        # The one craft number the caller never declares: it is read off the
        # library's own enrichment (game_platform_enrichment, per platform row).
        game_id = await make_steam_game("Scored", 606060, metacritic_score=83)

        with self._media(None):
            result = await record_assessment(game_id=game_id, verdict="buy_now")

        self.assertEqual(result["package"]["craft"]["metacritic_score"], 83)

    async def test_a_stored_metacritic_alone_is_enough_for_a_craft_block(self):
        # _block_or_none's rule is unchanged — the block exists when ANY member
        # does, and a critic-only candidate now has one.
        game_id = await make_steam_game("Critics Only", 707070, metacritic_score=71)

        with self._media(None):
            result = await record_assessment(game_id=game_id, verdict="skip")

        self.assertEqual(
            result["package"]["craft"],
            {
                "adjusted": None,
                "positive_pct": None,
                "review_count": None,
                "trajectory": None,
                "opencritic_score": None,
                "metacritic_score": 71,
            },
        )

    async def test_an_unresolved_comparison_is_left_unannotated(self):
        game_id = await seed_game("Lineage Probe")
        with self._media(None):
            result = await record_assessment(
                game_id=game_id,
                verdict="skip",
                comparisons=[
                    {"name": "Never Heard Of It", "relation": "better_version"}
                ],
            )
        (comparison,) = result["package"]["comparisons"]
        self.assertIsNone(comparison["game_id"])
        self.assertIsNone(comparison["owned"])
        self.assertIsNone(comparison["my_rating"])
        self.assertIsNone(comparison["playtime_hours"])

    async def test_a_mismatched_comparison_game_id_shows_the_rows_real_name(self):
        game_id = await seed_game("Mismatch Probe")
        other = await seed_game("The Actual Game")
        await add_platform(other, "steam", playtime_minutes=300)
        with self._media(None):
            result = await record_assessment(
                game_id=game_id,
                verdict="skip",
                comparisons=[
                    {"name": "Wrong Label", "relation": "similar", "game_id": other}
                ],
            )
        (comparison,) = result["package"]["comparisons"]
        # The library row is ground truth: its REAL name renders beside its
        # stats, so a mismatched id is a visible mistake instead of one game's
        # name over another game's ownership. The declared name still stands
        # in the stored presentation JSON.
        self.assertEqual(comparison["name"], "The Actual Game")
        self.assertEqual(comparison["game_id"], other)
        self.assertTrue(comparison["owned"])

    async def test_a_media_source_outage_is_reported_in_package_errors(self):
        # get_game_media never raises for a provider failure — it answers
        # empty-handed with an errors list, and the package must relay that
        # instead of rendering a silently bare card (Codex review, 2026-08-29).
        game_id = await seed_game("Outage Probe")
        empty_handed = {
            "media": None,
            "similar_raw": None,
            "similar_count": None,
            "pedigree_raw": None,
            "igdb_id": None,
            "errors": ["steam: fetch failed"],
        }
        with self._media(empty_handed):
            result = await record_assessment(game_id=game_id, verdict="skip")
        package = result["package"]
        self.assertIsNone(package["media"])
        self.assertEqual(package["errors"], ["media: steam: fetch failed"])

    async def test_similar_games_are_annotated_against_the_library(self):
        game_id = await seed_game("Similarity Probe")
        owned_unplayed = await seed_game("Owned Unplayed")
        await add_platform(owned_unplayed, "steam", playtime_minutes=0)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_id = 101 WHERE id = ?", (owned_unplayed,)
            )
            await db.commit()

        payload = {
            **_MEDIA_PAYLOAD,
            "similar_raw": [
                {
                    "igdb_id": 101,
                    "name": "Owned Unplayed",
                    "release_year": 2016,
                    "cover_image_id": "abc",
                },
                {
                    "igdb_id": 202,
                    "name": "Unknown Neighbour",
                    "release_year": None,
                    "cover_image_id": None,
                },
            ],
            "similar_count": 9,
        }
        with self._media(payload):
            result = await record_assessment(game_id=game_id, verdict="skip")

        similar = result["package"]["similar"]
        self.assertEqual(similar["count"], 9)
        self.assertTrue(similar["truncated"])
        owned_entry, unknown_entry = similar["items"]
        self.assertTrue(owned_entry["owned"])
        self.assertTrue(owned_entry["unplayed"])
        self.assertEqual(
            owned_entry["cover_url"],
            "https://images.igdb.com/igdb/image/upload/t_cover_big/abc.jpg",
        )
        self.assertFalse(unknown_entry["owned"])
        self.assertFalse(unknown_entry["unplayed"])
        self.assertIsNone(unknown_entry["cover_url"])

    async def test_the_pedigree_block_is_annotated_like_the_similar_row(self):
        # Same shared layer (tools/game_media.py) the detail card goes through,
        # so the card and the package render identical keys.
        game_id = await seed_game("Pedigree Package")
        earlier = await seed_game("Their Last One")
        await add_platform(earlier, "steam", playtime_minutes=300)
        await add_rating(earlier, "manual", 9.0, 9.0)
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 501 WHERE id = ?", (earlier,))
            await db.commit()

        payload = {
            **_MEDIA_PAYLOAD,
            "pedigree_raw": {
                "developer": {
                    "name": "Team Cherry",
                    "igdb_company_id": 6455,
                    "founded_year": 2012,
                    "country": 36,
                },
                "developer_names": ["Team Cherry"],
                "publisher_name": None,
                "previous_games": [
                    {
                        "igdb_id": 501,
                        "name": "Their Last One",
                        "release_year": 2014,
                        "cover_image_id": "abc",
                        "critic_score": 88,
                    }
                ],
                "previous_count": 1,
                "previous_truncated": False,
                "catalog_size": 2,
                "catalog_truncated": False,
                "big_catalog": False,
                "hypes": None,
            },
        }
        with self._media(payload):
            result = await record_assessment(game_id=game_id, verdict="buy_now")

        pedigree = result["package"]["pedigree"]
        self.assertEqual(pedigree["developer"]["founded_year"], 2012)
        (entry,) = pedigree["previous_games"]
        self.assertTrue(entry["owned"])
        self.assertEqual(entry["my_rating"], 9.0)
        self.assertEqual(entry["playtime_hours"], 5.0)
        self.assertEqual(entry["critic_score"], 88)
        self.assertEqual(
            entry["cover_url"],
            "https://images.igdb.com/igdb/image/upload/t_cover_big/abc.jpg",
        )
        self.assertEqual(
            pedigree["library_track_record"],
            {"owned_count": 1, "played_count": 1, "avg_my_rating": 9.0},
        )

    async def test_a_candidate_with_no_pedigree_gets_a_null_block(self):
        game_id = await seed_game("Studioless Package")
        with self._media(_MEDIA_PAYLOAD):
            result = await record_assessment(game_id=game_id, verdict="skip")
        self.assertIsNone(result["package"]["pedigree"])

    async def test_past_verdicts_come_from_the_rows_already_queried(self):
        game_id = await seed_game("Repeat Ask")
        for day in range(1, 4):
            await record_assessment(
                game_id=game_id,
                verdict="wishlist_for_sale",
                assessed_at=f"2026-0{day}-01",
                summary=f"take {day}",
                price_seen=30.0 - day,
                price_currency="EUR",
            )
        with self._media(None):
            result = await record_assessment(
                game_id=game_id, verdict="buy_now", assessed_at="2026-08-01"
            )

        past = result["package"]["past"]
        self.assertEqual(past["count"], 3)
        self.assertFalse(past["truncated"])
        self.assertEqual(
            past["items"][0],
            {
                "assessed_at": "2026-03-01T00:00:00+00:00",
                "verdict": "wishlist_for_sale",
                "summary": "take 3",
                "price_seen": 27.0,
                "price_currency": "EUR",
            },
        )

    async def test_a_media_failure_degrades_to_an_errors_entry(self):
        game_id = await seed_game("Media Outage")
        with patch(
            "gamelib_mcp.tools.assessment.get_game_media",
            AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            result = await record_assessment(
                game_id=game_id, verdict="skip", summary="still recorded"
            )

        # The verdict is recorded either way — that is the whole point.
        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["verdict"], "skip")
        package = result["package"]
        self.assertEqual(package["errors"], ["media: fetch failed"])
        self.assertIsNone(package["media"])
        self.assertEqual(package["summary"], "still recorded")

    async def test_a_broken_package_never_fails_the_recording(self):
        game_id = await seed_game("Assembly Outage")
        with patch(
            "gamelib_mcp.tools.assessment._build_package",
            AsyncMock(side_effect=RuntimeError("boom")),
        ):
            result = await record_assessment(game_id=game_id, verdict="buy_now")

        (row,) = await _assessment_rows(game_id)
        self.assertEqual(row["verdict"], "buy_now")
        self.assertEqual(
            result["package"],
            {
                "game": {"game_id": game_id},
                "verdict": "buy_now",
                "errors": ["package: assembly failed"],
            },
        )

    async def test_bulk_mode_returns_no_package(self):
        game_id = await seed_game("Bulk No Package")
        result = await main.record_assessment(
            items=[{"game_id": game_id, "verdict": "skip"}]
        )
        self.assertNotIn("package", result)
        self.assertNotIn("package", result["results"][0])

    async def test_media_identity_prefers_the_appid_then_igdb_then_the_name(self):
        game_id = await seed_game("Identity Order")
        async with db_module.get_db() as db:
            await db.execute("UPDATE games SET igdb_id = 777 WHERE id = ?", (game_id,))
            await db.commit()

        with self._media(None) as fetch:
            await record_assessment(game_id=game_id, verdict="skip", steam_appid=4242)
        self.assertEqual(
            fetch.await_args.kwargs,
            {"steam_appid": 4242, "igdb_id": 777, "name": "Identity Order"},
        )

        with self._media(None) as fetch:
            await record_assessment(
                game_id=game_id, verdict="skip", assessed_at="2026-01-02"
            )
        self.assertEqual(
            fetch.await_args.kwargs,
            {"steam_appid": None, "igdb_id": 777, "name": "Identity Order"},
        )


if __name__ == "__main__":
    unittest.main()
