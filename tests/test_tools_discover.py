"""Characterization tests for gamelib_mcp.tools.discover (discover_games)."""

import json
import math

from conftest import (
    ToolDBTestCase,
    add_platform,
    make_steam_game,
    seed_game,
    set_tag_affinity,
)
from fastmcp.exceptions import ToolError

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import discover
from gamelib_mcp.tools.platforms import update_game


class VibeFilterTests(ToolDBTestCase):
    async def test_matches_vibe_tag_group_and_shape(self):
        await make_steam_game(
            "Hades",
            1145360,
            playtime_minutes=0,
            tags=["roguelike", "action"],
            metacritic_score=93,
        )
        await make_steam_game("Stardew", 413150, playtime_minutes=0, tags=["cozy"])
        results = await discover.discover_games(vibes=["roguelike"], response_format="detailed")
        self.assertEqual(set(results), {"results", "total_matches", "has_more", "offset"})
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
                "cover_url",
                "platforms",
                "playtime_hours",
                "hltb_main",
                "metacritic_score",
                "opencritic_score",
                "steam_review_desc",
                "protondb_tier",
                "tags",
                "suggested_platform",
                "play_state",
                "owned",
                "wishlisted",
            },
        )
        self.assertEqual(game["tags"], ["roguelike", "action"])
        # no hardware_preference set -> suggested_platform falls back to first owned
        self.assertEqual(game["suggested_platform"], "steam")
        self.assertIs(game["owned"], True)
        self.assertIs(game["wishlisted"], False)

    async def test_raw_tag_string_fallback(self):
        await make_steam_game("Tetris", 1, playtime_minutes=0, tags=["falling blocks"])
        results = await discover.discover_games(vibes=["falling blocks"])
        self.assertEqual([g["name"] for g in results["results"]], ["Tetris"])

    async def test_vibe_only_matches_prominent_tags(self):
        # An open-world game carrying a low-vote "racing" tag deep in its
        # prominence-ordered list is not a racing game; a real racer with
        # "racing" up top is.
        filler = [f"tag{i}" for i in range(discover.VIBE_TAG_PROMINENCE_CUTOFF)]
        await make_steam_game(
            "OpenWorldDriver", 1, playtime_minutes=0, tags=filler + ["racing"]
        )
        await make_steam_game(
            "ActualRacer", 2, playtime_minutes=0, tags=["racing", "driving"]
        )
        results = await discover.discover_games(vibes=["racing"])
        self.assertEqual([g["name"] for g in results["results"]], ["ActualRacer"])
        self.assertNotIn("note", results)

    async def test_multiple_vibes_must_all_match(self):
        await make_steam_game("CozyRogue", 1, playtime_minutes=0, tags=["roguelike", "cozy"])
        await make_steam_game("HardRogue", 2, playtime_minutes=0, tags=["roguelike"])
        results = await discover.discover_games(vibes=["roguelike", "cozy"])
        self.assertEqual([g["name"] for g in results["results"]], ["CozyRogue"])

    async def test_unplayed_only_default_excludes_played(self):
        await make_steam_game("PlayedRogue", 1, playtime_minutes=600, tags=["roguelike"])
        results = await discover.discover_games(vibes=["roguelike"])
        self.assertEqual(results["results"], [])
        self.assertEqual(results["total_matches"], 0)

    async def test_suggested_platform_respects_hardware_preference(self):
        gid = await seed_game("Multiplat", tags=["roguelike"])
        await add_platform(gid, "steam", playtime_minutes=0)
        await add_platform(gid, "switch2", playtime_minutes=0)
        await db_module.set_meta("hardware_preference", json.dumps(["switch2", "steam"]))
        results = await discover.discover_games(vibes=["roguelike"])
        self.assertEqual(results["results"][0]["suggested_platform"], "switch2")

    async def test_concise_drops_platforms_and_tags(self):
        await make_steam_game("Hades", 1145360, playtime_minutes=0, tags=["roguelike", "action"])
        results = await discover.discover_games(vibes=["roguelike"])
        game = results["results"][0]
        self.assertNotIn("platforms", game)
        self.assertNotIn("tags", game)

    async def test_unplayed_only_includes_unknown_with_null_hours(self):
        # GOG/manual-style NULL-playtime game must surface as a recommendation
        # (not confirmed-played) and must NOT render a misleading 0.0 hours.
        gid = await seed_game("ManualRogue", tags=["roguelike"])
        await add_platform(gid, "gog")  # no playtime -> NULL
        results = await discover.discover_games(vibes=["roguelike"], unplayed_only=True)
        self.assertEqual([g["name"] for g in results["results"]], ["ManualRogue"])
        game = results["results"][0]
        self.assertEqual(game["play_state"], "unknown")
        self.assertIsNone(game["playtime_hours"])

    async def test_offset_and_has_more(self):
        await make_steam_game("Hades", 1, playtime_minutes=0, tags=["roguelike"], metacritic_score=90)
        await make_steam_game("Dead Cells", 2, playtime_minutes=0, tags=["roguelike"], metacritic_score=89)
        results = await discover.discover_games(vibes=["roguelike"], limit=1, offset=1)
        self.assertEqual(len(results["results"]), 1)
        self.assertEqual(results["total_matches"], 2)
        self.assertFalse(results["has_more"])


