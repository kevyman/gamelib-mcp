import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data import steam_store


class MigrationRegressionTests(unittest.IsolatedAsyncioTestCase):
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
            await asyncio.wait_for(both_started.wait(), timeout=1.0)
            release.set()
            count = await asyncio.wait_for(task, timeout=1.0)

        self.assertEqual(count, 2)
        self.assertGreaterEqual(peak_in_flight, 2)


if __name__ == "__main__":
    unittest.main()
