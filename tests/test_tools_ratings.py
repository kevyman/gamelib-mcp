"""Characterization tests for gamelib_mcp.tools.ratings."""

from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_rating,
    make_steam_game,
    seed_game,
    set_tag_affinity,
)

from gamelib_mcp.tools import ratings


class GetRatingsTests(ToolDBTestCase):
    async def test_shape_and_default_sort_by_score(self):
        a = await make_steam_game("Great Game", 1, playtime_minutes=10)
        b = await make_steam_game("Okay Game", 2, playtime_minutes=10)
        await add_rating(a, "backloggd", raw_score=4.5, normalized_score=9.0, review_text="love")
        await add_rating(b, "steam_review", raw_score=1.0, normalized_score=5.0)
        rows = await ratings.get_ratings(response_format="detailed")
        self.assertEqual(set(rows), {"results", "total_matches", "has_more"})
        self.assertEqual(rows["total_matches"], 2)
        self.assertFalse(rows["has_more"])
        ratings_rows = rows["results"]
        self.assertEqual([r["name"] for r in ratings_rows], ["Great Game", "Okay Game"])
        self.assertEqual(
            set(ratings_rows[0]),
            {
                "game_id",
                "appid",
                "steam_appid",
                "name",
                "platforms",
                "source",
                "raw_score",
                "normalized_score",
                "review_text",
                "synced_at",
            },
        )
        self.assertEqual(ratings_rows[0]["normalized_score"], 9.0)
        self.assertEqual(ratings_rows[0]["source"], "backloggd")
        self.assertEqual(ratings_rows[0]["appid"], 1)

    async def test_filter_by_source(self):
        a = await seed_game("A")
        b = await seed_game("B")
        await add_rating(a, "backloggd", 4.0, 8.0)
        await add_rating(b, "steam_review", 1.0, 5.0)
        rows = await ratings.get_ratings(source="steam_review")
        self.assertEqual([r["name"] for r in rows["results"]], ["B"])

    async def test_min_score_filter(self):
        a = await seed_game("High")
        b = await seed_game("Low")
        await add_rating(a, "backloggd", 4.5, 9.0)
        await add_rating(b, "backloggd", 1.0, 2.0)
        rows = await ratings.get_ratings(min_score=5.0)
        self.assertEqual([r["name"] for r in rows["results"]], ["High"])

    async def test_sort_by_name(self):
        a = await seed_game("Zeta")
        b = await seed_game("Alpha")
        await add_rating(a, "backloggd", 5.0, 10.0)
        await add_rating(b, "backloggd", 1.0, 2.0)
        rows = await ratings.get_ratings(sort_by="name")
        self.assertEqual([r["name"] for r in rows["results"]], ["Alpha", "Zeta"])

    async def test_concise_drops_platforms_and_review_text(self):
        a = await make_steam_game("Great Game", 1, playtime_minutes=10)
        await add_rating(a, "backloggd", raw_score=4.5, normalized_score=9.0, review_text="love")
        rows = await ratings.get_ratings()
        rating = rows["results"][0]
        self.assertNotIn("platforms", rating)
        self.assertNotIn("review_text", rating)

    async def test_offset_and_has_more(self):
        for i in range(3):
            game_id = await seed_game(f"Game {i}")
            await add_rating(game_id, "backloggd", raw_score=5 - i, normalized_score=10 - i)
        rows = await ratings.get_ratings(limit=1, offset=1)
        self.assertEqual(len(rows["results"]), 1)
        self.assertEqual(rows["total_matches"], 3)
        self.assertTrue(rows["has_more"])


