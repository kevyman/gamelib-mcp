"""Orphan garbage collection for games rows a wishlist sync un-references.

``delete_stale_wishlist_entries`` deletes the ``game_wishlist`` row of a title
removed upstream and used to leave its ``games`` row behind pointing at nothing
— the residue ``check_library``'s ``ownership.orphan`` reports. These tests pin
both halves: what ``delete_unreferenced_games`` will and (mostly) will not
delete, and that both wishlist syncs run it over exactly the rows they just
un-wishlisted, behind the unchanged complete-fetch guard.
"""

import unittest
from typing import ClassVar
from unittest.mock import AsyncMock, patch

from conftest import (
    ToolDBTestCase,
    add_assessment,
    add_platform,
    add_rating,
    seed_game,
)

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import dekudeals, steam_wishlist


class DeleteUnreferencedGamesTests(ToolDBTestCase):
    async def test_deletes_a_row_nothing_points_at(self):
        bare = await seed_game("Un-Wishlisted Forever")

        deleted = await db_module.delete_unreferenced_games([bare])

        self.assertEqual(deleted, [bare])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (bare,))
        self.assertIsNone(row)

    async def test_only_considers_the_ids_it_was_given(self):
        kept = await seed_game("Someone Else's Orphan")
        target = await seed_game("Mine")

        deleted = await db_module.delete_unreferenced_games([target])

        self.assertEqual(deleted, [target])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (kept,))
        self.assertIsNotNone(row)

    async def test_empty_input_is_a_no_op(self):
        self.assertEqual(await db_module.delete_unreferenced_games([]), [])

    async def _assert_kept(self, game_id: int) -> None:
        self.assertEqual(await db_module.delete_unreferenced_games([game_id]), [])
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (game_id,))
        self.assertIsNotNone(row)

    async def test_keeps_a_row_with_an_owned_platform(self):
        game = await seed_game("Owned On Steam")
        await add_platform(game, "steam")
        await self._assert_kept(game)

    async def test_keeps_a_row_whose_ownership_was_retired(self):
        # ADR 0007: owned=0 + unowned_at keeps the history on purpose.
        game = await seed_game("Refunded")
        pid = await add_platform(game, "steam", owned=0)
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET unowned_at = '2026-01-01' WHERE id = ?", (pid,)
            )
            await db.commit()
        await self._assert_kept(game)

    async def test_keeps_a_row_still_wishlisted_elsewhere(self):
        game = await seed_game("Wanted On Switch Too")
        await db_module.upsert_wishlist_entry(game, "switch2", source="manual")
        await self._assert_kept(game)

    async def test_keeps_a_row_with_a_recorded_assessment(self):
        # Deleting it would erase the verdict the calibration report reads.
        game = await seed_game("Evaluated And Skipped")
        await add_assessment(game, verdict="skip", steam_appid=440)
        await self._assert_kept(game)

    async def test_keeps_a_row_an_assessment_points_at_as_instead_game(self):
        game = await seed_game("Play This Instead")
        other = await seed_game("The Candidate")
        assessment_id = await add_assessment(other, verdict="skip")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_assessments SET instead_game_id = ? WHERE id = ?",
                (game, assessment_id),
            )
            await db.commit()
        await self._assert_kept(game)

    async def test_keeps_a_rated_row(self):
        game = await seed_game("Rated")
        await add_rating(game, "manual", 8.0, 8.0)
        await self._assert_kept(game)

    async def test_keeps_a_row_with_playtime_history(self):
        game = await seed_game("Played Once")
        async with db_module.get_db() as db:
            await db.execute(
                """INSERT INTO play_history
                   (game_id, platform, snapshot_date, playtime_minutes)
                   VALUES (?, 'steam', '2026-01-01', 120)""",
                (game,),
            )
            await db.commit()
        await self._assert_kept(game)

    async def test_keeps_a_parent_of_nested_content(self):
        parent = await seed_game("Base Game")
        await seed_game(
            "Base Game - DLC",
            content_type="dlc",
            is_primary_library_item=0,
            parent_game_id=parent,
        )
        await self._assert_kept(parent)

    async def test_keeps_a_hand_edited_row(self):
        # A non-empty manual_overrides is a human statement about the row.
        game = await seed_game("Hand Corrected")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET manual_overrides = '[\"name\"]' WHERE id = ?", (game,)
            )
            await db.commit()
        await self._assert_kept(game)

    async def test_an_empty_overrides_array_is_not_a_reference(self):
        game = await seed_game("Overrides Cleared")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET manual_overrides = '[]' WHERE id = ?", (game,)
            )
            await db.commit()
        self.assertEqual(await db_module.delete_unreferenced_games([game]), [game])


