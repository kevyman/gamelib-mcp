"""Characterization tests for gamelib_mcp.tools.ratings."""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, make_steam_game, seed_game, add_rating, set_tag_affinity
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

    async def test_empty_ratings_summary(self):
        profile = await ratings.get_taste_profile()
        self.assertEqual(profile["summary"]["total_rated"], 0)
        self.assertIsNone(profile["summary"]["avg_score"])
        self.assertEqual(profile["top_tags"], [])


class SyncRatingsTests(ToolDBTestCase):
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