class MatchSortTests(ToolDBTestCase):
    async def test_ranks_by_tag_affinity_with_match_score(self):
        await make_steam_game("LikedGame", 1, playtime_minutes=0, tags=["roguelike"])
        await make_steam_game("MehGame", 2, playtime_minutes=0, tags=["sports"])
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)
        await set_tag_affinity("sports", affinity_score=0.2, avg_score=3.0, game_count=2)
        results = await discover.discover_games()
        self.assertEqual([g["name"] for g in results["results"]], ["LikedGame", "MehGame"])
        # IDF-weighted mean of the stored (already shrunk) affinities: N=2
        # games and each tag sits on 1, below _IDF_DF_FLOOR, so df is floored
        # and idf = ln(1 + 2/floor); score = affinity·idf / (idf + prior).
        idf = math.log(1.0 + 2.0 / discover._IDF_DF_FLOOR)
        self.assertEqual(
            results["results"][0]["match_score"],
            round(2.5 * idf / (idf + discover._MATCH_PRIOR), 3),
        )
        self.assertEqual(
            results["results"][1]["match_score"],
            round(0.2 * idf / (idf + discover._MATCH_PRIOR), 3),
        )
        self.assertEqual(results["total_matches"], 2)

    async def test_rare_tag_idf_is_floored(self):
        # A tag on one library game is not maximally informative, it is
        # unmeasured. Without the floor its idf ceiling (ln(1+N)) dwarfs every
        # measured tag's — the rarity half of the double-counting bug.
        for i in range(30):
            await make_steam_game(f"Filler{i}", 100 + i, playtime_minutes=600, tags=["common"])
        await make_steam_game("RareTag", 1, playtime_minutes=0, tags=["unique"])
        await set_tag_affinity("unique", affinity_score=1.0, avg_score=9.0, game_count=1)

        results = await discover.discover_games(unplayed_only=False)

        rare = next(g for g in results["results"] if g["name"] == "RareTag")
        n = 31.0
        idf = math.log(1.0 + n / discover._IDF_DF_FLOOR)
        self.assertEqual(
            rare["match_score"], round(1.0 * idf / (idf + discover._MATCH_PRIOR), 3)
        )
        # Unfloored it would have been ln(1 + 31/1) — materially higher.
        self.assertLess(idf, math.log(1.0 + n))

    async def test_single_ubiquitous_tag_cannot_dominate(self):
        # The "100% match on a single 'action' tag" failure mode: a game whose
        # only scored tag is one common loved tag must rank below a game whose
        # whole (rarer) tag set matches the profile.
        for i, name in enumerate(["Filler1", "Filler2", "Filler3"]):
            await make_steam_game(name, 100 + i, playtime_minutes=600, tags=["action"])
        await make_steam_game("OneTagWonder", 1, playtime_minutes=0, tags=["action"])
        await make_steam_game(
            "RealFit", 2, playtime_minutes=0, tags=["roguelike", "deckbuilder", "action"]
        )
        await set_tag_affinity("action", affinity_score=2.0, avg_score=8.0, game_count=5)
        await set_tag_affinity("roguelike", affinity_score=1.8, avg_score=8.5, game_count=3)
        await set_tag_affinity("deckbuilder", affinity_score=1.6, avg_score=8.4, game_count=2)

        results = await discover.discover_games()

        names = [g["name"] for g in results["results"]]
        self.assertLess(names.index("RealFit"), names.index("OneTagWonder"))

    async def test_single_rare_low_support_tag_cannot_dominate(self):
        # The "Down in Bermuda" failure mode: a game whose ONLY tag is a rare
        # IGDB keyword ("dinosaurs") loved via 2 ratings used to get both a
        # huge IDF and a near-ceiling affinity, and topped every match ranking.
        # Affinity shrinkage (which now lands such a tag near zero upstream),
        # the IDF floor, and the prior must rank the rich profile above it.
        # The affinities below are still deliberately generous to the rare tag:
        # even un-shrunk, it must not win.
        for i in range(20):
            await make_steam_game(f"Filler{i}", 100 + i, playtime_minutes=600, tags=["action"])
        await make_steam_game("RareKeywordOnly", 1, playtime_minutes=0, tags=["dinosaurs"])
        await make_steam_game(
            "RichProfileFit", 2, playtime_minutes=0,
            tags=["roguelike", "deckbuilder", "narrative"],
        )
        await set_tag_affinity("dinosaurs", affinity_score=1.6, avg_score=10.0, game_count=2)
        await set_tag_affinity("roguelike", affinity_score=1.0, avg_score=8.5, game_count=13)
        await set_tag_affinity("deckbuilder", affinity_score=1.1, avg_score=8.4, game_count=6)
        await set_tag_affinity("narrative", affinity_score=1.1, avg_score=8.9, game_count=12)

        results = await discover.discover_games()

        names = [g["name"] for g in results["results"]]
        self.assertLess(names.index("RichProfileFit"), names.index("RareKeywordOnly"))

    async def test_unrated_tags_dilute_the_match(self):
        # A game stuffed with tags the profile knows nothing about is a weaker
        # bet than a lean game whose every tag is loved — unrated tags count
        # as neutral in the denominator instead of being ignored.
        await make_steam_game("PureFit", 1, playtime_minutes=0, tags=["roguelike"])
        await make_steam_game(
            "DilutedFit",
            2,
            playtime_minutes=0,
            tags=["roguelike", "mystery1", "mystery2", "mystery3", "mystery4"],
        )
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)

        results = await discover.discover_games()

        self.assertEqual(
            [g["name"] for g in results["results"]], ["PureFit", "DilutedFit"]
        )

    async def test_excludes_games_without_affinity_tags(self):
        await make_steam_game("NoAffinity", 1, playtime_minutes=0, tags=["obscure"])
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)
        results = await discover.discover_games()
        self.assertEqual(results["results"], [])

    async def test_empty_affinity_includes_sync_hint(self):
        await make_steam_game("Anything", 1, playtime_minutes=0, tags=["roguelike"])
        results = await discover.discover_games()
        self.assertEqual(results["results"], [])
        self.assertIn('sync(targets=["ratings"])', results["note"])

    async def test_populated_affinity_has_no_note(self):
        await make_steam_game("LikedGame", 1, playtime_minutes=0, tags=["roguelike"])
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)
        results = await discover.discover_games()
        self.assertNotIn("note", results)

    async def test_matched_tags_explain_ranking(self):
        await make_steam_game(
            "LikedGame", 1, playtime_minutes=0, tags=["roguelike", "indie", "action", "sports"]
        )
        await set_tag_affinity("roguelike", affinity_score=2.5, avg_score=9.0, game_count=4)
        await set_tag_affinity("indie", affinity_score=2.0, avg_score=8.5, game_count=3)
        await set_tag_affinity("action", affinity_score=1.5, avg_score=8.0, game_count=2)
        await set_tag_affinity("sports", affinity_score=0.2, avg_score=3.0, game_count=1)
        results = await discover.discover_games()
        matched = results["results"][0]["matched_tags"]
        # Top 3 by affinity, descending — the lowest tag is cut.
        self.assertEqual([m["tag"] for m in matched], ["roguelike", "indie", "action"])
        self.assertEqual(matched[0]["affinity_score"], 2.5)


