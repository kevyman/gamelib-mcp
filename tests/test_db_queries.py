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


class LoadRelatedContentForGamesTests(ToolDBTestCase):
    async def test_owned_and_priced_child_hoists_scalars(self):
        parent = await seed_game("Base Game")
        child = await seed_game(
            "Base Game: DLC",
            content_type="dlc",
            parent_game_id=parent,
            is_primary_library_item=0,
        )
        gp = await add_platform(child, "steam", owned=1)
        await db_module.set_platform_acquisition(
            gp,
            {"price_paid": 9.99, "price_currency": "USD", "acquired_at": "2024-01-01"},
        )

        result = await db_module.load_related_content_for_games([parent])
        entry = result[parent]["dlc"][0]
        self.assertIs(entry["owned"], True)
        self.assertEqual(entry["price_paid"], 9.99)
        self.assertEqual(entry["price_currency"], "USD")
        self.assertEqual(entry["acquired_at"], "2024-01-01")

    async def test_unowned_child_reports_no_price(self):
        parent = await seed_game("Base Game 2")
        child = await seed_game(
            "Base Game 2: DLC",
            content_type="dlc",
            parent_game_id=parent,
            is_primary_library_item=0,
        )
        await add_platform(child, "steam", owned=0)

        result = await db_module.load_related_content_for_games([parent])
        entry = result[parent]["dlc"][0]
        self.assertIs(entry["owned"], False)
        self.assertIsNone(entry["price_paid"])
        self.assertIsNone(entry["price_currency"])
        self.assertIsNone(entry["acquired_at"])

    async def test_multi_platform_child_hoists_earliest_priced_acquisition(self):
        parent = await seed_game("Base Game 3")
        child = await seed_game(
            "Base Game 3: DLC",
            content_type="dlc",
            parent_game_id=parent,
            is_primary_library_item=0,
        )
        gp_steam = await add_platform(child, "steam", owned=1)
        await db_module.set_platform_acquisition(
            gp_steam,
            {"price_paid": 5.0, "price_currency": "USD", "acquired_at": "2024-06-01"},
        )
        gp_switch = await add_platform(child, "switch2", owned=1)
        await db_module.set_platform_acquisition(
            gp_switch,
            {"price_paid": 7.0, "price_currency": "USD", "acquired_at": "2024-01-01"},
        )

        result = await db_module.load_related_content_for_games([parent])
        entry = result[parent]["dlc"][0]
        self.assertIs(entry["owned"], True)
        # Earliest acquired_at among owned+priced rows wins the deterministic
        # scalar hoist, regardless of which platform it came from.
        self.assertEqual(entry["price_paid"], 7.0)
        self.assertEqual(entry["acquired_at"], "2024-01-01")

    async def test_regression_grouping_by_content_type_unchanged(self):
        parent = await seed_game("Base Game 4")
        dlc = await seed_game(
            "Base Game 4: DLC",
            content_type="dlc",
            parent_game_id=parent,
            is_primary_library_item=0,
        )
        expansion = await seed_game(
            "Base Game 4: Expansion",
            content_type="expansion",
            parent_game_id=parent,
            is_primary_library_item=0,
        )
        await add_platform(dlc, "epic")
        await add_platform(expansion, "epic")

        result = await db_module.load_related_content_for_games([parent])
        self.assertEqual([e["name"] for e in result[parent]["dlc"]], ["Base Game 4: DLC"])
        self.assertEqual(
            [e["name"] for e in result[parent]["expansions"]], ["Base Game 4: Expansion"]
        )
        self.assertEqual(result[parent]["editions"], [])
        self.assertEqual(result[parent]["bundles"], [])
        self.assertEqual(result[parent]["other"], [])


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
