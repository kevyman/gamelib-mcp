"""Tests for Nintendo title id normalization: ingest-time uppercasing
(normalize_identifier_value) and the v32->v33 data migration that backfills
existing mixed-case rows in game_platform_identifiers/nintendo_play_summary.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data import db as db_module


class IngestNormalizationTests(ToolDBTestCase):
    """Every write/lookup chokepoint normalizes nintendo_title_id to uppercase."""

    async def test_upsert_game_platform_identifier_uppercases_nintendo_title_id(self):
        game_id = await seed_game("Mario Kart World")
        pid = await add_platform(game_id, "switch2", owned=1)

        await db_module.upsert_game_platform_identifier(pid, "nintendo_title_id", "0100abcdef000000")

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT identifier_value FROM game_platform_identifiers WHERE game_platform_id = ?",
                (pid,),
            )
        self.assertEqual(row["identifier_value"], "0100ABCDEF000000")

    async def test_upsert_game_platform_identifier_leaves_other_types_unchanged(self):
        game_id = await seed_game("Half-Life 2")
        pid = await add_platform(game_id, "steam", owned=1)

        # Not a real Steam appid format, but proves steam_appid is passed
        # through verbatim (unlike nintendo_title_id) — normalize_identifier_value
        # only special-cases nintendo_title_id.
        await db_module.upsert_game_platform_identifier(pid, "steam_appid", "MixedCase123")

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT identifier_value FROM game_platform_identifiers WHERE game_platform_id = ?",
                (pid,),
            )
        self.assertEqual(row["identifier_value"], "MixedCase123")

    async def test_upsert_nintendo_play_summary_uppercases_application_id(self):
        await db_module.upsert_nintendo_play_summary(
            [
                {
                    "device_id": "device-1",
                    "application_id": "0100abcdef000000",
                    "period_type": "day",
                    "period_key": "2026-07-01",
                    "playtime_minutes": 30,
                    "app_name": "Mario Kart World",
                }
            ]
        )

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT application_id FROM nintendo_play_summary LIMIT 1"
            )
        self.assertEqual(row["application_id"], "0100ABCDEF000000")

    async def test_get_game_by_identifier_finds_row_queried_with_lowercase(self):
        game_id = await seed_game("Mario Kart World")
        pid = await add_platform(game_id, "switch2", owned=1)
        await db_module.upsert_game_platform_identifier(pid, "nintendo_title_id", "0100ABCDEF000000")

        found = await db_module.get_game_by_identifier("nintendo_title_id", "0100abcdef000000")

        self.assertIsNotNone(found)
        self.assertEqual(found["id"], game_id)

    async def test_upsert_and_lookup_round_trip_regardless_of_input_case(self):
        # The two write chokepoints and the lookup chokepoint all normalize
        # independently, so any mix of input casing across them still bridges.
        game_id = await seed_game("Case Round Trip")
        pid = await add_platform(game_id, "switch2", owned=1)
        await db_module.upsert_game_platform_identifier(pid, "nintendo_title_id", "abc123def4567890")
        await db_module.upsert_nintendo_play_summary(
            [
                {
                    "device_id": "device-1",
                    "application_id": "ABC123DEF4567890",
                    "period_type": "day",
                    "period_key": "2026-07-01",
                    "playtime_minutes": 10,
                    "app_name": None,
                }
            ]
        )

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT playtime_minutes FROM v_game_playtime WHERE game_id = ? AND platform = 'switch2'",
                (game_id,),
            )
        self.assertEqual(row["playtime_minutes"], 10)


class NintendoIdMigrationTests(unittest.IsolatedAsyncioTestCase):
    """v32->v33 data fix: dedupe case-only rows, then uppercase everything."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "migration.sqlite"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _seed_v32_mixed_case_fixture(self) -> None:
        """A v32 DB with case-only duplicates in BOTH affected tables."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V32_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 32")

        conn.execute(
            "INSERT INTO games (id, name, content_type, is_primary_library_item) "
            "VALUES (1, 'Mario Kart World', 'base_game', 1)"
        )
        conn.execute(
            "INSERT INTO game_platforms (id, game_id, platform, owned, playtime_minutes) "
            "VALUES (10, 1, 'switch2', 1, 999)"
        )

        # Case-only duplicate nintendo_title_id identifiers on the same
        # game_platform_id, at different last_seen_at — the older (lowercase)
        # one must be discarded, the newer (already-uppercase) one kept.
        conn.execute(
            "INSERT INTO game_platform_identifiers "
            "(game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) "
            "VALUES (10, 'nintendo_title_id', '0100aaa0000aa000', 1, '2026-01-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO game_platform_identifiers "
            "(game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) "
            "VALUES (10, 'nintendo_title_id', '0100AAA0000AA000', 1, '2026-06-01T00:00:00+00:00')"
        )
        # Control row: a different identifier_type must never be touched.
        conn.execute(
            "INSERT INTO game_platform_identifiers "
            "(game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) "
            "VALUES (10, 'steam_appid', 'MixedCase123', 1, '2026-01-01T00:00:00+00:00')"
        )

        # Case-only duplicate nintendo_play_summary rows for the same
        # (device, day) — must MERGE (sum minutes, coalesce app_name), not
        # pick a winner.
        conn.execute(
            "INSERT INTO nintendo_play_summary "
            "(device_id, application_id, period_type, period_key, playtime_minutes, app_name, updated_at) "
            "VALUES ('dev1', '0100aaa0000aa000', 'day', '2026-07-01', 30, NULL, '2026-07-01T00:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO nintendo_play_summary "
            "(device_id, application_id, period_type, period_key, playtime_minutes, app_name, updated_at) "
            "VALUES ('dev1', '0100AAA0000AA000', 'day', '2026-07-01', 15, 'Mario Kart World', '2026-07-02T00:00:00+00:00')"
        )
        # Distinct row (different device): must survive untouched (case-normalized only).
        conn.execute(
            "INSERT INTO nintendo_play_summary "
            "(device_id, application_id, period_type, period_key, playtime_minutes, app_name, updated_at) "
            "VALUES ('dev2', '0100aaa0000aa000', 'day', '2026-07-02', 5, NULL, '2026-07-02T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

    async def test_migration_dedupes_and_uppercases_both_tables(self) -> None:
        self._seed_v32_mixed_case_fixture()

        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            db_module._DB_READY_PATH = None
            result = await db_module.migrate_db()

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            gpi_rows = conn.execute(
                "SELECT identifier_type, identifier_value FROM game_platform_identifiers "
                "WHERE identifier_type = 'nintendo_title_id'"
            ).fetchall()
            steam_rows = conn.execute(
                "SELECT identifier_value FROM game_platform_identifiers WHERE identifier_type = 'steam_appid'"
            ).fetchall()
            nps_rows = conn.execute(
                "SELECT device_id, application_id, playtime_minutes, app_name "
                "FROM nintendo_play_summary ORDER BY device_id"
            ).fetchall()
        finally:
            conn.close()

        # Exactly one nintendo_title_id survivor, uppercase, no UNIQUE violation.
        self.assertEqual(len(gpi_rows), 1)
        self.assertEqual(gpi_rows[0]["identifier_value"], "0100AAA0000AA000")

        # The unrelated identifier_type is untouched (still mixed case).
        self.assertEqual(len(steam_rows), 1)
        self.assertEqual(steam_rows[0]["identifier_value"], "MixedCase123")

        # nintendo_play_summary: the dev1 case-only pair merged (30 + 15 = 45,
        # app_name coalesced from the non-null side); dev2's distinct row
        # survives with its playtime unchanged, case-normalized.
        by_device = {row["device_id"]: row for row in nps_rows}
        self.assertEqual(len(nps_rows), 2)
        self.assertEqual(by_device["dev1"]["application_id"], "0100AAA0000AA000")
        self.assertEqual(by_device["dev1"]["playtime_minutes"], 45)
        self.assertEqual(by_device["dev1"]["app_name"], "Mario Kart World")
        self.assertEqual(by_device["dev2"]["application_id"], "0100AAA0000AA000")
        self.assertEqual(by_device["dev2"]["playtime_minutes"], 5)

        # Total playtime is preserved across the merge (30 + 15 + 5 = 50).
        total = sum(row["playtime_minutes"] for row in nps_rows)
        self.assertEqual(total, 50)

    async def test_normalize_helper_is_idempotent(self) -> None:
        self._seed_v32_mixed_case_fixture()

        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            db_module._DB_READY_PATH = None
            await db_module.migrate_db()

            async with db_module.get_db() as db:
                await db_module._normalize_nintendo_title_ids(db)

                gpi_rows = await db.execute_fetchall(
                    "SELECT identifier_value FROM game_platform_identifiers "
                    "WHERE identifier_type = 'nintendo_title_id'"
                )
                nps_rows = await db.execute_fetchall(
                    "SELECT device_id, application_id, playtime_minutes FROM nintendo_play_summary "
                    "ORDER BY device_id"
                )

        self.assertEqual([r["identifier_value"] for r in gpi_rows], ["0100AAA0000AA000"])
        self.assertEqual(
            [(r["device_id"], r["application_id"], r["playtime_minutes"]) for r in nps_rows],
            [("dev1", "0100AAA0000AA000", 45), ("dev2", "0100AAA0000AA000", 5)],
        )


if __name__ == "__main__":
    unittest.main()