def _steam_client(items):
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": {"items": items}}

    class _Client:
        def __init__(self):
            self.get = AsyncMock(return_value=_Resp())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return _Client()


class SteamWishlistSyncGcTests(ToolDBTestCase):
    # appid -> the name the store lookup would return. A wishlist-only item has
    # no platform row to resolve through, so the first sync names it this way.
    NAMES: ClassVar[dict[int, str]] = {
        111: "Still Wanted",
        222: "Dropped From The Wishlist",
    }

    async def _sync(self, items):
        async def _name(appid, *a, **kw):
            return self.NAMES[int(appid)]

        with (
            patch.object(steam_wishlist, "STEAM_API_KEY", "key"),
            patch.object(steam_wishlist, "STEAM_ID", "id"),
            patch.object(
                steam_wishlist.httpx, "AsyncClient", return_value=_steam_client(items)
            ),
            patch.object(steam_wishlist, "fetch_app_name", _name),
        ):
            return await steam_wishlist.fetch_wishlist()

    async def test_removed_entry_takes_its_bare_games_row_with_it(self):
        first = await self._sync([{"appid": 111}, {"appid": 222}])
        self.assertEqual(first["added"], 2)
        async with db_module.get_db() as db:
            dropped = await db.execute_fetchone(
                "SELECT game_id FROM game_wishlist WHERE store_identifier = '222'"
            )
        dropped_id = dropped["game_id"]

        result = await self._sync([{"appid": 111}])

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["orphans_removed"], 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (dropped_id,))
        self.assertIsNone(row)

    async def test_removed_entry_with_a_rating_keeps_its_row(self):
        await self._sync([{"appid": 111}, {"appid": 222}])
        async with db_module.get_db() as db:
            dropped = await db.execute_fetchone(
                "SELECT game_id FROM game_wishlist WHERE store_identifier = '222'"
            )
        dropped_id = dropped["game_id"]
        await add_rating(dropped_id, "manual", 9.0, 9.0)

        result = await self._sync([{"appid": 111}])

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["orphans_removed"], 0)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (dropped_id,))
        self.assertIsNotNone(row)

    async def test_nothing_removed_means_nothing_collected(self):
        await self._sync([{"appid": 111}])
        result = await self._sync([{"appid": 111}])
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["orphans_removed"], 0)


class DekuDealsWishlistSyncGcTests(ToolDBTestCase):
    URL = "https://www.dekudeals.com/wishlist/abc"

    async def _sync(self, titles):
        items = [{"title": title, "added_at": "2026-01-01T00:00:00+00:00"} for title in titles]
        with (
            patch.object(dekudeals, "DEKUDEALS_WISHLIST_URL", self.URL),
            patch.object(dekudeals, "_fetch_wishlist_items", AsyncMock(return_value=items)),
        ):
            return await dekudeals.sync_dekudeals_wishlist()

    async def test_removed_title_takes_its_bare_games_row_with_it(self):
        await self._sync(["Pikmin 4", "Metroid Dread"])
        async with db_module.get_db() as db:
            dropped = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = 'Metroid Dread'"
            )
        dropped_id = dropped["id"]

        result = await self._sync(["Pikmin 4"])

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["orphans_removed"], 1)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (dropped_id,))
        self.assertIsNone(row)

    async def test_removed_title_that_is_owned_keeps_its_row(self):
        await self._sync(["Pikmin 4", "Metroid Dread"])
        async with db_module.get_db() as db:
            dropped = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = 'Metroid Dread'"
            )
        dropped_id = dropped["id"]
        await add_platform(dropped_id, "switch2")

        result = await self._sync(["Pikmin 4"])

        self.assertEqual(result["removed"], 1)
        self.assertEqual(result["orphans_removed"], 0)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone("SELECT id FROM games WHERE id = ?", (dropped_id,))
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
