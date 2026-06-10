"""Direct tests for the split-out db query/claim/upsert submodules.

These exercise load_platforms_for_games (queries.py) and a claim round-trip
(claims.py) through the gamelib_mcp.data.db facade, pinning the submodule wiring
after the package split.
"""

from conftest import (
    ToolDBTestCase,
    add_enrichment,
    add_platform,
    add_steam_appid,
    add_steam_data,
    seed_game,
)
from gamelib_mcp.data import db as db_module


class LoadPlatformsForGamesTests(ToolDBTestCase):
    async def test_groups_platforms_with_identifiers_and_enrichment(self):
        gid = await seed_game("Hades")
        steam_gp = await add_platform(gid, "steam", playtime_minutes=120)
        await add_steam_appid(steam_gp, 1145360)
        await add_steam_data(steam_gp, steam_review_desc="Overwhelmingly Positive", protondb_tier="platinum")
        await add_enrichment(steam_gp, metacritic_score=93)
        await add_platform(gid, "switch2", playtime_minutes=300)

        result = await db_module.load_platforms_for_games([gid])
        platforms = {p["platform"]: p for p in result[gid]}
        self.assertEqual(set(platforms), {"steam", "switch2"})

        steam = platforms["steam"]
        self.assertEqual(steam["identifiers"]["steam_appid"], 1145360)
        self.assertEqual(steam["provider_data"]["protondb_tier"], "platinum")
        self.assertEqual(steam["provider_data"]["steam_review_desc"], "Overwhelmingly Positive")
        self.assertEqual(steam["metacritic_score"], 93)
        self.assertEqual(platforms["switch2"]["playtime_minutes"], 300)

    async def test_returns_empty_for_unknown_game(self):
        result = await db_module.load_platforms_for_games([999])
        self.assertEqual(result, {})


class ClaimRoundTripTests(ToolDBTestCase):
    async def test_claim_then_release_allows_reclaim(self):
        gid = await seed_game("Portal")
        past = "1970-01-01T00:00:00+00:00"

        first = await db_module.claim_game_ids_for_igdb(limit=5, stale_before=past)
        self.assertIn(gid, first)

        # Already claimed -> not returned again.
        second = await db_module.claim_game_ids_for_igdb(limit=5, stale_before=past)
        self.assertEqual(second, [])

        # After releasing the claim, it can be claimed once more.
        await db_module.release_game_claim(gid, "igdb_claimed_at")
        third = await db_module.claim_game_ids_for_igdb(limit=5, stale_before=past)
        self.assertIn(gid, third)
