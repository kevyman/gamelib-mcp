"""merge_games must carry the source's games-level enrichment to the target.

The source row's own columns have no FK and no cascade: they simply vanish with
the DELETE. Merging the duplicate that happened to win the IGDB match into its
identifier-bearing twin therefore dropped the link, the tags, the cover and the
HLTB times without saying so.
"""

import json
import unittest

from conftest import ToolDBTestCase, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools import admin

ENRICHMENT_COLUMNS = (
    "release_date",
    "genres",
    "tags",
    "features",
    "short_description",
    "sort_name",
    "hltb_main",
    "hltb_extra",
    "hltb_complete",
    "hltb_cached_at",
    "igdb_platforms",
    "cover_image_id",
    "completion_status",
    "igdb_id",
    "igdb_cached_at",
    "igdb_resolver_version",
    "igdb_claimed_at",
    "is_farmed",
)


async def set_game_columns(game_id: int, **columns) -> None:
    """Write games columns directly — no manual_overrides, like enrichment."""
    cols_sql = ", ".join(f"{column} = ?" for column in columns)
    async with db_module.get_db() as db:
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*columns.values(), game_id),
        )
        await db.commit()


async def read_game(game_id: int) -> dict | None:
    async with db_module.get_db() as db:
        row = await db.execute_fetchone(
            f"SELECT {', '.join(ENRICHMENT_COLUMNS)} FROM games WHERE id = ?",
            (game_id,),
        )
    return dict(row) if row else None


async def seed_enriched_source(name: str = "Decktamer (duplicate)") -> int:
    """A row carrying the enrichment: IGDB link, tags, cover, HLTB, dates."""
    game_id = await seed_game(name)
    await set_game_columns(
        game_id,
        igdb_id=252001,
        igdb_cached_at="2026-08-01T00:00:00+00:00",
        igdb_resolver_version=4,
        igdb_claimed_at="2026-08-01T00:00:00+00:00",
        igdb_platforms=json.dumps([6]),
        cover_image_id="co7abc",
        tags=json.dumps(["roguelike", "deckbuilder"]),
        genres=json.dumps(["Indie"]),
        features=json.dumps(["Single Player"]),
        release_date="2024-05-16",
        short_description="Monster-taming deckbuilder.",
        sort_name="Decktamer",
        hltb_main=9.5,
        hltb_extra=14.0,
        hltb_complete=22.5,
        hltb_cached_at="2026-07-30T00:00:00+00:00",
        completion_status="playing",
    )
    return game_id


