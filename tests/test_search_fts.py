"""FTS5 trigram index: lifecycle, trigger sync, and LIKE-parity of results."""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from gamelib_mcp.data import db as db_module
from gamelib_mcp.tools.search import build_name_match

FTS_AVAILABLE = True
try:
    sqlite3.connect(":memory:").execute(
        "CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')"
    )
except sqlite3.OperationalError:
    FTS_AVAILABLE = False

NAMES = [
    "Sekiro: Shadows Die Twice",
    "Dead Space",
    "Dead Space 2",
    "Hades II",
    "Ori and the Blind Forest",
    "OK K.O.! Let's Play Heroes",
]

QUERIES = [
    "sekiro",
    "sekiro shadows die twice",
    "dead space",
    "space",
    "hades ii",          # short token "ii" must still match via LIKE
    "ori",
    "blind forest",
    "zzz no such game",
    "",                   # empty query -> match everything
    "%",                  # noise query -> match nothing
]


@unittest.skipUnless(FTS_AVAILABLE, "SQLite build lacks FTS5/trigram")
class FtsTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self._tmp.name, "fts_test.db")
        self._env = patch.dict(os.environ, {"DATABASE_URL": db_path})
        self._env.start()
        db_module._DB_READY_PATH = None

    def tearDown(self):
        self._env.stop()
        db_module._DB_READY_PATH = None
        self._tmp.cleanup()

    async def _seed(self):
        await db_module.init_db()
        for name in NAMES:
            await db_module.upsert_game(None, name, match_existing_by_name=False)


class TestFtsLifecycle(FtsTestBase):
    async def test_init_db_creates_fts_and_reports_ready(self):
        await db_module.init_db()
        self.assertTrue(db_module.fts_ready())
        async with db_module.get_db() as db:
            cur = await db.execute(
                "SELECT name FROM sqlite_master WHERE name = 'games_fts'"
            )
            self.assertIsNotNone(await cur.fetchone())

    async def test_triggers_keep_index_live(self):
        await self._seed()
        async with db_module.get_db() as db:
            cur = await db.execute(
                "SELECT rowid FROM games_fts WHERE games_fts MATCH '\"sekiro\"'"
            )
            row = await cur.fetchone()
            self.assertIsNotNone(row)
            game_id = row[0]
            await db.execute(
                "UPDATE games SET name = 'Renamed Game', name_normalized = 'renamed game'"
                " WHERE id = ?",
                (game_id,),
            )
            await db.commit()
            cur = await db.execute(
                "SELECT rowid FROM games_fts WHERE games_fts MATCH '\"sekiro\"'"
            )
            self.assertIsNone(await cur.fetchone())
            cur = await db.execute(
                "SELECT rowid FROM games_fts WHERE games_fts MATCH '\"renamed\"'"
            )
            self.assertIsNotNone(await cur.fetchone())
            await db.execute("DELETE FROM games WHERE id = ?", (game_id,))
            await db.commit()
            cur = await db.execute(
                "SELECT rowid FROM games_fts WHERE games_fts MATCH '\"renamed\"'"
            )
            self.assertIsNone(await cur.fetchone())


class TestFtsParityWithLike(FtsTestBase):
    async def _run_match(self, db, match):
        sql = (
            f"SELECT g.id, {match.rank_sql} AS rank FROM games g"
            f" WHERE {match.where_sql} ORDER BY rank, g.id"
        )
        cur = await db.execute(sql, [*match.rank_params, *match.where_params])
        return [tuple(row) for row in await cur.fetchall()]

    async def test_fts_results_match_like_results(self):
        await self._seed()
        from gamelib_mcp.tools.search import NORMALIZED_NAME_SQL

        async with db_module.get_db() as db:
            for query in QUERIES:
                with self.subTest(query=query):
                    like = build_name_match(query, column=NORMALIZED_NAME_SQL)
                    fts = build_name_match(
                        query, column=NORMALIZED_NAME_SQL, use_fts=True
                    )
                    self.assertEqual(
                        await self._run_match(db, like),
                        await self._run_match(db, fts),
                    )


if __name__ == "__main__":
    unittest.main()
