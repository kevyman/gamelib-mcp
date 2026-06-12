"""Characterization tests for gamelib_mcp.tools.discover."""

from conftest import (
    ToolDBTestCase,
    make_steam_game,
    seed_game,
    add_platform,
    set_tag_affinity,
)
from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import discover


class FindGamesByVibeTests(ToolDBTestCase):
    async def test_matches_vibe_tag_group_and_shape(self):
        await make_steam_game(
            "Hades",
            1145360,
            playtime_minutes=0,
            tags=["roguelike", "action"],
            metacritic_score=93,
        )
        await make_steam_game("Stardew", 413150, playtime_minutes=0, tags=["cozy"])
        results = await discover.find_games_by_vibe("roguelike", response_format="detailed")
        self.assertEqual(set(results), {"results", "total_matches", "has_more"})
        self.assertEqual(results["total_matches"], 1)
        self.assertFalse(results["has_more"])
        self.assertEqual([g["name"] for g in results["results"]], ["Hades"])
        game = results["results"][0]
        self.assertEqual(
            set(game),
            {
                "game_id",
                "appid",
                "name",
                "platforms",
                "playtime_hours",
                "hltb_main",
                "metacritic_score",
                "opencritic_score",
                "steam_review_desc",
                "protondb_tier",
                "tags",
                "suggested_platform",
            },
        )
        self.assertEqual(game["tags"], ["roguelike", "action"])
        # no hardware_preference set -> suggested_platform falls back to first owned
        self.assertEqual(game["suggested_platform"], "steam")

    async def test_raw_tag_string_fallback(self):
        await make_steam_game("Tetris", 1, playtime_minutes=0, tags=["falling blocks"])
        results = await discover.find_games_by_vibe("falling blocks")
        self.assertEqual([g["name"] for g in results["results"]], ["Tetris"])

    async def test_unplayed_only_default_excludes_played(self):
        await make_steam_game("PlayedRogue", 1, playtime_minutes=600, tags=["roguelike"])
        results = await discover.find_games_by_vibe("roguelike")
        self.assertEqual(results["results"], [])
        self.assertEqual(results["total_matches"], 0)

    async def test_suggested_platform_respects_hardware_preference(self):
        import json

        gid = await seed_game("Multiplat", tags=["roguelike"])
        await add_platform(gid, "steam", playtime_minutes=0)
        await add_platform(gid, "switch2", playtime_minutes=0)
        await db_module.set_meta("hardware_preference", json.dumps(["switch2", "steam"]))
        results = await discover.find_games_by_vibe("roguelike")
        self.assertEqual(results["results"][0]["suggested_platform"], "switch2")

    async def test_concise_drops_platforms_and_tags(self):
        await make_steam_game("Hades", 1145360, playtime_minutes=0, tags=["roguelike", "action"])
        results = await discover.find_games_by_vibe("roguelike")
        game = results["results"][0]
        self.assertNotIn("platforms", game)
        self.assertNotIn("tags", game)

    async def test_offset_and_has_more(self):
        await make_steam_game("Hades", 1, playtime_minutes=0, tags=["roguelike"], metacritic_score=90)
        await make_steam_game("Dead Cells", 2, playtime_minutes=0, tags=["roguelike"], metacritic_score=89)
        results = await discover.find_games_by_vibe("roguelike", limit=1, offset=1)
        self.assertEqual(len(results["results"]), 1)
        self.assertEqual(results["total_matches"], 2)
        self.assertFalse(results["has_more"])


class GetRecommendationsTests(ToolDBTestCase):
    async def test_ranks_by_tag_affinity_with_match_score(self):
        await make_steam_game("LikedGame", 1, playtime_minutes=0, tags=["roguelike"])
        await make_steam_game("MehGame", 2, playtime_minutes=0, tags=["sports"])
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)
        await set_tag_affinity("sports", affinity_score=0.2, avg_score=3.0, game_count=2)
        results = await discover.get_recommendations()
        self.assertEqual([g["name"] for g in results["results"]], ["LikedGame", "MehGame"])
        self.assertIn("match_score", results["results"][0])
        self.assertEqual(results["results"][0]["match_score"], round(2.5, 3))
        self.assertEqual(results["results"][1]["match_score"], round(0.2, 3))
        self.assertEqual(results["total_matches"], 2)

    async def test_excludes_games_without_affinity_tags(self):
        await make_steam_game("NoAffinity", 1, playtime_minutes=0, tags=["obscure"])
        results = await discover.get_recommendations()
        self.assertEqual(results["results"], [])

    async def test_empty_affinity_includes_sync_hint(self):
        await make_steam_game("Anything", 1, playtime_minutes=0, tags=["roguelike"])
        results = await discover.get_recommendations()
        self.assertEqual(results["results"], [])
        self.assertIn("sync_ratings", results["note"])

    async def test_populated_affinity_has_no_note(self):
        await make_steam_game("LikedGame", 1, playtime_minutes=0, tags=["roguelike"])
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)
        results = await discover.get_recommendations()
        self.assertNotIn("note", results)


class VibeHintTests(ToolDBTestCase):
    async def test_unknown_vibe_with_no_matches_gets_hint(self):
        results = await discover.find_games_by_vibe("totally-not-a-vibe")
        self.assertEqual(results["total_matches"], 0)
        self.assertIn("Known vibes", results["note"])

    async def test_known_vibe_no_note(self):
        await make_steam_game("Hades", 1, playtime_minutes=0, tags=["roguelike"])
        results = await discover.find_games_by_vibe("roguelike")
        self.assertNotIn("note", results)