class CriticAndValueSortTests(ToolDBTestCase):
    async def test_critic_sort_prefers_opencritic_over_metacritic(self):
        await make_steam_game("OCWinner", 1, playtime_minutes=0, opencritic_score=92, metacritic_score=70)
        await make_steam_game("MCOnly", 2, playtime_minutes=0, metacritic_score=85)
        await make_steam_game("Unscored", 3, playtime_minutes=0)
        results = await discover.discover_games(sort_by="critic")
        names = [g["name"] for g in results["results"]]
        self.assertEqual(names, ["OCWinner", "MCOnly", "Unscored"])

    async def test_value_sort_requires_score_and_hltb(self):
        await make_steam_game(
            "ShortGem", 1, playtime_minutes=0, opencritic_score=91, hltb_main=8.0
        )
        await make_steam_game(
            "LongGem", 2, playtime_minutes=0, opencritic_score=91, hltb_main=60.0
        )
        await make_steam_game("NoHltb", 3, playtime_minutes=0, opencritic_score=95)
        results = await discover.discover_games(sort_by="value")
        names = [g["name"] for g in results["results"]]
        self.assertEqual(names, ["ShortGem", "LongGem"])
        self.assertEqual(results["results"][0]["value_note"], "91 on OpenCritic, ~8h")

    async def test_value_note_falls_back_to_metacritic(self):
        await make_steam_game(
            "MCGem", 1, playtime_minutes=0, metacritic_score=88, hltb_main=12.0
        )
        results = await discover.discover_games(sort_by="value")
        self.assertEqual(results["results"][0]["value_note"], "88 on Metacritic, ~12h")

    async def test_min_score_filters_on_coalesced_critic_score(self):
        await make_steam_game("Great", 1, playtime_minutes=0, opencritic_score=90)
        await make_steam_game("Mid", 2, playtime_minutes=0, metacritic_score=70)
        await make_steam_game("Unscored", 3, playtime_minutes=0)
        results = await discover.discover_games(sort_by="critic", min_score=80)
        self.assertEqual([g["name"] for g in results["results"]], ["Great"])

    async def test_unknown_sort_raises(self):
        with self.assertRaisesRegex(ToolError, "Unknown sort_by"):
            await discover.discover_games(sort_by="chaos")


