"""Wishlist identity must reach IGDB's store-ID resolution path."""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_assessment, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import igdb
from gamelib_mcp.data.igdb import IGDB_RESOLVER_VERSION


class IGDBWishlistIdentityTests(ToolDBTestCase):
    async def test_backfill_uses_wishlist_appid_for_same_name_games(self) -> None:
        game_id = await seed_game("Dispatch")
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="steam", store_identifier="2525340"
        )
        correct = igdb.IGDBGame(
            igdb_id=123,
            name="Dispatch",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2025-10-22",
            platforms=[6],
        )
        wrong = igdb.IGDBGame(
            igdb_id=456,
            name="Dispatch",
            category=igdb.CATEGORY_MAIN_GAME,
            first_release_date="2020-01-01",
            platforms=[6],
        )

        # Exercise both a new wishlist item and an old name-based mislink.
        for existing_id in (None, wrong.igdb_id):
            with self.subTest(existing_id=existing_id):
                async with db_module.get_db() as db:
                    await db.execute(
                        "UPDATE games SET igdb_id = ?, igdb_cached_at = NULL WHERE id = ?",
                        (existing_id, game_id),
                    )
                    await db.commit()

                with (
                    patch.dict(
                        "os.environ",
                        {"TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "secret"},
                    ),
                    patch.object(
                        igdb, "resolve_steam_appids_to_igdb",
                        AsyncMock(return_value={"2525340": correct.igdb_id}),
                    ) as external,
                    patch.object(
                        igdb, "fetch_game_by_id",
                        AsyncMock(side_effect=lambda game_id, **_: {
                            correct.igdb_id: correct, wrong.igdb_id: wrong,
                        }[game_id]),
                    ),
                    patch.object(
                        igdb, "_resolve_game_with_status",
                        AsyncMock(return_value=igdb._ResolveOutcome(wrong, True)),
                    ) as name_search,
                ):
                    processed = await igdb.backfill_missing_games(limit=1)

                self.assertEqual(processed, 1)
                row = await db_module.get_game_by_name_exact("Dispatch")
                self.assertEqual(row["igdb_id"], correct.igdb_id)
                external.assert_awaited_once_with(["2525340", "292030"])
                name_search.assert_not_awaited()
                async with db_module.get_db() as db:
                    count = await db.execute_fetchone("SELECT COUNT(*) AS n FROM game_platforms")
                self.assertEqual(count["n"], 0)

    async def test_platform_appid_takes_precedence_over_wishlist(self) -> None:
        game_id = await seed_game("Owned game")
        platform_id = await add_platform(game_id, "steam")
        await db_module.upsert_game_platform_identifier(platform_id, db_module.STEAM_APP_ID, "100")
        await db_module.upsert_wishlist_entry(game_id, "steam", store_identifier="200")

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertEqual(rows[0]["steam_appid"], "100")

    async def test_wishlist_fallback_works_when_owned_on_another_platform(self) -> None:
        game_id = await seed_game("Cross-platform game")
        await add_platform(game_id, "ps5")
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="assessment", store_identifier="200"
        )

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertEqual(rows[0]["steam_appid"], "200")

    async def test_assessment_appid_is_the_last_fallback(self) -> None:
        # Same shape as the wishlist arm, one table over: record_assessment
        # mints an ownership-free row and stores the appid on the assessment,
        # so without this arm the backfill sends an exact store identity
        # through name matching.
        game_id = await seed_game("Assessed Candidate")
        await add_assessment(game_id, steam_appid=2132850)

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        # Typed like the identifier arm it substitutes for (TEXT), since the
        # external_games batch stringifies every appid anyway.
        self.assertEqual(rows[0]["steam_appid"], "2132850")

    async def test_wishlist_identity_outranks_the_assessment_one(self) -> None:
        game_id = await seed_game("Both Shapes")
        await db_module.upsert_wishlist_entry(
            game_id, "steam", source="steam", store_identifier="200"
        )
        await add_assessment(game_id, steam_appid=300)

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertEqual(rows[0]["steam_appid"], "200")

    async def test_the_gog_product_id_is_loaded_for_the_backfill(self) -> None:
        # IGDB maps GOG product ids through external_games (category 5) exactly
        # as it maps Steam appids, and GOG rows are the ones name resolution
        # serves worst — 107 of 131 GOG-identified rows were unlinked.
        game_id = await seed_game("The Witcher 3: Wild Hunt")
        platform_id = await add_platform(game_id, "gog")
        await db_module.upsert_game_platform_identifier(
            platform_id, db_module.GOG_PRODUCT_ID, "1207664663"
        )

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertEqual(rows[0]["gog_product_id"], "1207664663")
        self.assertIsNone(rows[0]["steam_appid"])

    async def test_a_row_without_a_gog_identifier_loads_none(self) -> None:
        game_id = await seed_game("Steam Only")
        platform_id = await add_platform(game_id, "steam")
        await db_module.upsert_game_platform_identifier(
            platform_id, db_module.STEAM_APP_ID, "100"
        )

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertIsNone(rows[0]["gog_product_id"])
        self.assertEqual(rows[0]["steam_appid"], "100")

    async def test_scoped_claim_claims_only_the_named_rows(self) -> None:
        wanted = await seed_game("Wanted")
        other = await seed_game("Other")

        claimed = await db_module.claim_game_ids_for_igdb(
            limit=10,
            stale_before="1970-01-01T00:00:00+00:00",
            game_ids=[wanted],
            resolver_version=IGDB_RESOLVER_VERSION,
        )

        self.assertEqual(claimed, [wanted])
        # …and the row it left alone is still claimable.
        self.assertEqual(
            await db_module.claim_game_ids_for_igdb(
                limit=10,
                stale_before="1970-01-01T00:00:00+00:00",
                resolver_version=IGDB_RESOLVER_VERSION,
            ),
            [other],
        )

    async def test_other_stores_identifiers_are_not_steam_appids(self) -> None:
        game_id = await seed_game("Switch wishlist")
        await db_module.upsert_wishlist_entry(game_id, "switch2", store_identifier="200")
        await db_module.upsert_wishlist_entry(game_id, "steam")

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertIsNone(rows[0]["steam_appid"])
