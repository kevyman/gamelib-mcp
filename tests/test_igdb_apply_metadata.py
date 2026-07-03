"""Regression tests for content-classification persistence in _apply_igdb_metadata.

A later IGDB pass can return a bare main-game hit (default "base_game"/primary,
no parent). Applying that must not clobber a game already classified as nested
DLC/expansion, which would resurface it as its own primary library item.
"""

import json

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import igdb


async def _read_tags(game_id: int) -> list[str]:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone("SELECT tags FROM games WHERE id = ?", (game_id,))
    return json.loads(row["tags"]) if row["tags"] else []


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


    async def test_self_referential_parent_is_dropped_and_kept_primary(self) -> None:
        # IGDB returns an "edition" whose parent name resolves back to this same
        # row. We must not write parent_game_id = game_id (which would orphan the
        # row from both search and its parent's editions list); instead drop the
        # self-parent and keep it a primary library item.
        game_id = await seed_game("The House in Fata Morgana")

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=3000,
                name="The House in Fata Morgana",
                category=0,
                first_release_date=None,
                content_type="edition",
                parent_name="The House in Fata Morgana",
                is_primary_library_item=False,
            ),
        )

        after = await _read_classification(game_id)
        self.assertIsNone(after["parent_game_id"])
        self.assertEqual(after["is_primary_library_item"], 1)


class UpsertGameSelfParentTests(ToolDBTestCase):
    async def test_upsert_game_drops_self_referencing_parent(self) -> None:
        game_id = await seed_game("Some Edition")

        returned_id = await db_module.upsert_game(
            appid=None,
            name="Some Edition",
            content_type="edition",
            parent_game_id=game_id,
            is_primary_library_item=0,
        )

        self.assertEqual(returned_id, game_id)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT parent_game_id, is_primary_library_item FROM games WHERE id = ?",
                (game_id,),
            )
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)


class ApplyIgdbMetadataTagUnionTests(ToolDBTestCase):
    async def test_igdb_tags_union_into_existing_steam_tags(self) -> None:
        # Existing (SteamSpy) tags must be kept, IGDB tags appended canonicalized,
        # with case-insensitive dedup — not replaced or skipped.
        game_id = await seed_game("Sekiro", tags=["souls-like", "difficult"])

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=2000,
                name="Sekiro",
                category=0,
                first_release_date=None,
                tags=["Parrying", "Difficult", "Action-Adventure"],
            ),
        )

        tags = await _read_tags(game_id)
        # existing first, then new IGDB tags (canonicalized, "Difficult" deduped)
        self.assertEqual(tags, ["souls-like", "difficult", "parrying", "action-adventure"])

    async def test_igdb_tags_seed_when_empty(self) -> None:
        game_id = await seed_game("Hollow Knight")  # tags NULL

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=2001,
                name="Hollow Knight",
                category=0,
                first_release_date=None,
                tags=["Metroidvania", "Atmospheric"],
            ),
        )

        self.assertEqual(await _read_tags(game_id), ["metroidvania", "atmospheric"])

    async def test_igdb_tag_union_filters_feature_flags_and_caps(self) -> None:
        existing = [f"tag{i}" for i in range(igdb.MERGED_TAG_CAP - 1)]
        game_id = await seed_game("Big", tags=existing)

        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=2002,
                name="Big",
                category=0,
                first_release_date=None,
                tags=["Steam Trading Cards", "metroidvania", "atmospheric", "exploration"],
            ),
        )

        tags = await _read_tags(game_id)
        self.assertEqual(len(tags), igdb.MERGED_TAG_CAP)
        self.assertNotIn("steam trading cards", tags)
        self.assertEqual(tags[: len(existing)], existing)  # existing kept, in order
        self.assertEqual(tags[-1], "metroidvania")  # first IGDB tag fills last slot


class ApplyIgdbMetadataPlatformsTests(ToolDBTestCase):
    async def test_writes_igdb_platforms_json(self) -> None:
        game_id = await seed_game("Crossplay Game")
        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=901,
                name="Crossplay Game",
                category=0,
                first_release_date=None,
                platforms=[6, 130, 508],
            ),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (game_id,)
            )
        self.assertEqual(json.loads(row["igdb_platforms"]), [6, 130, 508])

    async def test_empty_platforms_leaves_column_null(self) -> None:
        game_id = await seed_game("No Platform Data")
        await igdb._apply_igdb_metadata(
            game_id,
            igdb.IGDBGame(
                igdb_id=902,
                name="No Platform Data",
                category=0,
                first_release_date=None,
                platforms=[],
            ),
        )
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (game_id,)
            )
        self.assertIsNone(row["igdb_platforms"])