class GetTasteProfileTests(ToolDBTestCase):
    async def test_summary_and_tag_rounding(self):
        a = await seed_game("A")
        await add_rating(a, "backloggd", 4.5, 9.0)
        b = await seed_game("B")
        await add_rating(b, "steam_review", 1.0, 4.0)
        await set_tag_affinity("roguelike", affinity_score=2.123456, avg_score=8.987, game_count=5)
        await set_tag_affinity("sports", affinity_score=0.111111, avg_score=3.4, game_count=2)
        profile = await ratings.get_taste_profile()
        self.assertEqual(set(profile), {"summary", "top_tags", "bottom_tags"})
        summary = profile["summary"]
        self.assertEqual(summary["total_rated"], 2)
        self.assertEqual(summary["avg_score"], round((9.0 + 4.0) / 2, 2))
        self.assertEqual(summary["backloggd_ratings"], 1)
        self.assertEqual(summary["steam_review_ratings"], 1)
        top = profile["top_tags"][0]
        self.assertEqual(set(top), {"tag", "affinity_score", "avg_score", "game_count"})
        self.assertEqual(top["tag"], "roguelike")
        self.assertEqual(top["affinity_score"], round(2.123456, 3))
        self.assertEqual(top["avg_score"], round(8.987, 2))

    async def test_low_support_tag_excluded_from_top(self):
        # A lone high rating produces a maximal affinity for its rare tags;
        # below _MIN_PROFILE_SUPPORT distinct games they don't belong in the
        # displayed profile at all ("cow" was topping the real one).
        await set_tag_affinity("go-kart", affinity_score=1.396, avg_score=10.0, game_count=1)
        await set_tag_affinity("cow", affinity_score=1.65, avg_score=10.0, game_count=2)
        await set_tag_affinity("deck-building", affinity_score=1.37, avg_score=8.5, game_count=6)
        profile = await ratings.get_taste_profile()
        tags = [t["tag"] for t in profile["top_tags"]]
        self.assertEqual(tags, ["deck-building"])
        # Raw stored affinity is still surfaced verbatim.
        deck = profile["top_tags"][0]
        self.assertEqual(deck["affinity_score"], 1.37)

    async def test_support_shrinkage_still_ranks_within_included_tags(self):
        # Above the floor, a small-sample outlier must still rank below a tag
        # backed by more games at a similar affinity.
        await set_tag_affinity("princess", affinity_score=1.5, avg_score=9.5, game_count=3)
        await set_tag_affinity("emotional", affinity_score=1.08, avg_score=8.7, game_count=12)
        profile = await ratings.get_taste_profile()
        tags = [t["tag"] for t in profile["top_tags"]]
        self.assertLess(tags.index("emotional"), tags.index("princess"))

    async def test_low_support_negative_tag_excluded_from_bottom(self):
        await set_tag_affinity("penguin", affinity_score=-1.4, avg_score=1.0, game_count=1)
        await set_tag_affinity("walking-sim", affinity_score=-1.3, avg_score=2.0, game_count=5)
        profile = await ratings.get_taste_profile()
        tags = [t["tag"] for t in profile["bottom_tags"]]
        self.assertEqual(tags, ["walking-sim"])

    async def test_empty_ratings_summary(self):
        profile = await ratings.get_taste_profile()
        self.assertEqual(profile["summary"]["total_rated"], 0)
        self.assertIsNone(profile["summary"]["avg_score"])
        self.assertEqual(profile["top_tags"], [])


class SyncRatingsTests(ToolDBTestCase):
    class FakeContext:
        def __init__(self):
            self.progress = []
            self.infos = []

        async def report_progress(self, progress, total):
            self.progress.append((progress, total))

        async def info(self, message):
            self.infos.append(message)

    async def test_aggregates_provider_results(self):
        with (
            patch.object(ratings, "sync_backloggd", AsyncMock(return_value={"synced": 3})),
            patch.object(ratings, "sync_steam_reviews", AsyncMock(return_value={"synced": 2})),
            patch.object(ratings, "recompute_tag_affinity", AsyncMock(return_value=7)),
        ):
            result = await ratings.sync_ratings()
        self.assertEqual(
            result,
            {
                "backloggd": {"synced": 3},
                "steam_reviews": {"synced": 2},
                "tag_affinity_tags_updated": 7,
                "status": "done",
            },
        )

    async def test_reports_progress(self):
        ctx = self.FakeContext()
        with (
            patch.object(ratings, "sync_backloggd", AsyncMock(return_value={"synced": 3})),
            patch.object(ratings, "sync_steam_reviews", AsyncMock(return_value={"synced": 2})),
            patch.object(ratings, "recompute_tag_affinity", AsyncMock(return_value=7)),
        ):
            await ratings.sync_ratings(ctx=ctx)

        self.assertEqual(ctx.progress, [(0, 3), (1, 3), (2, 3), (3, 3)])
        self.assertIn("Syncing Backloggd ratings", ctx.infos)
        self.assertIn("Recomputing tag affinity", ctx.infos)


