"""Characterization tests for gamelib_mcp.tools.ratings."""

from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_rating,
    make_steam_game,
    seed_game,
    set_tag_affinity,
)

from gamelib_mcp.data import db as db_module
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
        self.assertEqual(
            set(profile),
            {
                "summary",
                "top_tags",
                "bottom_tags",
                "shrinkage",
                "rate_next",
                "rate_next_candidates",
            },
        )
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

    async def test_ranks_on_stored_affinity_without_redamping(self):
        # affinity_score is already the shrunk posterior deviation, so the
        # profile ranks on it directly — no second game_count damping, which
        # would shrink the same evidence twice.
        await set_tag_affinity("emotional", affinity_score=1.08, avg_score=8.7, game_count=12)
        await set_tag_affinity("princess", affinity_score=0.21, avg_score=9.5, game_count=3)
        profile = await ratings.get_taste_profile()
        tags = [t["tag"] for t in profile["top_tags"]]
        self.assertEqual(tags, ["emotional", "princess"])
        self.assertEqual(profile["top_tags"][0]["affinity_score"], 1.08)

    async def test_strong_affinity_cut_needs_a_deep_enough_profile(self):
        # Fewer than STRONG_AFFINITY_RANK supported tags: no bar is invented.
        await set_tag_affinity("deck-building", affinity_score=0.4, avg_score=8.5, game_count=6)
        profile = await ratings.get_taste_profile()
        self.assertIsNone(profile["shrinkage"]["strong_affinity"])

        for i in range(12):
            await set_tag_affinity(f"tag{i}", affinity_score=0.5 - i * 0.02, avg_score=8.0, game_count=4)
        profile = await ratings.get_taste_profile()
        # 10th best supported affinity: deck-building (0.4) sits between
        # tag4 (0.42) and tag5 (0.40)... ranked list is 0.5, 0.48, 0.46, 0.44,
        # 0.42, 0.4 (deck-building ties tag5), so the 10th is 0.34.
        self.assertAlmostEqual(profile["shrinkage"]["strong_affinity"], 0.34, places=6)

    async def test_low_support_tag_no_longer_needs_a_display_floor(self):
        # The old floor existed because a 2-game 10/10 keyword ("cow") could
        # out-score a well-evidenced tag. With shrinkage applied at the source
        # those tags arrive near zero and sort themselves to the bottom.
        await set_tag_affinity("go-kart", affinity_score=0.04, avg_score=10.0, game_count=1)
        await set_tag_affinity("cow", affinity_score=0.09, avg_score=10.0, game_count=2)
        await set_tag_affinity("deck-building", affinity_score=0.21, avg_score=8.5, game_count=6)
        profile = await ratings.get_taste_profile()
        tags = [t["tag"] for t in profile["top_tags"]]
        self.assertEqual(tags, ["deck-building", "cow", "go-kart"])

    async def test_empty_ratings_summary(self):
        profile = await ratings.get_taste_profile()
        self.assertEqual(profile["summary"]["total_rated"], 0)
        self.assertIsNone(profile["summary"]["avg_score"])
        self.assertEqual(profile["top_tags"], [])


