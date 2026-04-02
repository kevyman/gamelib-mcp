import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from steam_mcp.data import db as db_module
from steam_mcp.data import steam_store


class MigrationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "migration.sqlite"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_v1_to_v2_rebuilds_foreign_keys_against_new_games_table(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.executescript(db_module._V1_SCHEMA_DDL)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA user_version = 1")
        conn.execute(
            """INSERT INTO games
               (id, appid, igdb_id, name, steam_review_score, steam_review_desc,
                protondb_tier, store_cached_at)
               VALUES (1, 10, 100, 'Portal', 95, 'Overwhelmingly Positive',
                       'gold', '2024-01-01T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO game_platforms
               (id, game_id, platform, owned, last_synced)
               VALUES (1, 1, 'steam', 1, '2024-01-01T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO ratings
               (id, game_id, source, raw_score, normalized_score, review_text, synced_at)
               VALUES (1, 1, 'manual', 9.0, 90.0, 'great', '2024-01-01T00:00:00+00:00')"""
        )
        conn.commit()

        game_platform_rows = conn.execute(
            """SELECT id, game_id, platform, owned, playtime_minutes,
                      playtime_2weeks_minutes, last_synced
               FROM game_platforms"""
        ).fetchall()
        ratings_rows = conn.execute(
            """SELECT id, game_id, source, raw_score, normalized_score,
                      review_text, synced_at
               FROM ratings"""
        ).fetchall()

        conn.execute("ALTER TABLE games RENAME TO games_v1_old")
        conn.execute("ALTER TABLE game_platforms RENAME TO game_platforms_v1_old")
        conn.execute("ALTER TABLE ratings RENAME TO ratings_v1_old")
        conn.executescript(db_module._V2_SCHEMA_DDL)

        old_game_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(games_v1_old)").fetchall()
        }
        keep_cols = [
            "id",
            "igdb_id",
            "name",
            "sort_name",
            "release_date",
            "genres",
            "tags",
            "short_description",
            "metacritic_score",
            "hltb_main",
            "hltb_extra",
            "hltb_complete",
            "opencritic_score",
            "hltb_cached_at",
            "is_farmed",
        ]
        present = [col for col in keep_cols if col in old_game_columns]
        cols_sql = ", ".join(present)
        conn.execute(f"INSERT INTO games ({cols_sql}) SELECT {cols_sql} FROM games_v1_old")

        for row in game_platform_rows:
            conn.execute(
                """INSERT INTO game_platforms
                   (id, game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                tuple(row),
            )

        missing_steam_rows = conn.execute(
            """SELECT g.id AS game_id
               FROM games_v1_old g
               LEFT JOIN game_platforms gp
                 ON gp.game_id = g.id AND gp.platform = ?
               WHERE g.appid IS NOT NULL AND gp.id IS NULL""",
            (db_module.STEAM_PLATFORM,),
        ).fetchall()
        for row in missing_steam_rows:
            conn.execute(
                """INSERT INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   VALUES (?, ?, 1, NULL, NULL, '2024-01-02T00:00:00+00:00')""",
                (row["game_id"], db_module.STEAM_PLATFORM),
            )

        steam_rows = conn.execute(
            """SELECT gp.id AS game_platform_id,
                      g.appid,
                      g.steam_review_score,
                      g.steam_review_desc,
                      g.protondb_tier,
                      g.store_cached_at,
                      g.protondb_cached_at,
                      g.steamspy_cached_at,
                      g.rtime_last_played,
                      g.library_updated_at
               FROM games_v1_old g
               JOIN game_platforms gp
                 ON gp.game_id = g.id AND gp.platform = ?""",
            (db_module.STEAM_PLATFORM,),
        ).fetchall()
        for row in steam_rows:
            conn.execute(
                """INSERT INTO game_platform_identifiers
                   (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
                   VALUES (?, ?, ?, 1, '2024-01-02T00:00:00+00:00')""",
                (
                    row["game_platform_id"],
                    db_module.STEAM_APP_ID,
                    str(row["appid"]),
                ),
            )
            conn.execute(
                """INSERT INTO steam_platform_data
                   (game_platform_id, steam_review_score, steam_review_desc, protondb_tier,
                    store_cached_at, protondb_cached_at, steamspy_cached_at,
                    rtime_last_played, library_updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["game_platform_id"],
                    row["steam_review_score"],
                    row["steam_review_desc"],
                    row["protondb_tier"],
                    row["store_cached_at"],
                    row["protondb_cached_at"],
                    row["steamspy_cached_at"],
                    row["rtime_last_played"],
                    row["library_updated_at"],
                ),
            )

        for row in ratings_rows:
            conn.execute(
                """INSERT INTO ratings
                   (id, game_id, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                tuple(row),
            )
        conn.execute("DROP TABLE IF EXISTS games_v1_old")
        conn.execute("DROP TABLE IF EXISTS game_platforms_v1_old")
        conn.execute("DROP TABLE IF EXISTS ratings_v1_old")
        conn.commit()

        game_platform_fks = conn.execute("PRAGMA foreign_key_list(game_platforms)").fetchall()
        ratings_fks = conn.execute("PRAGMA foreign_key_list(ratings)").fetchall()
        self.assertEqual(game_platform_fks[0]["table"], "games")
        self.assertEqual(ratings_fks[0]["table"], "games")

        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO games (id, name, is_farmed) VALUES (2, 'Half-Life', 0)")
        conn.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, last_synced)
               VALUES (2, 'steam', 1, '2024-01-02T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO ratings
               (game_id, source, raw_score, normalized_score, review_text, synced_at)
               VALUES (2, 'critic', 8.5, 85.0, 'classic', '2024-01-02T00:00:00+00:00')"""
        )

        identifier = conn.execute(
            """SELECT identifier_type, identifier_value
               FROM game_platform_identifiers
               WHERE game_platform_id = 1"""
        ).fetchone()
        steam_data = conn.execute(
            """SELECT steam_review_score, steam_review_desc, protondb_tier
               FROM steam_platform_data
               WHERE game_platform_id = 1"""
        ).fetchone()
        conn.close()

        self.assertEqual(identifier["identifier_type"], db_module.STEAM_APP_ID)
        self.assertEqual(identifier["identifier_value"], "10")
        self.assertEqual(steam_data["steam_review_score"], 95)
        self.assertEqual(steam_data["steam_review_desc"], "Overwhelmingly Positive")
        self.assertEqual(steam_data["protondb_tier"], "gold")


class SteamStoreRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_enrich_game_preserves_review_fields_when_review_fetch_fails(self) -> None:
        row = {
            "game_id": 1,
            "game_platform_id": 2,
            "store_cached_at": None,
        }

        class _DummyDb:
            async def execute(self, *_args, **_kwargs):
                return None

            async def commit(self):
                return None

        class _DummyContext:
            async def __aenter__(self):
                return _DummyDb()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        upsert = AsyncMock()
        with (
            patch.object(
                steam_store,
                "get_steam_platform_row_by_appid",
                AsyncMock(side_effect=[row, row]),
            ),
            patch.object(steam_store, "_fetch_all", AsyncMock(return_value=(None, {}))),
            patch.object(steam_store, "upsert_steam_platform_data", upsert),
            patch.object(steam_store, "get_db", return_value=_DummyContext()),
        ):
            refreshed = await steam_store.enrich_game(10)

        self.assertEqual(refreshed, row)
        _, kwargs = upsert.await_args
        self.assertEqual(kwargs.keys(), {"store_cached_at"})


if __name__ == "__main__":
    unittest.main()