class RateGameTests(ToolDBTestCase):
    async def test_rates_by_name_and_recomputes_affinity(self):
        await make_steam_game("Hades", 1, tags=["Roguelike", "Action"])
        result = await ratings.rate_game(name="hades", score=9.5, review_text="superb")

        self.assertEqual(result["name"], "Hades")
        self.assertEqual(result["source"], "manual")
        self.assertEqual(result["score"], 9.5)
        self.assertEqual(result["tags_affected"], ["Roguelike", "Action"])
        self.assertEqual(result["tag_affinity_tags_updated"], 2)

        rows = await ratings.get_ratings(source="manual", response_format="detailed")
        self.assertEqual(rows["total_matches"], 1)
        self.assertEqual(rows["results"][0]["normalized_score"], 9.5)
        self.assertEqual(rows["results"][0]["review_text"], "superb")

        profile = await ratings.get_taste_profile()
        self.assertEqual(profile["summary"]["manual_ratings"], 1)
        self.assertEqual({t["tag"] for t in profile["top_tags"]}, {"roguelike", "action"})

    async def test_rerating_overwrites_previous_manual_rating(self):
        gid = await make_steam_game("Hades", 1, tags=["Roguelike"])
        await ratings.rate_game(game_id=gid, score=6.0)
        await ratings.rate_game(game_id=gid, score=9.0)

        rows = await ratings.get_ratings(source="manual")
        self.assertEqual(rows["total_matches"], 1)
        self.assertEqual(rows["results"][0]["normalized_score"], 9.0)

    async def test_fuzzy_name_resolution(self):
        await make_steam_game("Sekiro: Shadows Die Twice", 814380, tags=["Souls-like"])
        result = await ratings.rate_game(name="sekrio shadows die twice", score=8.0)
        self.assertEqual(result["name"], "Sekiro: Shadows Die Twice")

    async def test_score_out_of_range_raises(self):
        from fastmcp.exceptions import ToolError

        await make_steam_game("Hades", 1)
        with self.assertRaisesRegex(ToolError, "between 0 and 10"):
            await ratings.rate_game(name="hades", score=11)

    async def test_unknown_game_raises(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "not found"):
            await ratings.rate_game(name="does-not-exist", score=5)

    async def test_requires_identifier(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "Provide game_id or name"):
            await ratings.rate_game(score=5)

    async def test_includes_content_type_and_parent_name_for_dlc(self):
        # Parent game
        parent_id = await make_steam_game("Bloodborne", 100, tags=["Horror", "Action"])
        # DLC as a nested game
        dlc_id = await seed_game(
            "The Old Hunters",
            tags=["Horror", "Action"],
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )

        result = await ratings.rate_game(game_id=dlc_id, score=9.5, review_text="excellent expansion")

        self.assertEqual(result["name"], "The Old Hunters")
        self.assertEqual(result["content_type"], "dlc")
        self.assertEqual(result["parent_name"], "Bloodborne")
        self.assertIn("parent_name", result)

    async def test_includes_content_type_none_for_primary_game(self):
        game_id = await make_steam_game("Hades", 1, tags=["Roguelike"])

        result = await ratings.rate_game(game_id=game_id, score=9.0)

        self.assertEqual(result["name"], "Hades")
        self.assertEqual(result["content_type"], "base_game")  # default value from schema
        self.assertNotIn("parent_name", result)  # No parent should mean no parent_name key


class RateGamesBatchTests(ToolDBTestCase):
    async def test_rates_many_with_single_affinity_recompute(self):
        a = await make_steam_game("Hades", 1, tags=["Roguelike"])
        await make_steam_game("Celeste", 2, tags=["Platformer"])
        recompute = AsyncMock(return_value=5)
        with patch.object(ratings, "recompute_tag_affinity", recompute):
            result = await ratings.rate_games_batch(
                [
                    {"game_id": a, "score": 9.0},
                    {"name": "celeste", "score": 7.5, "review_text": "tight"},
                ]
            )
        recompute.assert_awaited_once()
        self.assertEqual([r["status"] for r in result["results"]], ["ok", "ok"])
        self.assertEqual(result["ok"], 2)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["tag_affinity_tags_updated"], 5)
        # Per-item results carry the stored rating but not the (deferred)
        # per-call recompute count.
        self.assertNotIn("tag_affinity_tags_updated", result["results"][0])
        rows = await ratings.get_ratings(source="manual", response_format="detailed")
        self.assertEqual(rows["total_matches"], 2)

    async def test_per_item_error_isolation_preserves_order(self):
        gid = await make_steam_game("Hades", 1, tags=["Roguelike"])
        result = await ratings.rate_games_batch(
            [
                {"game_id": gid, "score": 11},          # out of range
                {"name": "does-not-exist", "score": 5},  # unresolvable
                {"game_id": gid, "score": 8.0, "bogus": 1},  # unknown key
                {"game_id": gid, "score": 8.5},          # valid
            ]
        )
        self.assertEqual(
            [r["status"] for r in result["results"]],
            ["error", "error", "error", "ok"],
        )
        self.assertEqual(result["errors"], 3)
        self.assertEqual(result["ok"], 1)
        for bad in result["results"][:3]:
            self.assertIn("error", bad)
            self.assertIn("item", bad)
        self.assertIn("bogus", result["results"][2]["error"])
        rows = await ratings.get_ratings(source="manual")
        self.assertEqual(rows["total_matches"], 1)
        self.assertEqual(rows["results"][0]["normalized_score"], 8.5)

    async def test_dry_run_writes_nothing(self):
        gid = await make_steam_game("Hades", 1, tags=["Roguelike"])
        recompute = AsyncMock(return_value=5)
        with patch.object(ratings, "recompute_tag_affinity", recompute):
            result = await ratings.rate_games_batch(
                [{"game_id": gid, "score": 9.0}], dry_run=True
            )
        recompute.assert_not_awaited()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["tag_affinity_tags_updated"], 0)
        rows = await ratings.get_ratings(source="manual")
        self.assertEqual(rows["total_matches"], 0)

    async def test_empty_and_cap_raise(self):
        from fastmcp.exceptions import ToolError

        with self.assertRaisesRegex(ToolError, "must not be empty"):
            await ratings.rate_games_batch([])
        with self.assertRaisesRegex(ToolError, "capped at 200"):
            await ratings.rate_games_batch([{"game_id": 1, "score": 5}] * 201)
