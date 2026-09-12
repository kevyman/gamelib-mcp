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
from gamelib_mcp.data.igdb import IGDB_RESOLVER_VERSION


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

        first = await db_module.claim_game_ids_for_igdb(
            limit=5, stale_before=past, resolver_version=IGDB_RESOLVER_VERSION
        )
        self.assertIn(gid, first)

        # Already claimed -> not returned again.
        second = await db_module.claim_game_ids_for_igdb(
            limit=5, stale_before=past, resolver_version=IGDB_RESOLVER_VERSION
        )
        self.assertEqual(second, [])

        # After releasing the claim, it can be claimed once more.
        await db_module.release_game_claim(gid, "igdb_claimed_at")
        third = await db_module.claim_game_ids_for_igdb(
            limit=5, stale_before=past, resolver_version=IGDB_RESOLVER_VERSION
        )
        self.assertIn(gid, third)


class ResolverVersionClaimTests(ToolDBTestCase):
    """A no-match stamp is only permanent while the resolver stands still."""

    _STAMP = "2026-01-01T00:00:00+00:00"
    _PAST = "1970-01-01T00:00:00+00:00"

    async def _stamp(self, game_id: int, *, version: int | None, igdb_id: int | None = None):
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_cached_at = ?, igdb_resolver_version = ?, "
                "igdb_id = ? WHERE id = ?",
                (self._STAMP, version, igdb_id, game_id),
            )
            await db.commit()

    async def test_a_stale_no_match_is_claimable_and_a_current_one_is_not(self):
        stale = await seed_game("Stale No Match")
        current = await seed_game("Current No Match")
        await self._stamp(stale, version=1)
        await self._stamp(current, version=2)

        claimed = await db_module.claim_game_ids_for_igdb(
            limit=10, stale_before=self._PAST, resolver_version=2
        )

        self.assertIn(stale, claimed)
        self.assertNotIn(current, claimed)

    async def test_a_no_match_predating_the_column_is_claimable(self):
        unversioned = await seed_game("Unversioned No Match")
        await self._stamp(unversioned, version=None)

        claimed = await db_module.claim_game_ids_for_igdb(
            limit=10, stale_before=self._PAST, resolver_version=2
        )

        self.assertIn(unversioned, claimed)

    async def test_a_linked_row_is_never_reclaimed_by_a_version_bump(self):
        linked = await seed_game("Linked Long Ago")
        await self._stamp(linked, version=1, igdb_id=4242)

        claimed = await db_module.claim_game_ids_for_igdb(
            limit=10, stale_before=self._PAST, resolver_version=2
        )

        self.assertNotIn(linked, claimed)

    async def test_never_checked_rows_are_claimed_before_stale_no_matches(self):
        # A large stale backlog must drain BEHIND new rows, never starve them.
        stale_a = await seed_game("Stale A")
        stale_b = await seed_game("Stale B")
        fresh = await seed_game("Never Checked")
        await self._stamp(stale_a, version=1)
        await self._stamp(stale_b, version=1)

        claimed = await db_module.claim_game_ids_for_igdb(
            limit=10, stale_before=self._PAST, resolver_version=2
        )

        self.assertEqual(claimed[0], fresh)
        self.assertEqual(sorted(claimed[1:]), sorted([stale_a, stale_b]))

    async def test_the_game_ids_scope_still_narrows_the_claim(self):
        wanted = await seed_game("Wanted")
        other = await seed_game("Other")
        await self._stamp(wanted, version=1)

        claimed = await db_module.claim_game_ids_for_igdb(
            limit=10, stale_before=self._PAST, game_ids=[wanted], resolver_version=2
        )

        self.assertEqual(claimed, [wanted])
        self.assertNotIn(other, claimed)


class SeedPlatformProviderAliasTests(ToolDBTestCase):
    """seed_platform_provider_alias — enrichment-time alias seeding (bug-report §4)."""

    async def _aliases(self, game_id: int) -> list[dict]:
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                "SELECT alias, alias_type, source, source_key FROM game_aliases "
                "WHERE game_id = ? ORDER BY id",
                (game_id,),
            )
        return [dict(r) for r in rows]

    async def test_differing_provider_name_is_recorded(self):
        gid = await seed_game("Orwell")
        gpid = await add_platform(gid, "steam")

        await db_module.seed_platform_provider_alias(
            gpid,
            "orwell keeping an eye on you",
            source="metacritic",
            source_key="orwell-keeping-an-eye-on-you",
        )

        aliases = await self._aliases(gid)
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["alias"], "orwell keeping an eye on you")
        self.assertEqual(aliases[0]["alias_type"], "provider_name")
        self.assertEqual(aliases[0]["source"], "metacritic")

    async def test_equivalent_name_is_skipped(self):
        gid = await seed_game("Sekiro: Shadows Die Twice")
        gpid = await add_platform(gid, "steam")

        # Same title modulo punctuation/case — adds no information.
        await db_module.seed_platform_provider_alias(
            gpid, "sekiro shadows die twice", source="metacritic"
        )

        self.assertEqual(await self._aliases(gid), [])

    async def test_unknown_platform_row_is_a_no_op(self):
        await db_module.seed_platform_provider_alias(
            999999, "some title", source="opencritic"
        )
        async with db_module.get_db() as db:
            count = await db.execute_fetchone("SELECT COUNT(*) AS c FROM game_aliases")
        self.assertEqual(count["c"], 0)
