"""Wishlist identity must reach IGDB's store-ID resolution path."""

from unittest.mock import AsyncMock, patch

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import igdb


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
                external.assert_awaited_once_with(["2525340"])
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

    async def test_other_stores_identifiers_are_not_steam_appids(self) -> None:
        game_id = await seed_game("Switch wishlist")
        await db_module.upsert_wishlist_entry(game_id, "switch2", store_identifier="200")
        await db_module.upsert_wishlist_entry(game_id, "steam")

        rows = await db_module.load_games_for_igdb_backfill([game_id])

        self.assertIsNone(rows[0]["steam_appid"])
