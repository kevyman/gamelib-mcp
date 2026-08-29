import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiosqlite
from conftest import DEADLOCK_TIMEOUT, add_platform, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_store


class MigrationRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "migration.sqlite"

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_init_db_creates_missing_parent_directory(self) -> None:
        nested_db_path = Path(self.tmpdir.name) / "missing" / "nested" / "gamelib.db"

        with patch.dict("os.environ", {"DATABASE_URL": f"file:{nested_db_path}"}, clear=False):
            await db_module.init_db()

        self.assertTrue(nested_db_path.exists())

    async def test_pre_migration_snapshot_preserves_old_database(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V1_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("INSERT INTO games (name) VALUES ('Snapshot Me')")
        conn.commit()
        conn.close()

        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            result = await db_module.migrate_db()

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        backup_path = Path(f"{self.db_path}.pre-v1.bak")
        self.assertTrue(backup_path.exists())
        backup = sqlite3.connect(backup_path)
        try:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            names = [row[0] for row in backup.execute("SELECT name FROM games")]
        finally:
            backup.close()
        self.assertEqual(names, ["Snapshot Me"])

    async def test_no_snapshot_for_fresh_or_current_database(self) -> None:
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.migrate_db()  # fresh install straight to current schema
            db_module._DB_READY_PATH = None
            await db_module.migrate_db()  # already at the current schema

        self.assertEqual(list(Path(self.tmpdir.name).glob("*.bak")), [])

    async def test_fresh_database_includes_v11_content_relationship_schema(self) -> None:
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            db_module._DB_READY_PATH = None
            await db_module.init_db()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        game_cols = {row["name"] for row in conn.execute("PRAGMA table_info(games)").fetchall()}
        alias_cols = {
            row["name"] for row in conn.execute("PRAGMA table_info(game_aliases)").fetchall()
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIn("content_type", game_cols)
        self.assertIn("parent_game_id", game_cols)
        self.assertIn("is_primary_library_item", game_cols)
        self.assertEqual(
            alias_cols,
            {
                "id",
                "game_id",
                "alias",
                "alias_normalized",
                "alias_type",
                "source",
                "source_key",
            },
        )

    async def test_v10_to_v11_preserves_existing_games_as_primary_base_games(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V10_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 10")
        conn.execute("INSERT INTO games (id, name, name_normalized) VALUES (1, 'Portal', 'portal')")
        conn.commit()
        conn.close()

        async with aiosqlite.connect(self.db_path) as db:
            await db_module._configure_connection(db, enable_wal=True)
            await db_module._run_migrations(db)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT content_type, parent_game_id, is_primary_library_item FROM games WHERE id = 1"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(row["content_type"], "base_game")
        self.assertIsNone(row["parent_game_id"])
        self.assertEqual(row["is_primary_library_item"], 1)

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

    async def test_v2_to_v3_rebuilds_foreign_keys_against_new_games_table(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V2_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 2")
        conn.execute("INSERT INTO games (id, name, is_farmed) VALUES (1, 'Portal', 0)")
        conn.execute(
            """INSERT INTO game_platforms
               (id, game_id, platform, owned, last_synced)
               VALUES (1, 1, 'steam', 1, '2024-01-01T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()

        async with aiosqlite.connect(self.db_path) as db:
            await db_module._configure_connection(db, enable_wal=True)
            await db_module._run_migrations(db)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        game_platform_fks = conn.execute("PRAGMA foreign_key_list(game_platforms)").fetchall()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO games (id, name, is_farmed) VALUES (2, 'Half-Life', 0)")
        conn.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, last_synced)
               VALUES (2, 'steam', 1, '2024-01-02T00:00:00+00:00')"""
        )
        conn.close()

        self.assertEqual(game_platform_fks[0]["table"], "games")

    async def test_current_schema_repairs_game_foreign_keys_left_by_old_migration(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V6_SCHEMA_DDL)
        conn.execute("INSERT INTO games (id, name, is_farmed) VALUES (1, 'Portal', 0)")
        conn.execute(
            """INSERT INTO game_platforms
               (id, game_id, platform, owned, last_synced)
               VALUES (1, 1, 'steam', 1, '2024-01-01T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO ratings
               (id, game_id, source, raw_score, normalized_score, synced_at)
               VALUES (1, 1, 'manual', 9.0, 90.0, '2024-01-01T00:00:00+00:00')"""
        )
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("ALTER TABLE games RENAME TO games_v2_old")
        conn.execute(
            """CREATE TABLE games (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                igdb_id          INTEGER UNIQUE,
                name             TEXT NOT NULL,
                sort_name        TEXT,
                release_date     TEXT,
                genres           TEXT,
                tags             TEXT,
                short_description TEXT,
                hltb_main        REAL,
                hltb_extra       REAL,
                hltb_complete    REAL,
                hltb_cached_at   TEXT,
                hltb_claimed_at  TEXT,
                igdb_cached_at   TEXT,
                igdb_claimed_at  TEXT,
                is_farmed        INTEGER NOT NULL DEFAULT 0
            )"""
        )
        conn.execute("INSERT INTO games SELECT * FROM games_v2_old")
        conn.execute("DROP TABLE games_v2_old")
        conn.execute("PRAGMA user_version = 6")
        conn.commit()
        conn.close()

        async with aiosqlite.connect(self.db_path) as db:
            await db_module._configure_connection(db, enable_wal=True)
            await db_module._run_migrations(db)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        game_platform_fks = conn.execute("PRAGMA foreign_key_list(game_platforms)").fetchall()
        ratings_fks = conn.execute("PRAGMA foreign_key_list(ratings)").fetchall()
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("INSERT INTO games (id, name, is_farmed) VALUES (2, 'Half-Life', 0)")
        conn.execute(
            """INSERT INTO game_platforms
               (game_id, platform, owned, last_synced)
               VALUES (2, 'steam', 1, '2024-01-02T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO ratings
               (game_id, source, raw_score, normalized_score, synced_at)
               VALUES (2, 'manual', 8.0, 80.0, '2024-01-02T00:00:00+00:00')"""
        )
        conn.close()

        self.assertEqual(game_platform_fks[0]["table"], "games")
        self.assertEqual(ratings_fks[0]["table"], "games")

    async def test_schema_contains_claim_columns(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            await db_module.init_db()
            async with db_module.get_db() as conn:
                games_cols = await conn.execute_fetchall("PRAGMA table_info(games)")
                spd_cols = await conn.execute_fetchall("PRAGMA table_info(steam_platform_data)")
                gpe_cols = await conn.execute_fetchall("PRAGMA table_info(game_platform_enrichment)")

        self.assertIn("igdb_claimed_at", {row["name"] for row in games_cols})
        self.assertIn("hltb_claimed_at", {row["name"] for row in games_cols})
        self.assertIn("store_claimed_at", {row["name"] for row in spd_cols})
        self.assertIn("protondb_claimed_at", {row["name"] for row in spd_cols})
        self.assertIn("steamspy_claimed_at", {row["name"] for row in spd_cols})
        self.assertIn("opencritic_claimed_at", {row["name"] for row in gpe_cols})
        self.assertIn("metacritic_claimed_at", {row["name"] for row in gpe_cols})

    async def test_schema_contains_opencritic_scrape_columns(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            await db_module.init_db()
            async with db_module.get_db() as conn:
                gpe_cols = await conn.execute_fetchall("PRAGMA table_info(game_platform_enrichment)")

        names = {row["name"] for row in gpe_cols}
        self.assertIn("opencritic_url", names)
        self.assertIn("opencritic_num_reviews", names)

    async def test_platform_dict_exposes_opencritic_scrape_fields(self) -> None:
        platform = db_module._platform_dict(
            {
                "game_platform_id": 1,
                "platform": "steam",
                "owned": 1,
                "playtime_minutes": 120,
                "playtime_2weeks_minutes": 0,
                "last_played": None,
                "last_synced": "2026-04-07T00:00:00+00:00",
                "acquired_at": None,
                "price_paid": None,
                "price_currency": None,
                "purchase_source": None,
                "bundle_name": None,
                "platform_release_date": "2024-02-01",
                "metacritic_score": 88,
                "metacritic_url": "https://www.metacritic.com/game/pc/portal-2/",
                "opencritic_score": 90,
                "opencritic_tier": "Mighty",
                "opencritic_percent_rec": 96.0,
                "opencritic_url": "https://opencritic.com/game/120/portal-2",
                "opencritic_num_reviews": 135,
                "steam_review_score": None,
                "steam_review_desc": None,
                "protondb_tier": None,
                "rtime_last_played": None,
                "library_updated_at": None,
            }
        )

        self.assertEqual(platform["opencritic_url"], "https://opencritic.com/game/120/portal-2")
        self.assertEqual(platform["opencritic_num_reviews"], 135)

    async def test_load_platforms_for_games_includes_opencritic_scrape_fields(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            await db_module.init_db()
            async with db_module.get_db() as conn:
                await conn.execute(
                    "INSERT INTO games (id, name, is_farmed) VALUES (1, 'Portal 2', 0)"
                )
                await conn.execute(
                    """INSERT INTO game_platforms
                       (id, game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                       VALUES (1, 1, 'steam', 1, 120, 0, '2026-04-07T00:00:00+00:00')"""
                )
                await conn.execute(
                    """INSERT INTO game_platform_enrichment
                       (game_platform_id, platform_release_date, metacritic_score, metacritic_url,
                        opencritic_id, opencritic_url, opencritic_score, opencritic_tier,
                        opencritic_percent_rec, opencritic_num_reviews, opencritic_cached_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        1,
                        "2024-02-01",
                        88,
                        "https://www.metacritic.com/game/pc/portal-2/",
                        120,
                        "https://opencritic.com/game/120/portal-2",
                        90,
                        "Mighty",
                        96.0,
                        135,
                        "2026-04-07T00:00:00+00:00",
                    ),
                )
                await conn.commit()

            platforms = await db_module.load_platforms_for_games([1])

        self.assertEqual(
            platforms[1][0]["opencritic_url"],
            "https://opencritic.com/game/120/portal-2",
        )
        self.assertEqual(platforms[1][0]["opencritic_num_reviews"], 135)

    async def test_fresh_db_initializes_with_latest_columns(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db_module._configure_connection(db, enable_wal=True)
            result = await db_module._run_migrations(db)

            version = await db_module._get_user_version(db)
            cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_platform_enrichment)")}
            game_cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")}
            gp_cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_platforms)")}
            tables = await db_module._table_names(db)

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        self.assertIn("opencritic_url", cols)
        self.assertIn("opencritic_num_reviews", cols)
        self.assertIn("name_normalized", game_cols)
        self.assertIn("game_wishlist", tables)
        self.assertIn("manual_overrides", gp_cols)

    async def test_v30_to_v31_adds_game_platforms_manual_overrides(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db_module._configure_connection(db, enable_wal=True)
            # Minimal pre-v31 game_platforms table (no manual_overrides column).
            await db.execute(
                """CREATE TABLE game_platforms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    owned INTEGER NOT NULL DEFAULT 1,
                    playtime_minutes INTEGER,
                    UNIQUE(game_id, platform)
                )"""
            )
            await db_module._set_user_version(db, 30)
            await db.commit()

            before = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_platforms)")}
            self.assertNotIn("manual_overrides", before)

            await db_module._migrate_v30_to_v31(db, None)

            after = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_platforms)")}
            version = await db_module._get_user_version(db)

        self.assertIn("manual_overrides", after)
        self.assertEqual(version, 31)

    async def test_v33_to_v34_adds_ownership_lifecycle_columns(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db_module._configure_connection(db, enable_wal=True)
            # Minimal pre-v34 game_platforms table (neither v34 column).
            await db.execute(
                """CREATE TABLE game_platforms (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    game_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    owned INTEGER NOT NULL DEFAULT 1,
                    playtime_minutes INTEGER,
                    last_synced TEXT,
                    UNIQUE(game_id, platform)
                )"""
            )
            await db.execute(
                "INSERT INTO game_platforms (game_id, platform, owned, last_synced)"
                " VALUES (1, 'steam', 1, '2026-07-01T00:00:00+00:00')"
            )
            await db_module._set_user_version(db, 33)
            await db.commit()

            await db_module._migrate_v33_to_v34(db, None)

            after = {
                row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_platforms)")
            }
            version = await db_module._get_user_version(db)
            row = await db.execute_fetchone(
                "SELECT unowned_at, last_seen_in_source FROM game_platforms WHERE game_id = 1"
            )

        self.assertIn("unowned_at", after)
        self.assertIn("last_seen_in_source", after)
        self.assertEqual(version, 34)
        # No backfill on either column: an existing row has never been marked
        # unowned, and claiming a source "saw" it would be evidence we invented.
        self.assertIsNone(row["unowned_at"])
        self.assertIsNone(row["last_seen_in_source"])

    async def test_v4_database_migrates_opencritic_scrape_columns(self) -> None:
        conn = sqlite3.connect(self.db_path)
        old_v4_schema = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        short_description TEXT,
        hltb_main        REAL,
        hltb_extra       REAL,
        hltb_complete    REAL,
        hltb_cached_at   TEXT,
        hltb_claimed_at  TEXT,
        igdb_cached_at   TEXT,
        igdb_claimed_at  TEXT,
        is_farmed        INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS game_platforms (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id          INTEGER NOT NULL REFERENCES games(id),
        platform         TEXT NOT NULL,
        owned            INTEGER NOT NULL DEFAULT 1,
        playtime_minutes INTEGER,
        playtime_2weeks_minutes INTEGER,
        last_synced      TEXT,
        UNIQUE(game_id, platform)
    );

    CREATE TABLE IF NOT EXISTS game_platform_identifiers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_platform_id INTEGER NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
        identifier_type  TEXT NOT NULL,
        identifier_value TEXT NOT NULL,
        is_primary       INTEGER NOT NULL DEFAULT 1,
        last_seen_at     TEXT,
        UNIQUE(identifier_type, identifier_value)
    );

    CREATE TABLE IF NOT EXISTS steam_platform_data (
        game_platform_id    INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        steam_review_score  INTEGER,
        steam_review_desc   TEXT,
        protondb_tier       TEXT,
        store_cached_at     TEXT,
        store_claimed_at    TEXT,
        protondb_cached_at  TEXT,
        protondb_claimed_at TEXT,
        steamspy_cached_at  TEXT,
        steamspy_claimed_at TEXT,
        rtime_last_played   INTEGER,
        library_updated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS game_platform_enrichment (
        game_platform_id       INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        platform_release_date  TEXT,
        metacritic_score       INTEGER,
        metacritic_url         TEXT,
        metacritic_claimed_at  TEXT,
        opencritic_id          INTEGER,
        opencritic_score       INTEGER,
        opencritic_tier        TEXT,
        opencritic_percent_rec REAL,
        opencritic_cached_at   TEXT,
        opencritic_claimed_at  TEXT,
        metacritic_cached_at   TEXT
    );

    CREATE TABLE IF NOT EXISTS ratings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id INTEGER REFERENCES games(id),
        source TEXT NOT NULL,
        raw_score REAL,
        normalized_score REAL,
        review_text TEXT,
        synced_at TEXT NOT NULL,
        UNIQUE(game_id, source)
    );

    CREATE TABLE IF NOT EXISTS tag_affinity (
        tag TEXT PRIMARY KEY,
        affinity_score REAL,
        avg_score REAL,
        game_count INTEGER,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_game_platforms_game_id ON game_platforms(game_id);
    CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_platform_id
        ON game_platform_identifiers(game_platform_id);
    CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
        ON game_platform_identifiers(identifier_type, identifier_value);
"""
        conn.executescript(old_v4_schema)
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict(
            "os.environ",
            {"DATABASE_URL": f"file:{self.db_path}"},
            clear=False,
        ):
            await db_module.init_db()
            async with db_module.get_db() as migrated:
                cols = await migrated.execute_fetchall("PRAGMA table_info(game_platform_enrichment)")

        names = {row["name"] for row in cols}
        self.assertIn("opencritic_url", names)
        self.assertIn("opencritic_num_reviews", names)

    async def test_v5_to_v6_cleans_metacritic_and_hltb_contamination(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V5_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 5")
        # game 1: HLTB main is a real "no data" zero; complete is real.
        # game 2: clean data that must be left untouched.
        conn.execute(
            "INSERT INTO games (id, name, hltb_main, hltb_extra, hltb_complete) "
            "VALUES (1, 'ZeroHLTB', 0, 0, 12.5), (2, 'GoodHLTB', 10.0, 15.0, 20.0)"
        )
        conn.execute("INSERT INTO game_platforms (id, game_id, platform) VALUES (1, 1, 'steam'), (2, 2, 'steam')")
        # gpe 1: contaminated user score (8); gpe 2: legit low Metascore (36) kept.
        conn.execute(
            "INSERT INTO game_platform_enrichment "
            "(game_platform_id, metacritic_score, metacritic_url, metacritic_cached_at, metacritic_claimed_at) "
            "VALUES (1, 8, 'http://mc/x', '2026-01-01', '2026-01-01'), (2, 36, 'http://mc/y', '2026-01-01', NULL)"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                games = {
                    row["name"]: row
                    for row in await db.execute_fetchall(
                        "SELECT name, hltb_main, hltb_extra, hltb_complete FROM games"
                    )
                }
                gpe = {
                    row["game_platform_id"]: row
                    for row in await db.execute_fetchall(
                        "SELECT game_platform_id, metacritic_score, metacritic_cached_at, "
                        "metacritic_claimed_at FROM game_platform_enrichment"
                    )
                }

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        # Contaminated user score nulled, with cache/claim cleared for re-scrape.
        self.assertIsNone(gpe[1]["metacritic_score"])
        self.assertIsNone(gpe[1]["metacritic_cached_at"])
        self.assertIsNone(gpe[1]["metacritic_claimed_at"])
        # Legit low Metascore preserved.
        self.assertEqual(gpe[2]["metacritic_score"], 36)
        self.assertEqual(gpe[2]["metacritic_cached_at"], "2026-01-01")
        # HLTB zeros nulled in place; non-zero values untouched.
        self.assertIsNone(games["ZeroHLTB"]["hltb_main"])
        self.assertIsNone(games["ZeroHLTB"]["hltb_extra"])
        self.assertEqual(games["ZeroHLTB"]["hltb_complete"], 12.5)
        self.assertEqual(games["GoodHLTB"]["hltb_main"], 10.0)

    async def test_v6_to_v7_backfills_name_normalized(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V6_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 6")
        conn.execute(
            "INSERT INTO games (id, name) VALUES "
            "(1, 'Sekiro™: Shadows Die Twice'), (2, \"Don't Starve\")"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                rows = await db.execute_fetchall(
                    "SELECT id, name_normalized FROM games ORDER BY id"
                )
                indexes = {
                    row[1]
                    for row in await db.execute_fetchall("PRAGMA index_list(games)")
                }

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(rows[0]["name_normalized"], "sekiro shadows die twice")
        self.assertEqual(rows[1]["name_normalized"], "don t starve")
        self.assertIn("idx_games_name_normalized", indexes)

    async def test_v7_to_v8_quarantines_feature_flags_from_tags(self) -> None:
        import json

        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V7_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 7")
        # Game 1 has real tags mixed with feature flags; game 2 has ONLY flags
        # (its tags must empty out and its steam caches must clear for re-fetch);
        # game 3 is clean and must be untouched.
        conn.execute(
            "INSERT INTO games (id, name, tags) VALUES "
            "(1, 'Hades', ?), (2, 'Sekiro', ?), (3, 'Celeste', ?)",
            (
                json.dumps(["Roguelike", "Steam Trading Cards", "Action"]),
                json.dumps(["Steam Achievements", "Family Sharing", "Steam Cloud"]),
                json.dumps(["Platformer"]),
            ),
        )
        conn.execute(
            "INSERT INTO game_platforms (id, game_id, platform) VALUES "
            "(1, 1, 'steam'), (2, 2, 'steam'), (3, 3, 'steam')"
        )
        conn.execute(
            "INSERT INTO steam_platform_data (game_platform_id, store_cached_at, steamspy_cached_at) "
            "VALUES (1, '2026-01-01', '2026-01-01'), (2, '2026-01-01', '2026-01-01'), "
            "(3, '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at) "
            "VALUES ('steam achievements', 6.5, 9.5, 1, '2026-01-01'), "
            "('roguelike', 8.0, 9.0, 2, '2026-01-01')"
        )
        # Rate Hades so the v13 post-migration affinity recompute keeps its tags.
        conn.execute(
            "INSERT INTO ratings (game_id, source, raw_score, normalized_score, synced_at) "
            "VALUES (1, 'backloggd', 5.0, 10.0, '2026-01-01')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                games = {
                    row["id"]: row
                    for row in await db.execute_fetchall(
                        "SELECT id, tags, features FROM games"
                    )
                }
                spd = {
                    row["game_platform_id"]: row
                    for row in await db.execute_fetchall(
                        "SELECT game_platform_id, store_cached_at, steamspy_cached_at "
                        "FROM steam_platform_data"
                    )
                }
                affinity_tags = {
                    row["tag"]
                    for row in await db.execute_fetchall("SELECT tag FROM tag_affinity")
                }

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        # v12->v13 canonicalizes (lowercases) surviving tags in place.
        self.assertEqual(json.loads(games[1]["tags"]), ["roguelike", "action"])
        self.assertEqual(json.loads(games[1]["features"]), ["Steam Trading Cards"])
        # Game 2 emptied out -> caches cleared so enrichment re-fetches tags.
        self.assertEqual(json.loads(games[2]["tags"]), [])
        self.assertIsNone(spd[2]["store_cached_at"])
        self.assertIsNone(spd[2]["steamspy_cached_at"])
        # store_cached_at survives (v13 only resets SteamSpy/IGDB caches, not store).
        self.assertEqual(spd[1]["store_cached_at"], "2026-01-01")
        self.assertEqual(json.loads(games[3]["tags"]), ["platformer"])
        self.assertIsNone(games[3]["features"])
        # Affinity is rebuilt from ratings by the v13 post-migration recompute;
        # feature flags stay out, Hades' real (canonicalized) tags survive.
        self.assertEqual(affinity_tags, {"roguelike", "action"})

    async def test_v8_to_v9_adds_manual_overrides_column(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V8_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 8")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                cols = await db_module._table_columns(db, "games")
                overrides = await db_module.get_manual_overrides(db, 1)

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIn("manual_overrides", cols)
        self.assertEqual(overrides, set())

    async def test_v12_to_v13_canonicalizes_tags_and_reclaims_steam(self) -> None:
        import json

        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V12_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 12")
        # 1: steam game, mixed-case + synonym tags -> canonicalized
        # 2: non-steam game, IGDB cache must be LEFT ALONE
        # 3: steam game with manual tags override -> still canonicalized in place
        conn.execute(
            "INSERT INTO games (id, name, tags, igdb_cached_at, igdb_claimed_at, manual_overrides) VALUES "
            "(1, 'Sekiro', ?, '2026-01-01', NULL, NULL), "
            "(2, 'Metroid Dread', ?, '2026-01-01', NULL, NULL), "
            "(3, 'Pinned', ?, '2026-01-01', NULL, ?)",
            (
                json.dumps(["Souls-like", "Difficult", "souls-like"]),
                json.dumps(["metroidvania"]),
                json.dumps(["Soulslike"]),
                json.dumps(["tags"]),
            ),
        )
        conn.execute(
            "INSERT INTO game_platforms (id, game_id, platform) VALUES "
            "(1, 1, 'steam'), (2, 2, 'switch2'), (3, 3, 'steam')"
        )
        conn.execute(
            "INSERT INTO game_platform_identifiers (game_platform_id, identifier_type, identifier_value) VALUES "
            "(1, ?, '814380'), (3, ?, '900000')",
            (db_module.STEAM_APP_ID, db_module.STEAM_APP_ID),
        )
        conn.execute(
            "INSERT INTO steam_platform_data (game_platform_id, steamspy_cached_at, store_cached_at) VALUES "
            "(1, '2026-01-01', '2026-01-01'), (3, '2026-01-01', '2026-01-01')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                games = {
                    row["id"]: row
                    for row in await db.execute_fetchall(
                        "SELECT id, tags, igdb_cached_at FROM games"
                    )
                }
                spd = {
                    row["game_platform_id"]: row
                    for row in await db.execute_fetchall(
                        "SELECT game_platform_id, steamspy_cached_at, store_cached_at "
                        "FROM steam_platform_data"
                    )
                }

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        # Tags canonicalized + deduped in place.
        self.assertEqual(json.loads(games[1]["tags"]), ["souls-like", "difficult"])
        self.assertEqual(json.loads(games[2]["tags"]), ["metroidvania"])
        # Manual-override rows are canonicalized in place too (synonym normalized).
        self.assertEqual(json.loads(games[3]["tags"]), ["souls-like"])
        # SteamSpy cache cleared for steam rows; store cache untouched.
        self.assertIsNone(spd[1]["steamspy_cached_at"])
        self.assertEqual(spd[1]["store_cached_at"], "2026-01-01")
        # IGDB cache cleared for Steam-linked games by the v12->v13 step...
        self.assertIsNone(games[1]["igdb_cached_at"])
        self.assertIsNone(games[3]["igdb_cached_at"])
        # ...and separately for game 2 by the v23->v24 step: it's an owned
        # switch2 game with no igdb_id and no series membership, exactly the
        # false-positive-gap signature that migration re-claims.
        self.assertIsNone(games[2]["igdb_cached_at"])

    async def test_v14_to_v15_repairs_self_referencing_parent(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V12_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 14")
        # 1: orphaned self-referencing edition -> promoted back to primary.
        conn.execute(
            "INSERT INTO games (id, name, content_type, parent_game_id, is_primary_library_item) "
            "VALUES (1, 'The House in Fata Morgana', 'edition', 1, 0)"
        )
        # 2: genuine nested edition with a distinct parent -> left untouched.
        conn.execute("INSERT INTO games (id, name) VALUES (2, 'Base Game')")
        conn.execute(
            "INSERT INTO games (id, name, content_type, parent_game_id, is_primary_library_item) "
            "VALUES (3, 'Base Game: Deluxe', 'edition', 2, 0)"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                rows = {
                    row["id"]: row
                    for row in await db.execute_fetchall(
                        "SELECT id, parent_game_id, is_primary_library_item FROM games"
                    )
                }

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        # Self-referencing row is repaired: parent cleared, promoted to primary.
        self.assertIsNone(rows[1]["parent_game_id"])
        self.assertEqual(rows[1]["is_primary_library_item"], 1)
        # Legitimate nested edition with a distinct parent is left alone.
        self.assertEqual(rows[3]["parent_game_id"], 2)
        self.assertEqual(rows[3]["is_primary_library_item"], 0)

    async def test_v15_to_v16_adds_game_wishlist_table(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V12_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 15")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.execute(
            "INSERT INTO game_platforms (id, game_id, platform, owned, last_synced) "
            "VALUES (1, 1, 'steam', 1, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                tables = await db_module._table_names(db)
                gp_cols = await db_module._table_columns(db, "game_platforms")
                gp_row = await db.execute_fetchone(
                    "SELECT owned FROM game_platforms WHERE id = 1"
                )

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIn("game_wishlist", tables)
        # Wishlist tracking lives in its own table, never as a game_platforms column.
        self.assertNotIn("wishlisted_at", gp_cols)
        # Existing ownership is untouched by the additive migration.
        self.assertEqual(gp_row["owned"], 1)

    async def test_v17_to_v18_adds_game_prices_and_store_identifier(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V17_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 17")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.execute(
            "INSERT INTO game_wishlist (id, game_id, platform, wishlisted_at, source) "
            "VALUES (1, 1, 'steam', '2026-01-01T00:00:00+00:00', 'steam')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            result = await db_module.migrate_db()
            async with db_module.get_db() as db:
                cols = {
                    row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_prices)")
                }
                wl_cols = await db_module._table_columns(db, "game_wishlist")
                wl_row = await db.execute_fetchone(
                    "SELECT platform, source FROM game_wishlist WHERE id = 1"
                )

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        self.assertLessEqual(
            {
                "game_id",
                "platform",
                "shop",
                "price",
                "regular_price",
                "cut_pct",
                "currency",
                "deal_url",
                "fetched_at",
            },
            cols,
        )
        self.assertIn("store_identifier", wl_cols)
        # Existing wishlist row survives the additive migration untouched.
        self.assertEqual(wl_row["platform"], "steam")
        self.assertEqual(wl_row["source"], "steam")

    async def test_v19_adds_igdb_platforms_and_reclaims_wishlisted_games(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            # Fresh DB is already v19: column exists.
            async with db_module.get_db() as db:
                cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(games)")}
            self.assertIn("igdb_platforms", cols)

            # Step function re-claims IGDB only for wishlisted games missing availability.
            wished = await seed_game("Wishlisted Enriched")
            other = await seed_game("Not Wishlisted")
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET igdb_cached_at = '2026-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
                    (wished, other),
                )
                await db.commit()
            await db_module.upsert_wishlist_entry(wished, "steam", source="steam")

            async with db_module.get_db() as db:
                from gamelib_mcp.data.db import _migrate_v18_to_v19

                await _migrate_v18_to_v19(db, None)
                await db.commit()
                rows = {
                    r["id"]: r["igdb_cached_at"]
                    for r in await db.execute_fetchall(
                        "SELECT id, igdb_cached_at FROM games WHERE id IN (?, ?)", (wished, other)
                    )
                }

        self.assertIsNone(rows[wished])
        self.assertIsNotNone(rows[other])

    async def test_v29_adds_acquisition_columns_and_preserves_rows(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V25_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 28")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Portal 2')")
        conn.execute(
            "INSERT INTO game_platforms (game_id, platform, owned, playtime_minutes)"
            " VALUES (1, 'steam', 1, 90)"
        )
        # Pre-adding one column exercises the ALTER guard (no duplicate-column error).
        conn.execute("ALTER TABLE game_platforms ADD COLUMN price_paid REAL")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                row = await db.execute_fetchone(
                    "SELECT platform, owned, playtime_minutes, acquired_at, price_paid,"
                    "       price_currency, purchase_source, bundle_name"
                    " FROM game_platforms WHERE game_id = 1"
                )

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(row["platform"], "steam")
        self.assertEqual(row["playtime_minutes"], 90)
        for column in ("acquired_at", "price_paid", "price_currency",
                       "purchase_source", "bundle_name"):
            self.assertIsNone(row[column])

    async def test_v39_adds_assessment_presentation_and_preserves_verdicts(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V38_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 38")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.execute(
            "INSERT INTO game_assessments (game_id, assessed_at, verdict, summary)"
            " VALUES (1, '2026-08-01T00:00:00+00:00', 'buy_now', 'buy it')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                columns = await db_module._table_columns(db, "game_assessments")
                row = await db.execute_fetchone(
                    "SELECT verdict, summary, presentation FROM game_assessments"
                    " WHERE game_id = 1"
                )

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIn("presentation", columns)
        self.assertEqual(row["verdict"], "buy_now")
        self.assertEqual(row["summary"], "buy it")
        # Never backfilled: a pitch authored at recording time cannot be
        # reconstructed from the components afterwards.
        self.assertIsNone(row["presentation"])

    async def test_v35_backfills_steam_last_played_from_rtime(self) -> None:
        # v14 added game_platforms.last_played but only PSN/Nintendo ever wrote
        # it; Steam kept its epoch copy in steam_platform_data. Now that
        # last_played gates play-history window deltas, the rows already synced
        # need the value they always had.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            async with db_module.get_db() as db:
                cursor = await db.execute("INSERT INTO games (name) VALUES ('Hades')")
                game_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO game_platforms (game_id, platform, owned) VALUES (?, 'steam', 1)",
                    (game_id,),
                )
                row = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ?", (game_id,)
                )
                platform_id = row["id"]
                # 1709424000 = 2024-03-03; a second row with 0 = "never played".
                await db.execute(
                    "INSERT INTO steam_platform_data (game_platform_id, rtime_last_played)"
                    " VALUES (?, 1709424000)",
                    (platform_id,),
                )

                cursor = await db.execute("INSERT INTO games (name) VALUES ('Unplayed')")
                never_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO game_platforms (game_id, platform, owned) VALUES (?, 'steam', 1)",
                    (never_id,),
                )
                never_row = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ?", (never_id,)
                )
                await db.execute(
                    "INSERT INTO steam_platform_data (game_platform_id, rtime_last_played)"
                    " VALUES (?, 0)",
                    (never_row["id"],),
                )

                # A row that already carries a hand-pinned last_played.
                cursor = await db.execute("INSERT INTO games (name) VALUES ('Pinned')")
                pinned_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO game_platforms (game_id, platform, owned, last_played)"
                    " VALUES (?, 'steam', 1, '2020-01-01')",
                    (pinned_id,),
                )
                pinned_row = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ?", (pinned_id,)
                )
                await db.execute(
                    "INSERT INTO steam_platform_data (game_platform_id, rtime_last_played)"
                    " VALUES (?, 1709424000)",
                    (pinned_row["id"],),
                )

                await db.execute("PRAGMA user_version = 34")
                await db.commit()

            db_module._DB_READY_PATH = None
            await db_module.init_db()

            async with db_module.get_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT g.name, gp.last_played FROM game_platforms gp"
                    " JOIN games g ON g.id = gp.game_id ORDER BY g.name"
                )
            by_name = {r["name"]: r["last_played"] for r in rows}

        self.assertEqual(by_name["Hades"], "2024-03-03")
        # 0 means never played, not 1970-01-01.
        self.assertIsNone(by_name["Unplayed"])
        # An existing value is never overwritten.
        self.assertEqual(by_name["Pinned"], "2020-01-01")

    async def test_v36_backfills_snapshot_last_played_from_platform_row(self) -> None:
        # The gate reads the snapshot's frozen last_played, so existing rows
        # need the current platform value copied onto them — right today, and
        # frozen from here on, which is the point.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            async with db_module.get_db() as db:
                cursor = await db.execute("INSERT INTO games (name) VALUES ('Ghost of Tsushima')")
                game_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO game_platforms (game_id, platform, owned, last_played)"
                    " VALUES (?, 'ps5', 1, '2022-09-21')",
                    (game_id,),
                )
                for day, minutes in (("2026-07-04", 46), ("2026-08-02", 4933)):
                    await db.execute(
                        "INSERT INTO play_history"
                        " (game_id, platform, snapshot_date, playtime_minutes)"
                        " VALUES (?, 'ps5', ?, ?)",
                        (game_id, day, minutes),
                    )

                # A platform that reports no last-played at all stays NULL.
                cursor = await db.execute("INSERT INTO games (name) VALUES ('Some GOG Game')")
                gog_id = cursor.lastrowid
                await db.execute(
                    "INSERT INTO game_platforms (game_id, platform, owned) VALUES (?, 'gog', 1)",
                    (gog_id,),
                )
                await db.execute(
                    "INSERT INTO play_history"
                    " (game_id, platform, snapshot_date, playtime_minutes)"
                    " VALUES (?, 'gog', '2026-07-04', 120)",
                    (gog_id,),
                )

                await db.execute("PRAGMA user_version = 35")
                await db.commit()

            db_module._DB_READY_PATH = None
            await db_module.init_db()

            async with db_module.get_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT game_id, snapshot_date, last_played FROM play_history"
                    " ORDER BY game_id, snapshot_date"
                )
            values = [(r["game_id"], r["snapshot_date"], r["last_played"]) for r in rows]

        self.assertEqual(
            values,
            [
                (game_id, "2026-07-04", "2022-09-21"),
                (game_id, "2026-08-02", "2022-09-21"),
                (gog_id, "2026-07-04", None),
            ],
        )

    async def test_v20_reclaims_only_still_unresolved_wishlisted_games(self) -> None:
        # Handover doc: the v19 re-claim ran, but 9 of 187 wishlisted games
        # never got igdb_platforms populated (resolution gaps this branch
        # fixes). v20 re-runs the identical re-claim so those stragglers are
        # retried post-deploy with the fixed igdb.py logic — but must not
        # re-claim wishlisted games that already resolved successfully, nor
        # any non-wishlisted game.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
            self.assertEqual(version, db_module.SCHEMA_VERSION)
            self.assertEqual(db_module.SCHEMA_VERSION, 39)

            unresolved = await seed_game("Still Unresolved Wishlisted Game")
            resolved = await seed_game("Already Resolved Wishlisted Game")
            not_wishlisted = await seed_game("Not Wishlisted")
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET igdb_cached_at = '2026-01-01T00:00:00+00:00' "
                    "WHERE id IN (?, ?, ?)",
                    (unresolved, resolved, not_wishlisted),
                )
                await db.execute(
                    "UPDATE games SET igdb_platforms = '[6]' WHERE id = ?", (resolved,)
                )
                await db.commit()
            await db_module.upsert_wishlist_entry(unresolved, "steam", source="steam")
            await db_module.upsert_wishlist_entry(resolved, "steam", source="steam")

            async with db_module.get_db() as db:
                from gamelib_mcp.data.db import _migrate_v19_to_v20

                await _migrate_v19_to_v20(db, None)
                await db.commit()
                rows = {
                    r["id"]: r["igdb_cached_at"]
                    for r in await db.execute_fetchall(
                        "SELECT id, igdb_cached_at FROM games WHERE id IN (?, ?, ?)",
                        (unresolved, resolved, not_wishlisted),
                    )
                }

        self.assertIsNone(rows[unresolved])
        self.assertIsNotNone(rows[resolved])
        self.assertIsNotNone(rows[not_wishlisted])

    async def test_v27_to_v28_reclaims_poisoned_enrichment_rows(self) -> None:
        # 2026-07-05 prod audit: (1) IGDB operational failures cached as
        # permanent no-matches, (2) igdb-linked rows enriched before cover
        # fetching existed, (3) HLTB permanent NOT_FOUND sentinels. The v28
        # step re-claims exactly those three shapes and nothing else.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            stamp = "2026-07-05T02:00:00+00:00"
            false_no_match = await seed_game("Rocket League")
            missing_cover = await seed_game("Layers of Fear")
            healthy = await seed_game("Portal 2")
            never_checked = await seed_game("Fresh Import")
            hltb_not_found = await seed_game("HITMAN 2")
            hltb_cached = await seed_game("Elden Ring")
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET igdb_cached_at = ?, igdb_claimed_at = ? WHERE id = ?",
                    (stamp, stamp, false_no_match),
                )
                await db.execute(
                    "UPDATE games SET igdb_id = 111, igdb_cached_at = ? WHERE id = ?",
                    (stamp, missing_cover),
                )
                await db.execute(
                    "UPDATE games SET igdb_id = 222, cover_image_id = 'co123', "
                    "igdb_cached_at = ? WHERE id = ?",
                    (stamp, healthy),
                )
                await db.execute(
                    "UPDATE games SET hltb_cached_at = 'NOT_FOUND', hltb_claimed_at = ? "
                    "WHERE id = ?",
                    (stamp, hltb_not_found),
                )
                await db.execute(
                    "UPDATE games SET hltb_cached_at = ? WHERE id = ?",
                    (stamp, hltb_cached),
                )
                await db.commit()

            async with db_module.get_db() as db:
                from gamelib_mcp.data.db import _migrate_v27_to_v28

                await _migrate_v27_to_v28(db, None)
                await db.commit()
                rows = {
                    r["id"]: r
                    for r in await db.execute_fetchall(
                        "SELECT id, igdb_cached_at, igdb_claimed_at, hltb_cached_at, "
                        "hltb_claimed_at FROM games"
                    )
                }

        # False no-match: fully re-claimed.
        self.assertIsNone(rows[false_no_match]["igdb_cached_at"])
        self.assertIsNone(rows[false_no_match]["igdb_claimed_at"])
        # Linked but missing a cover: re-claimed (re-fetches by id).
        self.assertIsNone(rows[missing_cover]["igdb_cached_at"])
        # Linked with a cover: untouched.
        self.assertEqual(rows[healthy]["igdb_cached_at"], stamp)
        # Never checked at all: nothing to reset.
        self.assertIsNone(rows[never_checked]["igdb_cached_at"])
        # HLTB NOT_FOUND sentinel: re-claimed; ordinary cache stamp untouched.
        self.assertIsNone(rows[hltb_not_found]["hltb_cached_at"])
        self.assertIsNone(rows[hltb_not_found]["hltb_claimed_at"])
        self.assertEqual(rows[hltb_cached]["hltb_cached_at"], stamp)

    async def test_v20_to_v21_adds_completion_status(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V19_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 20")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            result = await db_module.migrate_db()
            async with db_module.get_db() as db:
                cols = {
                    row[1] for row in await db.execute_fetchall("PRAGMA table_info(games)")
                }
                row = await db.execute_fetchone(
                    "SELECT completion_status FROM games WHERE id = 1"
                )

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        self.assertIn("completion_status", cols)
        # Existing row survives the additive migration untouched (NULL = unset).
        self.assertIsNone(row["completion_status"])

    async def test_fresh_v21_db_enforces_completion_status_check(self) -> None:
        # A freshly-initialized database gets the canonical DDL (with the CHECK
        # constraint) directly; only in-place migrations add a plain column
        # (see _migrate_v20_to_v21) since older SQLite versions can't add a
        # CHECK'd column via ALTER TABLE.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    await db.execute(
                        "INSERT INTO games (name, completion_status) VALUES ('x', 'finished')"
                    )

    async def test_v21_to_v22_adds_play_history(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V20_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 21")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            result = await db_module.migrate_db()
            async with db_module.get_db() as db:
                cols = {
                    row[1] for row in await db.execute_fetchall("PRAGMA table_info(play_history)")
                }

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        self.assertEqual(
            cols,
            {"game_id", "platform", "snapshot_date", "playtime_minutes", "last_played"},
        )

    async def test_v26_to_v27_quarantines_widened_feature_vocabulary(self) -> None:
        # v26 -> v27 is data-only (schema identical), so a fresh current-schema
        # DB downgraded to user_version 26 exercises it directly.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 26")
        conn.execute(
            "INSERT INTO games (id, name, tags, features) VALUES "
            "(1, 'Racer', '[\"racing\", \"save anytime\", \"digital distribution\"]',"
            " '[\"steam cloud\"]')"
        )
        conn.execute(
            "INSERT INTO ratings (game_id, source, raw_score, normalized_score, synced_at)"
            " VALUES (1, 'manual', 9.0, 9.0, 'now')"
        )
        # Stale rows on the pre-v27 avg*log(count) scale — the new read side
        # would misinterpret them as signed centered affinities.
        conn.execute(
            "INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)"
            " VALUES ('save anytime', 0.4, 7.0, 3, 'now')"
        )
        conn.execute(
            "INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)"
            " VALUES ('racing', 6.2, 9.0, 1, 'now')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT tags, features FROM games WHERE id = 1").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        affinity = {
            r["tag"]: r["affinity_score"]
            for r in conn.execute("SELECT tag, affinity_score FROM tag_affinity")
        }
        conn.close()

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(row["tags"], '["racing"]')
        # Merged into existing features, not overwritten.
        self.assertEqual(
            row["features"], '["steam cloud", "save anytime", "digital distribution"]'
        )
        # init_db recomputed affinity under the new formula: the quarantined
        # tag is gone and the surviving tag is on the mean-centered scale (a
        # single 9.0 rating IS the mean, so it centers to zero) rather than
        # keeping its stale 6.2.
        self.assertEqual(set(affinity), {"racing"})
        self.assertAlmostEqual(affinity["racing"], 0.0, places=6)

    async def test_v29_to_v30_quarantines_igdb_metadata_keyword_families(self) -> None:
        # v29 -> v30 is data-only (schema identical), so a fresh current-schema
        # DB downgraded to user_version 29 exercises it directly. The affinity
        # purge must catch prefix-family members ("previously on - ...") that
        # an IN-list over STEAM_FEATURE_FLAGS cannot express.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA user_version = 29")
        conn.execute(
            "INSERT INTO games (id, name, tags, features) VALUES "
            "(1, 'Shogun', '[\"deck-building\", \"previously on - prime gaming\","
            " \"pax west 2017\", \"kickstarter\"]', '[\"steam cloud\"]')"
        )
        conn.execute(
            "INSERT INTO ratings (game_id, source, raw_score, normalized_score, synced_at)"
            " VALUES (1, 'manual', 10.0, 10.0, 'now')"
        )
        conn.execute(
            "INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)"
            " VALUES ('previously on - prime gaming', 0.97, 9.15, 3, 'now')"
        )
        conn.execute(
            "INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)"
            " VALUES ('deck-building', 1.35, 9.23, 6, 'now')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT tags, features FROM games WHERE id = 1").fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        affinity_tags = {
            r["tag"] for r in conn.execute("SELECT tag FROM tag_affinity")
        }
        conn.close()

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertEqual(row["tags"], '["deck-building"]')
        self.assertEqual(
            row["features"],
            '["steam cloud", "previously on - prime gaming", "pax west 2017", "kickstarter"]',
        )
        self.assertIn("deck-building", affinity_tags)
        self.assertNotIn("previously on - prime gaming", affinity_tags)

    async def test_v22_to_v23_rebuilds_check_constraint_for_evergreen(self) -> None:
        # A DB fresh-initialized while SCHEMA_VERSION was 21 or 22 got the
        # canonical DDL directly, CHECK constraint and all — _V21_SCHEMA_DDL
        # (unchanged by this branch) still carries the pre-evergreen CHECK.
        # Related rows (game_platforms, an FK child) must survive the rebuild.
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V21_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 22")
        conn.execute(
            "INSERT INTO games (id, name, completion_status) "
            "VALUES (1, 'Rocket League', 'playing')"
        )
        conn.execute(
            """INSERT INTO game_platforms
               (id, game_id, platform, owned, last_synced)
               VALUES (1, 1, 'steam', 1, '2024-01-01T00:00:00+00:00')"""
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            result = await db_module.migrate_db()
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT name, completion_status FROM games WHERE id = 1"
                )
                gp_row = await db.execute_fetchone(
                    "SELECT game_id, platform FROM game_platforms WHERE id = 1"
                )
                # Previously rejected by the old CHECK; must succeed now.
                await db.execute(
                    "UPDATE games SET completion_status = 'evergreen' WHERE id = 1"
                )
                await db.commit()
                updated = await db.execute_fetchone(
                    "SELECT completion_status FROM games WHERE id = 1"
                )
                index_names = {
                    r[0]
                    for r in await db.execute_fetchall(
                        "SELECT name FROM sqlite_master "
                        "WHERE type = 'index' AND tbl_name = 'games'"
                    )
                }

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        self.assertEqual(row["name"], "Rocket League")
        self.assertEqual(row["completion_status"], "playing")
        self.assertEqual(gp_row["game_id"], 1)
        self.assertEqual(gp_row["platform"], "steam")
        self.assertEqual(updated["completion_status"], "evergreen")
        for expected_index in db_module._GAMES_TABLE_INDEXES:
            self.assertIn(expected_index, index_names)

    async def test_v22_to_v23_skips_rebuild_when_no_check_constraint(self) -> None:
        # A DB that only ever added completion_status via the v20->v21 plain
        # ALTER TABLE has no CHECK to begin with and needs no repair.
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V19_SCHEMA_DDL)
        conn.execute("ALTER TABLE games ADD COLUMN completion_status TEXT")
        conn.execute("PRAGMA user_version = 22")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Tabletop Simulator')")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            result = await db_module.migrate_db()
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET completion_status = 'evergreen' WHERE id = 1"
                )
                await db.commit()
                updated = await db.execute_fetchone(
                    "SELECT completion_status FROM games WHERE id = 1"
                )

        self.assertEqual(result.final_version, db_module.SCHEMA_VERSION)
        self.assertEqual(updated["completion_status"], "evergreen")

    async def test_v23_to_v24_reclaims_owned_or_wishlisted_games_missing_series_data(
        self,
    ) -> None:
        # discover_series_gaps false-positived owned games it couldn't match
        # to a series member: (a) an owned row with no igdb_id at all (e.g.
        # "Borderlands GOTY"), and (b) an owned row whose igdb_id resolved to
        # an edition-specific IGDB entry with no series membership of its own
        # (e.g. "The Witcher: Enhanced Edition"). v24 re-claims both so
        # background IGDB backfill re-links them; games that already have a
        # linked igdb_id AND a series membership, and games that are neither
        # owned nor wishlisted, must be left alone.
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()

            no_igdb_owned = await seed_game("Borderlands GOTY")
            await add_platform(no_igdb_owned, "steam", owned=1)

            edition_no_membership = await seed_game("The Witcher: Enhanced Edition")
            await add_platform(edition_no_membership, "steam", owned=1)
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET igdb_id = ? WHERE id = ?",
                    (283715, edition_no_membership),
                )
                await db.commit()

            fully_resolved = await seed_game("The Witcher")
            await add_platform(fully_resolved, "steam", owned=1)
            async with db_module.get_db() as db:
                await db.execute(
                    "UPDATE games SET igdb_id = ? WHERE id = ?", (80, fully_resolved)
                )
                await db.commit()
            await db_module.upsert_game_series_links(
                fully_resolved, [("collection", 900, "The Witcher")]
            )

            wishlisted_no_igdb = await seed_game("Wishlisted No IGDB")
            await db_module.upsert_wishlist_entry(wishlisted_no_igdb, "steam", source="steam")

            unowned_unwishlisted = await seed_game("Neither Owned Nor Wishlisted")

            target_ids = (
                no_igdb_owned,
                edition_no_membership,
                fully_resolved,
                wishlisted_no_igdb,
                unowned_unwishlisted,
            )
            async with db_module.get_db() as db:
                await db.execute(
                    f"UPDATE games SET igdb_cached_at = '2026-01-01T00:00:00+00:00', "
                    f"igdb_claimed_at = '2026-01-01T00:00:00+00:00' "
                    f"WHERE id IN ({','.join('?' * len(target_ids))})",
                    target_ids,
                )
                await db.commit()

                from gamelib_mcp.data.db import _migrate_v23_to_v24

                await _migrate_v23_to_v24(db, None)
                await db.commit()

                rows = {
                    r["id"]: (r["igdb_cached_at"], r["igdb_claimed_at"])
                    for r in await db.execute_fetchall(
                        f"SELECT id, igdb_cached_at, igdb_claimed_at FROM games "
                        f"WHERE id IN ({','.join('?' * len(target_ids))})",
                        target_ids,
                    )
                }

        self.assertEqual(rows[no_igdb_owned], (None, None))
        self.assertEqual(rows[edition_no_membership], (None, None))
        self.assertEqual(
            rows[fully_resolved], ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00")
        )
        self.assertEqual(rows[wishlisted_no_igdb], (None, None))
        self.assertEqual(
            rows[unowned_unwishlisted],
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )

    async def test_fresh_db_accepts_evergreen_directly(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                await db.execute(
                    "INSERT INTO games (name, completion_status) VALUES ('Rocket League', 'evergreen')"
                )
                await db.commit()
                row = await db.execute_fetchone(
                    "SELECT completion_status FROM games WHERE name = 'Rocket League'"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    await db.execute(
                        "INSERT INTO games (name, completion_status) VALUES ('x', 'finished')"
                    )

        self.assertEqual(row["completion_status"], "evergreen")

    async def test_v9_to_v10_adds_series_tables(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V9_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 9")
        conn.execute("INSERT INTO games (id, name) VALUES (1, 'Hollow Knight')")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            # Existing v9 game data survives the migration, and series writes work.
            await db_module.upsert_game_series_links(
                1, [("collection", 5, "Hollow Knight"), ("franchise", 6, "Team Cherry")]
            )
            async with db_module.get_db() as db:
                version = await db_module._get_user_version(db)
                tables = await db_module._table_names(db)
            series = await db_module.load_series_for_games([1])

        self.assertEqual(version, db_module.SCHEMA_VERSION)
        self.assertIn("game_series", tables)
        self.assertIn("game_series_membership", tables)
        self.assertEqual(
            {(s["kind"], s["name"]) for s in series[1]},
            {("collection", "Hollow Knight"), ("franchise", "Team Cherry")},
        )

    async def test_v9_to_v10_requeues_cached_igdb_games_for_series_backfill(self) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.executescript(db_module._V9_SCHEMA_DDL)
        conn.execute("PRAGMA user_version = 9")
        # Game 1: previously matched + cached by IGDB -> must be requeued.
        conn.execute(
            "INSERT INTO games (id, name, igdb_id, igdb_cached_at, igdb_claimed_at) "
            "VALUES (1, 'Hollow Knight', 999, '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        # Game 2: checked but never matched (no igdb_id) -> left untouched.
        conn.execute(
            "INSERT INTO games (id, name, igdb_id, igdb_cached_at) "
            "VALUES (2, 'Some Indie', NULL, '2026-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                matched = await db.execute_fetchone(
                    "SELECT igdb_cached_at, igdb_claimed_at FROM games WHERE id = 1"
                )
                unmatched = await db.execute_fetchone(
                    "SELECT igdb_cached_at FROM games WHERE id = 2"
                )

        # Matched game is requeued (both timestamps cleared) so the IGDB worker
        # revisits it and backfills series. The unmatched game used to stay
        # untouched here (v10 only requeues igdb_id-matched rows), but the full
        # chain now ends at v28 which deliberately re-claims every checked
        # no-match row (the 2026-07-05 false no-match audit), so it ends up
        # requeued too.
        self.assertIsNone(matched["igdb_cached_at"])
        self.assertIsNone(matched["igdb_claimed_at"])
        self.assertIsNone(unmatched["igdb_cached_at"])

    async def test_upsert_game_maintains_name_normalized(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            game_id = await db_module.upsert_game(appid=None, name="Hades II")
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT name_normalized FROM games WHERE id = ?", (game_id,)
                )

        self.assertEqual(row["name_normalized"], "hades ii")

    async def test_bulk_steam_sync_backfills_name_normalized(self) -> None:
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            await db_module.bulk_upsert_steam_library(
                [{"appid": 814380, "name": "Sekiro: Shadows Die Twice", "playtime_minutes": 344}],
                synced_at="2026-06-11T00:00:00+00:00",
            )
            async with db_module.get_db() as db:
                row = await db.execute_fetchone(
                    "SELECT name_normalized FROM games WHERE name = ?",
                    ("Sekiro: Shadows Die Twice",),
                )

        self.assertEqual(row["name_normalized"], "sekiro shadows die twice")

    async def test_redundant_lookup_index_is_removed(self):
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                indexes = {row[1] for row in await db.execute_fetchall("PRAGMA index_list(game_platform_identifiers)")}

        self.assertNotIn("idx_game_platform_identifiers_lookup", indexes)

    async def test_migration_drops_redundant_lookup_index(self):
        # Create a v4 DB that has the redundant index present
        conn = sqlite3.connect(str(self.db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS games (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                igdb_id          INTEGER UNIQUE,
                name             TEXT NOT NULL,
                sort_name        TEXT,
                release_date     TEXT,
                genres           TEXT,
                tags             TEXT,
                short_description TEXT,
                hltb_main        REAL,
                hltb_extra       REAL,
                hltb_complete    REAL,
                hltb_cached_at   TEXT,
                hltb_claimed_at  TEXT,
                igdb_cached_at   TEXT,
                igdb_claimed_at  TEXT,
                is_farmed        INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS game_platforms (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id          INTEGER NOT NULL REFERENCES games(id),
                platform         TEXT NOT NULL,
                owned            INTEGER NOT NULL DEFAULT 1,
                playtime_minutes INTEGER,
                playtime_2weeks_minutes INTEGER,
                last_synced      TEXT,
                UNIQUE(game_id, platform)
            );
            CREATE TABLE IF NOT EXISTS game_platform_identifiers (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                game_platform_id INTEGER NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
                identifier_type  TEXT NOT NULL,
                identifier_value TEXT NOT NULL,
                is_primary       INTEGER NOT NULL DEFAULT 1,
                last_seen_at     TEXT,
                UNIQUE(identifier_type, identifier_value)
            );
            CREATE TABLE IF NOT EXISTS steam_platform_data (
                game_platform_id    INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
                steam_review_score  INTEGER,
                steam_review_desc   TEXT,
                protondb_tier       TEXT,
                store_cached_at     TEXT,
                store_claimed_at    TEXT,
                protondb_cached_at  TEXT,
                protondb_claimed_at TEXT,
                steamspy_cached_at  TEXT,
                steamspy_claimed_at TEXT,
                rtime_last_played   INTEGER,
                library_updated_at  TEXT
            );
            CREATE TABLE IF NOT EXISTS game_platform_enrichment (
                game_platform_id       INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
                platform_release_date  TEXT,
                metacritic_score       INTEGER,
                metacritic_url         TEXT,
                metacritic_claimed_at  TEXT,
                opencritic_id          INTEGER,
                opencritic_score       INTEGER,
                opencritic_tier        TEXT,
                opencritic_percent_rec REAL,
                opencritic_cached_at   TEXT,
                opencritic_claimed_at  TEXT,
                metacritic_cached_at   TEXT
            );
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id INTEGER REFERENCES games(id),
                source TEXT NOT NULL,
                raw_score REAL,
                normalized_score REAL,
                review_text TEXT,
                synced_at TEXT NOT NULL,
                UNIQUE(game_id, source)
            );
            CREATE TABLE IF NOT EXISTS tag_affinity (
                tag TEXT PRIMARY KEY,
                affinity_score REAL,
                avg_score REAL,
                game_count INTEGER,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_game_platforms_game_id ON game_platforms(game_id);
            CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform);
            CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_platform_id
                ON game_platform_identifiers(game_platform_id);
            CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
                ON game_platform_identifiers(identifier_type, identifier_value);
        """)
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
        conn.close()

        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            async with db_module.get_db() as db:
                indexes = {row[1] for row in await db.execute_fetchall("PRAGMA index_list(game_platform_identifiers)")}

        self.assertNotIn("idx_game_platform_identifiers_lookup", indexes)
        self.assertIn("sqlite_autoindex_game_platform_identifiers_1", indexes)

    async def test_identifier_primary_repair_demotes_extra_rows(self):
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            game_id = await db_module.upsert_game(appid=None, name="TestGame")
            platform_id = await db_module.upsert_game_platform(
                game_id=game_id,
                platform="steam",
                playtime_minutes=0,
                owned=1,
            )

            now = "2026-04-08T00:00:00+00:00"
            async with db_module.get_db() as db:
                # Insert two rows for the same (game_platform_id, identifier_type) with is_primary=1
                # We need to bypass the UNIQUE constraint on (identifier_type, identifier_value)
                # by using different identifier_value values
                await db.execute(
                    "INSERT INTO game_platform_identifiers (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) VALUES (?, ?, ?, 1, ?)",
                    (platform_id, "steam_appid", "100", now),
                )
                await db.execute(
                    "INSERT INTO game_platform_identifiers (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) VALUES (?, ?, ?, 1, ?)",
                    (platform_id, "steam_appid", "101", now),
                )
                await db.commit()

                await db_module._repair_identifier_primary_flags(db)

                rows = await db.execute_fetchall(
                    "SELECT identifier_value, is_primary FROM game_platform_identifiers WHERE game_platform_id = ? AND identifier_type = ? ORDER BY id",
                    (platform_id, "steam_appid"),
                )

        self.assertEqual([row[1] for row in rows], [1, 0])

    async def test_upsert_identifier_demotes_existing_primary(self):
        db_module._DB_READY_PATH = None
        with patch.dict("os.environ", {"DATABASE_URL": f"file:{self.db_path}"}, clear=False):
            await db_module.init_db()
            game_id = await db_module.upsert_game(appid=None, name="TestGame2")
            platform_id = await db_module.upsert_game_platform(
                game_id=game_id,
                platform="steam",
                playtime_minutes=0,
                owned=1,
            )

            # Write first identifier as primary
            await db_module.upsert_game_platform_identifier(
                game_platform_id=platform_id,
                identifier_type="steam_appid",
                identifier_value="200",
                is_primary=True,
            )

            # Write second identifier as primary for same (platform_id, identifier_type)
            await db_module.upsert_game_platform_identifier(
                game_platform_id=platform_id,
                identifier_type="steam_appid",
                identifier_value="201",
                is_primary=True,
            )

            async with db_module.get_db() as db:
                rows = await db.execute_fetchall(
                    "SELECT identifier_value, is_primary FROM game_platform_identifiers WHERE game_platform_id = ? AND identifier_type = ? ORDER BY id",
                    (platform_id, "steam_appid"),
                )

        # First identifier should be demoted, second should be primary
        primaries = [row["is_primary"] for row in rows]
        self.assertEqual(sum(primaries), 1, "Exactly one row should be primary")
        self.assertEqual(rows[-1]["is_primary"], 1, "Most recently written row should be primary")


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


class BackgroundEnrichmentRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_store_batch_processes_multiple_games_concurrently(self) -> None:
        from gamelib_mcp.data import enrich_bg

        rows = [
            {"game_platform_id": 11, "appid": 10, "name": "Portal 2"},
            {"game_platform_id": 12, "appid": 20, "name": "Half-Life 2"},
        ]
        in_flight = 0
        peak_in_flight = 0
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_enrich_game(appid: int, *args, **kwargs) -> None:
            nonlocal in_flight, peak_in_flight
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            if in_flight >= 2:
                both_started.set()
            try:
                await release.wait()
            finally:
                in_flight -= 1

        with (
            patch.object(enrich_bg, "claim_steam_platform_ids_for_store", AsyncMock(return_value=[11, 12])),
            patch.object(enrich_bg, "load_store_batch_rows", AsyncMock(return_value=rows)),
            patch.object(enrich_bg, "enrich_game", AsyncMock(side_effect=fake_enrich_game)),
            patch.object(enrich_bg, "_finalize_store_claim", AsyncMock()),
            patch.object(enrich_bg.asyncio, "sleep", AsyncMock()),
            patch.object(enrich_bg, "_STORE_START_INTERVAL", 0.0),
        ):
            task = asyncio.create_task(enrich_bg._run_store_batch())
            await asyncio.wait_for(both_started.wait(), timeout=DEADLOCK_TIMEOUT)
            release.set()
            count = await asyncio.wait_for(task, timeout=DEADLOCK_TIMEOUT)

        self.assertEqual(count, 2)
        self.assertGreaterEqual(peak_in_flight, 2)


if __name__ == "__main__":
    unittest.main()
