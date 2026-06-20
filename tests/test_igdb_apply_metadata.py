"""Regression tests for content-classification persistence in _apply_igdb_metadata.

A later IGDB pass can return a bare main-game hit (default "base_game"/primary,
no parent). Applying that must not clobber a game already classified as nested
DLC/expansion, which would resurface it as its own primary library item.
"""

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import igdb


async def _read_classification(game_id: int) -> dict:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            "SELECT content_type, parent_game_id, is_primary_library_item "
            "FROM games WHERE id = ?",
            (game_id,),
        )
    return dict(row)


class ApplyIgdbMetadataGuardTests(ToolDBTestCase):
    async def test_default_refetch_preserves_nested_classification(self) -> None:
        parent_id = await seed_game("Fallout: New Vegas")
        dlc_id = await seed_game(
            "Fallout New Vegas: Dead Money",
            content_type="dlc",
            parent_game_id=parent_id,
            is_primary_library_item=0,
        )

        # A bare main-game re-fetch (the default classification) must not flip
        # the DLC back to a primary library item.
        await igdb._apply_igdb_metadata(
            dlc_id,
            igdb.IGDBGame(igdb_id=999, name="Fallout New Vegas: Dead Money", category=0, first_release_date=None),
        )

        after = await _read_classification(dlc_id)
        self.assertEqual(after["content_type"], "dlc")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)

    async def test_nested_classification_still_applies_to_default_row(self) -> None:
        parent_id = await seed_game("Sid Meier's Civilization IV")
        game_id = await seed_game("Sid Meier's Civilization IV: Warlords")

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=1000,
                name="Sid Meier's Civilization IV: Warlords",
                category=2,
                first_release_date=None,
                content_type="expansion",
                parent_name="Sid Meier's Civilization IV",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(game_id)
        self.assertEqual(after["content_type"], "expansion")
        self.assertEqual(after["parent_game_id"], parent_id)
        self.assertEqual(after["is_primary_library_item"], 0)
