"""get_assessment_context: the game-quality skill's mechanical layer (ADR 0006).

The craft/fit math is unit-tested against the known outputs of the scripts it
ports (skills/game-quality/scripts/craft_score.py and fit_check.py); the tool
itself is exercised end-to-end over a real temp DB via the conftest harness.
"""

import unittest

from conftest import (
    ToolDBTestCase,
    add_platform,
    add_rating,
    add_steam_appid,
    add_steam_data,
    seed_game,
    set_tag_affinity,
)
from gamelib_mcp import main
from gamelib_mcp.tools import assessment
from gamelib_mcp.tools.assessment import compute_craft_score, compute_fit


class CraftScoreMathTests(unittest.TestCase):
    """Pinned against the retired craft_score.py's outputs for the same inputs."""

    def test_skill_example_matches_the_script(self):
        # The SKILL.md worked example: 88% of 114,479 all-time, 89% of 3,074 recent.
        out = compute_craft_score(88, 114479, 89, 3074)
        self.assertEqual(out["adjusted"], 0.8686)
        self.assertEqual(out["raw_positive_pct"], 88.0)
        self.assertEqual(out["band"], "excellent")
        self.assertEqual(out["recent"]["adjusted"], 0.8552)
        self.assertEqual(out["recent"]["delta_pp_vs_alltime"], 1.0)
        self.assertEqual(out["trajectory"], "stable")
        self.assertIn("86.9% adjusted", out["formatted_line"])
        self.assertIn("88% of 114,479 reviews", out["formatted_line"])
        self.assertNotIn("insufficient_data", out)

    def test_fractional_percentages_are_equivalent(self):
        self.assertEqual(
            compute_craft_score(0.88, 114479)["adjusted"],
            compute_craft_score(88, 114479)["adjusted"],
        )

    def test_band_thresholds(self):
        cases = [
            (0.92, "elite"),
            (0.9199, "excellent"),
            (0.85, "excellent"),
            (0.8499, "very_good"),
            (0.78, "very_good"),
            (0.7799, "divisive"),
            (0.70, "divisive"),
            (0.6999, "caution"),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                key, _label = assessment._band_for(score)
                self.assertEqual(key, expected)

    def test_insufficient_data_below_50_reviews(self):
        out = compute_craft_score(90, 49)
        self.assertTrue(out["insufficient_data"])
        self.assertIn("Fewer than 50 reviews", out["note"])
        # The script still reports the (meaningless) adjusted score and band.
        self.assertEqual(out["adjusted"], 0.7768)
        self.assertIn("band", out)

    def test_early_access_discounts_one_band(self):
        out = compute_craft_score(95, 1000, early_access=True)
        # adjusted 0.8938 is "excellent"; EA discounts it to very_good.
        self.assertEqual(out["adjusted"], 0.8938)
        self.assertEqual(out["band"], "very_good")
        self.assertTrue(out["early_access_discount_applied"])

    def test_early_access_cannot_go_below_caution(self):
        out = compute_craft_score(40, 1000, early_access=True)
        self.assertEqual(out["band"], "caution")

    def test_trajectory_improving_and_regressing(self):
        improving = compute_craft_score(80, 10000, 86, 500)
        self.assertTrue(improving["trajectory"].startswith("improving"))
        regressing = compute_craft_score(90, 10000, 82, 500)
        self.assertTrue(regressing["trajectory"].startswith("REGRESSING"))
        self.assertIn("recent REGRESSING", regressing["formatted_line"])

    def test_recent_omitted_when_not_supplied(self):
        out = compute_craft_score(88, 114479)
        self.assertNotIn("recent", out)
        self.assertNotIn("trajectory", out)


def _profile(top=None, bottom=None):
    return {"top_tags": top or [], "bottom_tags": bottom or []}


def _tag(tag, affinity, game_count=5, avg_score=8.0):
    return {
        "tag": tag,
        "affinity_score": affinity,
        "avg_score": avg_score,
        "game_count": game_count,
    }


class FitCheckTests(unittest.TestCase):
    """Ports fit_check.py's ladder, rescaled 'strong' threshold aside."""

    TOP = [
        _tag("roguelike", 2.1, 6),
        _tag("deckbuilder", 1.5, 4),
        _tag("indie", 1.2, 12),
        _tag("pixel graphics", 0.4, 8),
    ]
    BOTTOM = [_tag("sports", -1.8, 3, avg_score=4.0)]

    def test_strong_fit(self):
        out = compute_fit(
            ["Roguelike", "Deckbuilder", "Indie", "Pixel Graphics"],
            _profile(self.TOP, self.BOTTOM),
        )
        self.assertEqual(out["suggested_call"], "strong fit")
        self.assertFalse(out["core_gap"])
        self.assertEqual(out["top_coverage"], 1.0)
        self.assertEqual(len(out["matched_top_tags"]), 4)
        # Sorted by affinity descending.
        self.assertEqual(out["matched_top_tags"][0]["tag"], "Roguelike")

    def test_probable_miss_on_bottom_match_without_strong_top(self):
        out = compute_fit(["Sports", "Football"], _profile(self.TOP, self.BOTTOM))
        self.assertEqual(out["suggested_call"], "probable miss")
        self.assertEqual(len(out["matched_bottom_tags"]), 1)

    def test_cold_start_overlap_resolves_by_affinity_sign(self):
        # A cold-start profile (fewer eligible rows than the top-20 +
        # bottom-10 window) returns the same rows in both lists; a negative
        # tag present in both must classify as a bottom match, never ride
        # the top-first branch into a top match.
        overlap = self.TOP + [_tag("sports", -1.8, 3, avg_score=4.0)]
        out = compute_fit(["Sports", "Football"], _profile(overlap, overlap))
        self.assertEqual(out["matched_top_tags"], [])
        self.assertEqual(len(out["matched_bottom_tags"]), 1)
        self.assertEqual(out["suggested_call"], "probable miss")

    def test_cold_start_overlap_keeps_positive_tags_on_top(self):
        overlap = self.TOP + [_tag("sports", -1.8, 3, avg_score=4.0)]
        out = compute_fit(
            ["Roguelike", "Deckbuilder", "Indie", "Pixel Graphics"],
            _profile(overlap, overlap),
        )
        self.assertEqual(out["suggested_call"], "strong fit")
        self.assertEqual(len(out["matched_top_tags"]), 4)
        self.assertEqual(out["matched_bottom_tags"], [])

    def test_coin_flip_on_single_weak_match(self):
        out = compute_fit(
            ["Pixel Graphics", "Farming", "Fishing"],
            _profile(self.TOP, self.BOTTOM),
        )
        # One matched top tag under the strong threshold, coverage 1/3 < 0.4.
        self.assertEqual(out["suggested_call"], "coin flip")

    def test_probable_miss_when_nothing_matches(self):
        out = compute_fit(["Racing", "Sports Sim"], _profile(self.TOP))
        self.assertEqual(out["suggested_call"], "probable miss")
        self.assertEqual(out["unmatched_tags"], ["Racing", "Sports Sim"])

    def test_core_gap_downgrades_the_call_one_rung(self):
        # First 4 tags (the core loop) miss the profile entirely; the strong
        # roguelike match rides in 5th. Base call would be coin flip
        # (1 strong match, coverage 0.2) — the core gap drops it to miss.
        out = compute_fit(
            ["Survival", "Crafting", "Base Building", "Open World", "Roguelike"],
            _profile(self.TOP),
        )
        self.assertTrue(out["core_gap"])
        self.assertEqual(out["suggested_call"], "probable miss")

    def test_normalization_collides_surface_variants(self):
        top = [_tag("souls-like", 2.0), _tag("singleplayer", 1.4)]
        out = compute_fit(["Souls Like", "Single Player"], _profile(top))
        self.assertEqual(len(out["matched_top_tags"]), 2)
        self.assertEqual(out["unmatched_tags"], [])

    def test_candidate_tags_are_deduped(self):
        out = compute_fit(["Co-op", "Co-Op", "coop"], _profile(self.TOP))
        self.assertEqual(out["candidate_tags"], 1)


class AssessmentInputValidationTests(ToolDBTestCase):
    async def test_requires_identity_or_tags(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_assessment_context()
        self.assertIn("identity", str(ctx.exception))

    async def test_positive_pct_requires_the_count(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_assessment_context(tags=["indie"], steam_positive_pct=90)
        self.assertIn("steam_total_reviews", str(ctx.exception))

    async def test_count_requires_the_pct(self):
        with self.assertRaises(Exception):
            await main.get_assessment_context(tags=["indie"], steam_total_reviews=100)

    async def test_recent_requires_all_time(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_assessment_context(
                tags=["indie"],
                steam_recent_positive_pct=90,
                steam_recent_total_reviews=100,
            )
        self.assertIn("all-time", str(ctx.exception))

    async def test_recent_pair_must_be_complete(self):
        with self.assertRaises(Exception):
            await main.get_assessment_context(
                tags=["indie"],
                steam_positive_pct=90,
                steam_total_reviews=100,
                steam_recent_positive_pct=90,
            )

    async def test_pct_out_of_range(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_assessment_context(
                tags=["indie"], steam_positive_pct=101, steam_total_reviews=100
            )
        self.assertIn("between 0 and 100", str(ctx.exception))

    async def test_blank_tags_rejected(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_assessment_context(tags=["", "   "])
        self.assertIn("non-empty", str(ctx.exception))

    async def test_too_many_tags_rejected(self):
        with self.assertRaises(Exception) as ctx:
            await main.get_assessment_context(tags=[f"tag {i}" for i in range(41)])
        self.assertIn("at most", str(ctx.exception))

    async def test_validation_runs_before_resolution(self):
        # A bad craft pair must fail loud even though the identity would
        # resolve — every mode's inputs are validated before any work.
        gid = await seed_game("Validated First")
        await add_platform(gid, "steam", playtime_minutes=100)
        with self.assertRaises(Exception):
            await main.get_assessment_context(game_id=gid, steam_positive_pct=90)


class CraftSourcePrecedenceTests(ToolDBTestCase):
    async def _seed_cached_steam_game(self) -> int:
        gid = await seed_game("Hades", tags=["roguelike", "indie"])
        gpid = await add_platform(gid, "steam", playtime_minutes=1200)
        await add_steam_appid(gpid, 1145360)
        await add_steam_data(
            gpid,
            steam_review_score=8,
            steam_review_desc="Very Positive",
            store_cached_at="2026-07-20T00:00:00+00:00",
        )
        return gid

    async def test_server_cache_serves_enum_only_with_limitations(self):
        gid = await self._seed_cached_steam_game()
        result = await main.get_assessment_context(game_id=gid)
        craft = result["craft"]
        self.assertEqual(craft["source"], "server_cache")
        self.assertEqual(craft["steam_review_score"], 8)
        self.assertEqual(craft["steam_review_desc"], "Very Positive")
        self.assertEqual(craft["as_of"], "2026-07-20T00:00:00+00:00")
        # No counts server-side → no adjusted score, and the response says so.
        self.assertNotIn("adjusted", craft)
        self.assertIn("no review counts", craft["limitations"])

    async def test_caller_numbers_outrank_the_server_cache(self):
        gid = await self._seed_cached_steam_game()
        result = await main.get_assessment_context(
            game_id=gid, steam_positive_pct=88, steam_total_reviews=114479
        )
        craft = result["craft"]
        self.assertEqual(craft["source"], "caller")
        self.assertEqual(craft["adjusted"], 0.8686)
        self.assertEqual(craft["band"], "excellent")

    async def test_no_craft_block_without_any_data(self):
        gid = await seed_game("Uncached Game", tags=["indie"])
        await add_platform(gid, "gog")
        result = await main.get_assessment_context(game_id=gid)
        self.assertNotIn("craft", result)

    async def test_appid_resolution_reaches_the_same_game(self):
        await self._seed_cached_steam_game()
        result = await main.get_assessment_context(appid=1145360)
        self.assertEqual(result["game_resolution"], "resolved")
        self.assertEqual(result["game"]["name"], "Hades")


class AnchorTests(ToolDBTestCase):
    async def test_anchors_capped_with_true_total_and_flag(self):
        for i in range(10):
            gid = await seed_game(f"Rogue {i}", tags=["roguelike"])
            await add_platform(gid, "steam", playtime_minutes=60 * (i + 1))
        result = await main.get_assessment_context(tags=["Roguelike"])
        self.assertEqual(len(result["anchors"]), 8)
        self.assertEqual(result["anchor_count"], 10)
        self.assertTrue(result["anchors_truncated"])

    async def test_anchor_matching_collides_transcription_variants(self):
        # Regression: compute_fit matched "Single Player"/"Turn Based" against
        # a row stored as ["singleplayer", "turn-based"] while the anchor
        # lookup's exact canonical-string match returned anchor_count=0 — a
        # false "no anchor evidence" signal contradicting the fit block on the
        # very same tags. Anchors must match on the same collapsed fit key.
        gid = await seed_game(
            "Stored Variant", tags=["singleplayer", "turn-based"]
        )
        await add_platform(gid, "steam", playtime_minutes=120)
        result = await main.get_assessment_context(
            tags=["Single Player", "Turn Based"]
        )
        self.assertEqual(result["anchor_count"], 1)
        self.assertEqual(
            result["anchors"][0]["matched_core_tags"],
            ["Single Player", "Turn Based"],
        )

    async def test_anchor_carries_rating_playtime_and_status(self):
        gid = await seed_game(
            "Slay the Spire", tags=["roguelike", "deckbuilder"]
        )
        await add_platform(gid, "steam", playtime_minutes=14640)
        await add_rating(gid, "backloggd", 9.0, 9.0)
        async with self._status_db() as db:
            await db.execute(
                "UPDATE games SET completion_status = 'evergreen' WHERE id = ?",
                (gid,),
            )
            await db.commit()

        result = await main.get_assessment_context(
            tags=["Roguelike", "Deckbuilder", "Card Game"]
        )
        self.assertEqual(result["anchor_count"], 1)
        self.assertFalse(result["anchors_truncated"])
        anchor = result["anchors"][0]
        self.assertEqual(anchor["name"], "Slay the Spire")
        self.assertEqual(anchor["matched_core_tags"], ["Roguelike", "Deckbuilder"])
        self.assertEqual(anchor["rating"], {"source": "backloggd", "score": 9.0})
        self.assertEqual(anchor["playtime_hours"], 244.0)
        self.assertEqual(anchor["completion_status"], "evergreen")
        self.assertEqual(anchor["play_state"], "played")

    def _status_db(self):
        from gamelib_mcp.data import db as db_module

        return db_module.get_db()

    async def test_full_weight_rating_outranks_steam_review(self):
        gid = await seed_game("Dual Rated", tags=["roguelike"])
        await add_platform(gid, "steam", playtime_minutes=300)
        await add_rating(gid, "steam_review", 1.0, 8.5)
        await add_rating(gid, "manual", 6.0, 6.0)
        result = await main.get_assessment_context(tags=["Roguelike"])
        self.assertEqual(
            result["anchors"][0]["rating"], {"source": "manual", "score": 6.0}
        )

    async def test_candidate_itself_is_excluded(self):
        gid = await seed_game("Hades II", tags=["roguelike"])
        await add_platform(gid, "steam", playtime_minutes=60)
        other = await seed_game("Hades", tags=["roguelike"])
        await add_platform(other, "steam", playtime_minutes=600)
        result = await main.get_assessment_context(game_id=gid, tags=["Roguelike"])
        self.assertEqual(result["anchor_count"], 1)
        self.assertEqual(result["anchors"][0]["game_id"], other)

    async def test_nested_farmed_and_unowned_rows_are_not_anchors(self):
        farmed = await seed_game("Farmed", tags=["roguelike"], is_farmed=1)
        await add_platform(farmed, "steam", playtime_minutes=6000)
        dlc_parent = await seed_game("Base", tags=["strategy"])
        await add_platform(dlc_parent, "steam")
        dlc = await seed_game(
            "Base DLC",
            tags=["roguelike"],
            content_type="dlc",
            parent_game_id=dlc_parent,
            is_primary_library_item=0,
        )
        await add_platform(dlc, "steam", playtime_minutes=100)
        unowned = await seed_game("Wishlist Only", tags=["roguelike"])
        await add_platform(unowned, "steam", owned=0)
        result = await main.get_assessment_context(tags=["Roguelike"])
        self.assertEqual(result["anchor_count"], 0)
        self.assertEqual(result["anchors"], [])


class FitThroughToolTests(ToolDBTestCase):
    async def test_fit_uses_the_stored_taste_profile(self):
        await set_tag_affinity("roguelike", 2.1, 9.0, 6)
        await set_tag_affinity("deckbuilder", 1.5, 8.5, 4)
        await set_tag_affinity("indie", 1.2, 8.2, 12)
        await set_tag_affinity("sports", -1.8, 4.0, 3)
        result = await main.get_assessment_context(
            tags=["Roguelike", "Deckbuilder", "Indie"]
        )
        fit = result["fit"]
        self.assertEqual(fit["suggested_call"], "strong fit")
        self.assertEqual(fit["tags_source"], "caller")
        self.assertEqual(len(fit["matched_top_tags"]), 3)
        # Raw per-tag rows come back for every candidate tag.
        by_tag = {row["tag"]: row for row in fit["tag_affinities"]}
        self.assertEqual(by_tag["Roguelike"]["affinity_score"], 2.1)
        self.assertEqual(by_tag["Roguelike"]["game_count"], 6)

    async def test_unseen_tag_reports_null_affinity(self):
        await set_tag_affinity("roguelike", 2.1, 9.0, 6)
        result = await main.get_assessment_context(tags=["Roguelike", "Fishing"])
        by_tag = {row["tag"]: row for row in result["fit"]["tag_affinities"]}
        self.assertIsNone(by_tag["Fishing"]["affinity_score"])
        self.assertEqual(by_tag["Fishing"]["game_count"], 0)

    async def test_empty_profile_returns_no_call(self):
        result = await main.get_assessment_context(tags=["Roguelike"])
        fit = result["fit"]
        self.assertIsNone(fit["suggested_call"])
        self.assertIn("No taste-profile data", fit["note"])

    async def test_library_tags_fill_in_when_caller_omits_them(self):
        await set_tag_affinity("roguelike", 2.1, 9.0, 6)
        gid = await seed_game("Tagged Game", tags=["roguelike", "indie"])
        await add_platform(gid, "steam", playtime_minutes=60)
        result = await main.get_assessment_context(game_id=gid)
        fit = result["fit"]
        self.assertEqual(fit["tags_source"], "library")
        self.assertEqual(fit["candidate_tags"], 2)
        # Library tags also drive the anchors block (candidate excluded).
        self.assertEqual(result["anchor_count"], 0)

    async def test_no_fit_without_any_tags(self):
        gid = await seed_game("Untagged Game")
        await add_platform(gid, "steam")
        result = await main.get_assessment_context(game_id=gid)
        self.assertNotIn("fit", result)
        self.assertNotIn("anchors", result)


class GameBlockAndPaceTests(ToolDBTestCase):
    async def test_game_block_is_a_compact_ownership_subset(self):
        gid = await seed_game("Owned Game", tags=["indie"], hltb_main=12.0)
        gpid = await add_platform(gid, "steam", playtime_minutes=90)
        await add_steam_appid(gpid, 12345)
        from gamelib_mcp.data import db as db_module

        async with db_module.get_db() as db:
            await db.execute(
                """UPDATE game_platforms
                   SET price_paid = 4.99, price_currency = 'EUR',
                       purchase_source = 'humble', bundle_name = 'Choice 2026-01'
                   WHERE id = ?""",
                (gpid,),
            )
            await db.commit()
        await add_rating(gid, "manual", 8.0, 8.0)

        result = await main.get_assessment_context(game_id=gid)
        self.assertEqual(result["game_resolution"], "resolved")
        game = result["game"]
        self.assertEqual(game["name"], "Owned Game")
        self.assertTrue(game["owned"])
        self.assertFalse(game["wishlisted"])
        self.assertEqual(game["playtime_hours"], 1.5)
        self.assertEqual(game["hltb_main"], 12.0)
        self.assertEqual(game["my_rating"]["normalized_score"], 8.0)
        self.assertEqual(len(game["owned_platforms"]), 1)
        platform = game["owned_platforms"][0]
        self.assertEqual(platform["platform"], "steam")
        self.assertEqual(platform["price_paid"], 4.99)
        self.assertEqual(platform["bundle_name"], "Choice 2026-01")

    async def test_unknown_game_id_reports_not_found_with_other_blocks(self):
        result = await main.get_assessment_context(game_id=999999, tags=["indie"])
        self.assertEqual(result["game_resolution"], "not_found")
        self.assertNotIn("game", result)
        # The tag-driven blocks still come back.
        self.assertIn("fit", result)
        self.assertIn("anchors", result)
        self.assertIn("pace", result)

    async def test_name_resolves_like_get_game_detail(self):
        gid = await seed_game("Sekiro: Shadows Die Twice", tags=["souls-like"])
        await add_platform(gid, "steam", playtime_minutes=600)
        result = await main.get_assessment_context(name="sekiro shadow")
        self.assertEqual(result["game_resolution"], "resolved")
        self.assertEqual(result["game"]["game_id"], gid)

    async def test_tags_only_call_omits_game_and_resolution(self):
        result = await main.get_assessment_context(tags=["indie"])
        self.assertNotIn("game", result)
        self.assertNotIn("game_resolution", result)

    async def test_pace_block_is_always_present(self):
        result = await main.get_assessment_context(tags=["indie"])
        pace = result["pace"]
        self.assertEqual(pace["total_minutes"], 0)
        self.assertEqual(pace["total_hours"], 0.0)
        self.assertEqual(pace["by_platform"], {})
        self.assertIsNone(pace["most_played"])
        self.assertIn("window", pace)

    async def test_pace_reports_window_activity(self):
        from datetime import date, timedelta

        from gamelib_mcp.data import db as db_module

        gid = await seed_game("Active Game")
        await add_platform(gid, "steam", playtime_minutes=500)
        today = date.today()
        async with db_module.get_db() as db:
            for day, minutes in (
                (today - timedelta(days=40), 100),
                (today - timedelta(days=5), 400),
            ):
                await db.execute(
                    """INSERT INTO play_history
                       (game_id, platform, snapshot_date, playtime_minutes)
                       VALUES (?, 'steam', ?, ?)""",
                    (gid, day.isoformat(), minutes),
                )
            await db.commit()

        result = await main.get_assessment_context(tags=["indie"])
        pace = result["pace"]
        self.assertEqual(pace["total_minutes"], 300)
        self.assertEqual(pace["by_platform"], {"steam": 300})
        self.assertEqual(pace["most_played"]["name"], "Active Game")


if __name__ == "__main__":
    unittest.main()