class MergeGamesEnrichmentTests(ToolDBTestCase):
    async def test_bare_target_inherits_every_games_level_column(self) -> None:
        source = await seed_enriched_source()
        target = await seed_game("Decktamer")

        result = await admin.merge_games(source, target)

        self.assertEqual(
            result["game_fields_filled"],
            [
                "completion_status",
                "cover_image_id",
                "features",
                "genres",
                "hltb_cached_at",
                "hltb_complete",
                "hltb_extra",
                "hltb_main",
                "igdb_cached_at",
                "igdb_id",
                "igdb_platforms",
                "igdb_resolver_version",
                "release_date",
                "short_description",
                "sort_name",
                "tags",
            ],
        )
        merged = await read_game(target)
        self.assertEqual(merged["igdb_id"], 252001)
        self.assertEqual(merged["igdb_cached_at"], "2026-08-01T00:00:00+00:00")
        self.assertEqual(merged["igdb_resolver_version"], 4)
        # A lease on the source's enrichment says nothing about the target.
        self.assertIsNone(merged["igdb_claimed_at"])
        self.assertEqual(merged["cover_image_id"], "co7abc")
        self.assertEqual(json.loads(merged["tags"]), ["roguelike", "deckbuilder"])
        self.assertEqual(merged["hltb_main"], 9.5)
        self.assertEqual(merged["hltb_complete"], 22.5)
        self.assertEqual(merged["release_date"], "2024-05-16")
        self.assertEqual(merged["completion_status"], "playing")
        self.assertIsNone(await read_game(source))

    async def test_target_values_win_and_are_not_reported(self) -> None:
        source = await seed_enriched_source()
        target = await seed_game("Decktamer")
        await set_game_columns(
            target,
            igdb_id=999999,
            igdb_cached_at="2026-09-01T00:00:00+00:00",
            igdb_resolver_version=5,
            tags=json.dumps(["card game"]),
            hltb_main=3.0,
        )

        result = await admin.merge_games(source, target)

        for column in ("igdb_id", "igdb_cached_at", "igdb_resolver_version",
                       "tags", "hltb_main"):
            self.assertNotIn(column, result["game_fields_filled"])
        merged = await read_game(target)
        self.assertEqual(merged["igdb_id"], 999999)
        self.assertEqual(merged["igdb_cached_at"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(merged["igdb_resolver_version"], 5)
        self.assertEqual(json.loads(merged["tags"]), ["card game"])
        self.assertEqual(merged["hltb_main"], 3.0)
        # The columns the target really lacks still arrive.
        self.assertIn("cover_image_id", result["game_fields_filled"])
        self.assertEqual(merged["cover_image_id"], "co7abc")

    async def test_dry_run_reports_the_same_list_without_writing(self) -> None:
        source = await seed_enriched_source()
        target = await seed_game("Decktamer")

        preview = await admin.merge_games(source, target, dry_run=True)
        untouched = await read_game(target)

        self.assertIn("igdb_id", preview["game_fields_filled"])
        self.assertIn("cover_image_id", preview["game_fields_filled"])
        self.assertIsNone(untouched["igdb_id"])
        self.assertIsNone(untouched["cover_image_id"])
        self.assertIsNone(untouched["tags"])
        # Source untouched too — its igdb_id is only released in the wet run.
        self.assertEqual((await read_game(source))["igdb_id"], 252001)

        result = await admin.merge_games(source, target)
        self.assertEqual(
            result["game_fields_filled"], preview["game_fields_filled"]
        )

    async def test_target_manual_override_is_never_written(self) -> None:
        source = await seed_enriched_source()
        target = await seed_game("Decktamer")
        # The user cleared tags by hand: NULL, but pinned.
        await db_module.apply_manual_game_fields(target, {"tags": None})

        result = await admin.merge_games(source, target)

        self.assertNotIn("tags", result["game_fields_filled"])
        merged = await read_game(target)
        self.assertIsNone(merged["tags"])
        self.assertEqual(json.loads(merged["genres"]), ["Indie"])

    async def test_source_manual_override_travels_with_the_copied_value(self) -> None:
        """A value the SOURCE pinned by hand stays pinned on the target.

        Copying the value without its manual_overrides entry would leave the
        user's edit unprotected: the next sync or enrichment pass could
        overwrite it, which is exactly what the pin exists to prevent.
        """
        source = await seed_game("Decktamer (duplicate)")
        await db_module.apply_manual_game_fields(
            source, {"release_date": "2024-05-16", "completion_status": "playing"}
        )
        target = await seed_game("Decktamer")
        await db_module.apply_manual_game_fields(target, {"sort_name": "Decktamer"})

        result = await admin.merge_games(source, target)

        self.assertEqual(
            result["game_overrides_carried"], ["completion_status", "release_date"]
        )
        merged = await read_game(target)
        self.assertEqual(merged["release_date"], "2024-05-16")
        self.assertEqual(merged["completion_status"], "playing")
        async with db_module.get_db() as db:
            overrides = await db_module.get_manual_overrides(db, target)
        # The target's own pin survives beside the carried ones.
        self.assertEqual(overrides, {"completion_status", "release_date", "sort_name"})

    async def test_source_override_on_an_uncopied_column_is_not_carried(self) -> None:
        """Target wins on conflict, so the source's pin on that column dies
        with the source row — carrying it would pin the TARGET's value as if
        the user had set it."""
        source = await seed_game("Decktamer (duplicate)")
        await db_module.apply_manual_game_fields(source, {"release_date": "2024-05-16"})
        target = await seed_game("Decktamer")
        await set_game_columns(target, release_date="2024-05-17")

        preview = await admin.merge_games(source, target, dry_run=True)
        result = await admin.merge_games(source, target)

        self.assertEqual(preview["game_overrides_carried"], [])
        self.assertEqual(result["game_overrides_carried"], [])
        self.assertEqual((await read_game(target))["release_date"], "2024-05-17")
        async with db_module.get_db() as db:
            self.assertEqual(await db_module.get_manual_overrides(db, target), set())

    async def test_is_farmed_is_or_merged(self) -> None:
        source = await seed_game("Farmed duplicate", is_farmed=1)
        target = await seed_game("Farmed", is_farmed=0)

        result = await admin.merge_games(source, target)

        self.assertIn("is_farmed", result["game_fields_filled"])
        self.assertEqual((await read_game(target))["is_farmed"], 1)

    async def test_is_farmed_target_flag_survives_an_unfarmed_source(self) -> None:
        source = await seed_game("Clean duplicate", is_farmed=0)
        target = await seed_game("Clean", is_farmed=1)

        result = await admin.merge_games(source, target)

        self.assertEqual(result["game_fields_filled"], [])
        self.assertEqual((await read_game(target))["is_farmed"], 1)


if __name__ == "__main__":
    unittest.main()