class RateNextTests(ToolDBTestCase):
    """The coverage section of the taste report: what to rate next, and why."""

    async def test_ranks_by_playtime_then_tag_rarity(self):
        # One rated game establishes "roguelike" as well-covered; the two
        # candidates differ only in playtime and how novel their tags are.
        rated = await make_steam_game("Hades", 1, tags=["Roguelike"])
        await add_rating(rated, "backloggd", 4.5, 9.0)

        much_played = await make_steam_game(
            "Long Haul", 2, playtime_minutes=12_000, tags=["Roguelike"]
        )
        barely_played = await make_steam_game(
            "Quick Look", 3, playtime_minutes=30, tags=["Roguelike"]
        )
        novel_tags = await make_steam_game(
            "Odd One", 4, playtime_minutes=30, tags=["Fishing", "Farming Sim"]
        )

        profile = await ratings.get_taste_profile()
        order = [entry["game_id"] for entry in profile["rate_next"]]
        self.assertEqual(order[0], much_played)
        # Same playtime, so the rarity term breaks the tie: two tags the rated
        # sample has never seen beat one it already covers.
        self.assertLess(order.index(novel_tags), order.index(barely_played))
        self.assertEqual(profile["rate_next_candidates"], 3)

    async def test_recently_played_outranks_an_equal_dormant_game(self):
        from datetime import UTC, datetime, timedelta

        recent_day = (datetime.now(UTC) - timedelta(days=3)).date().isoformat()
        old_day = (datetime.now(UTC) - timedelta(days=400)).date().isoformat()

        recent = await make_steam_game("Fresh", 1, playtime_minutes=600, tags=["indie"])
        dormant = await make_steam_game("Dusty", 2, playtime_minutes=600, tags=["indie"])
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET last_played = ? WHERE game_id = ?",
                (recent_day, recent),
            )
            await db.execute(
                "UPDATE game_platforms SET last_played = ? WHERE game_id = ?",
                (old_day, dormant),
            )
            await db.commit()

        entries = (await ratings.get_taste_profile())["rate_next"]
        by_id = {entry["game_id"]: entry for entry in entries}
        self.assertEqual(entries[0]["game_id"], recent)
        self.assertIn("played in the last 90 days", by_id[recent]["reasons"])
        self.assertNotIn("played in the last 90 days", by_id[dormant]["reasons"])
        self.assertEqual(by_id[recent]["last_played"], recent_day)

    async def test_reasons_name_the_playtime_and_the_rare_tags(self):
        game_id = await make_steam_game(
            "Explainable", 1, playtime_minutes=14_640, tags=["Fishing", "Cozy"]
        )

        entry = (await ratings.get_taste_profile())["rate_next"][0]
        self.assertEqual(entry["game_id"], game_id)
        self.assertEqual(entry["playtime_hours"], 244.0)
        self.assertEqual(entry["reasons"][0], "244h played, unrated")
        self.assertEqual(entry["reasons"][1], "2 rarely-rated tags: cozy, fishing")

    async def test_never_played_candidate_says_so(self):
        await make_steam_game("Shelfware", 1, tags=["indie"])
        entry = (await ratings.get_taste_profile())["rate_next"][0]
        self.assertEqual(entry["playtime_hours"], 0.0)
        self.assertEqual(entry["reasons"][0], "owned but never played, unrated")

    async def test_excludes_rated_unowned_untagged_and_finished_games(self):
        keeper = await make_steam_game("Keeper", 1, playtime_minutes=100, tags=["indie"])

        rated = await make_steam_game("Rated", 2, playtime_minutes=100, tags=["indie"])
        await add_rating(rated, "manual", 8.0, 8.0)

        await make_steam_game("Untagged", 3, playtime_minutes=100)

        unowned = await seed_game("Wishlisted Only", tags=["indie"])
        await db_module.upsert_wishlist_entry(unowned, "steam", source="manual")

        retired = await make_steam_game("Refunded", 4, playtime_minutes=100, tags=["indie"])
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET owned = 0 WHERE game_id = ?", (retired,)
            )
            await db.commit()

        finished = await make_steam_game("Done", 5, playtime_minutes=100, tags=["indie"])
        dropped = await make_steam_game("Dropped", 6, playtime_minutes=100, tags=["indie"])
        evergreen = await make_steam_game(
            "Forever", 7, playtime_minutes=100, tags=["indie"]
        )
        async with db_module.get_db() as db:
            for game_id, status in (
                (finished, "completed"),
                (dropped, "abandoned"),
                (evergreen, "evergreen"),
            ):
                await db.execute(
                    "UPDATE games SET completion_status = ? WHERE id = ?",
                    (status, game_id),
                )
            await db.commit()

        dlc = await make_steam_game("Some DLC", 8, playtime_minutes=100, tags=["indie"])
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET content_type = 'dlc', is_primary_library_item = 0"
                " WHERE id = ?",
                (dlc,),
            )
            await db.commit()

        profile = await ratings.get_taste_profile()
        # evergreen stays a candidate — an endless game is unrated, not judged.
        self.assertEqual(
            {entry["game_id"] for entry in profile["rate_next"]}, {keeper, evergreen}
        )
        self.assertEqual(profile["rate_next_candidates"], 2)

    async def test_list_is_capped_but_the_count_is_true(self):
        for i in range(ratings.RATE_NEXT_LIMIT + 5):
            await make_steam_game(f"Candidate {i}", i + 1, playtime_minutes=i, tags=["indie"])

        profile = await ratings.get_taste_profile()
        self.assertEqual(len(profile["rate_next"]), ratings.RATE_NEXT_LIMIT)
        self.assertEqual(profile["rate_next_candidates"], ratings.RATE_NEXT_LIMIT + 5)

    async def test_empty_library_returns_an_empty_queue(self):
        profile = await ratings.get_taste_profile()
        self.assertEqual(profile["rate_next"], [])
        self.assertEqual(profile["rate_next_candidates"], 0)


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