class VibeHintTests(ToolDBTestCase):
    async def test_unknown_vibe_with_no_matches_gets_hint(self):
        results = await discover.discover_games(vibes=["totally-not-a-vibe"])
        self.assertEqual(results["total_matches"], 0)
        self.assertIn("Known vibes", results["note"])

    async def test_known_vibe_no_note(self):
        await make_steam_game("Hades", 1, playtime_minutes=0, tags=["roguelike"])
        results = await discover.discover_games(vibes=["roguelike"])
        self.assertNotIn("note", results)


class WishlistOnlyExclusionTests(ToolDBTestCase):
    async def test_wishlist_only_game_never_recommended(self):
        # A wishlist sync creates a games row + game_wishlist row with zero
        # game_platforms rows. Even with a matching tag and populated
        # affinity, discover_games must never recommend it — is_primary_
        # library_item is a content-type flag (game vs DLC), not ownership.
        gid = await seed_game("Persona 3 Reload", tags=["rpg"])
        await db_module.upsert_wishlist_entry(gid, "switch2", source="dekudeals")
        await make_steam_game("Hollow Knight", 1, playtime_minutes=0, tags=["rpg"])
        await set_tag_affinity("rpg", affinity_score=2.5, avg_score=9.0, game_count=2)

        results = await discover.discover_games()

        names = [g["name"] for g in results["results"]]
        self.assertNotIn("Persona 3 Reload", names)
        self.assertIn("Hollow Knight", names)

    async def test_unowned_stub_playtime_does_not_hide_unplayed_game(self):
        # An owned=0 stub's 600 minutes must not mark an otherwise-unplayed
        # game 'played' — it would silently vanish from unplayed_only
        # recommendations despite never actually being played.
        gid = await make_steam_game("Doom", 1, playtime_minutes=0, tags=["shooter"])
        await add_platform(gid, "epic", playtime_minutes=600, owned=0)

        results = await discover.discover_games(vibes=["shooter"])

        self.assertEqual([g["name"] for g in results["results"]], ["Doom"])
        game = results["results"][0]
        self.assertEqual(game["play_state"], "unplayed")
        self.assertEqual(game["playtime_hours"], 0.0)


class CompletionStatusExclusionTests(ToolDBTestCase):
    async def test_discover_excludes_abandoned_and_completed(self):
        gid_abandoned = await make_steam_game(
            "Starfield", 1, playtime_minutes=0, tags=["rpg"]
        )
        await update_game(game_id=gid_abandoned, completion_status="abandoned")
        gid_completed = await make_steam_game(
            "Outer Wilds", 2, playtime_minutes=0, tags=["rpg"]
        )
        await update_game(game_id=gid_completed, completion_status="completed")
        await make_steam_game("Hollow Knight", 3, playtime_minutes=0, tags=["rpg"])

        results = await discover.discover_games(vibes=["rpg"])
        names = [g["name"] for g in results["results"]]
        self.assertNotIn("Starfield", names)
        self.assertNotIn("Outer Wilds", names)
        self.assertIn("Hollow Knight", names)

    async def test_discover_excludes_even_when_unplayed_only_false(self):
        gid = await make_steam_game("Abandoned Game", 1, playtime_minutes=200)
        await update_game(game_id=gid, completion_status="abandoned")
        results = await discover.discover_games(unplayed_only=False, sort_by="critic")
        names = [g["name"] for g in results["results"]]
        self.assertNotIn("Abandoned Game", names)

    async def test_discover_does_not_exclude_evergreen(self):
        # Unlike completed/abandoned, an evergreen game (no completion concept)
        # is still a legitimate recommendation candidate.
        gid = await make_steam_game(
            "Rocket League", 1, playtime_minutes=6000, tags=["rpg"]
        )
        await update_game(game_id=gid, completion_status="evergreen")
        results = await discover.discover_games(
            vibes=["rpg"], unplayed_only=False, sort_by="critic"
        )
        names = [g["name"] for g in results["results"]]
        self.assertIn("Rocket League", names)
