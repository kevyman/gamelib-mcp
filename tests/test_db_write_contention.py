"""bulk_upsert_steam_library vs. concurrent platform commits.

The Steam bulk upsert is the one read-then-write bulk transaction in the sync
path: each chunk resolves appids against the live tables, then writes. As a
deferred transaction that shape can only lose — any other platform committing
between the read and the write invalidates the read snapshot, SQLite raises
SQLITE_BUSY_SNAPSHOT immediately (busy_timeout deliberately not consulted),
and the retry_on_write_contention backstop's ~1.5s budget is no match for a
full library refresh where five other platforms commit for the whole run. In
production Steam lost that race on 100% of full refreshes (2026-08-02 through
2026-08-04) while succeeding instantly when synced alone.

The fix is BEGIN IMMEDIATE per chunk: the transaction is a writer from its
first statement, so it queues behind other writers under busy_timeout instead
of building a snapshot someone else can invalidate. These tests pin both the
behavior (survives a continuously-committing concurrent writer) and the
mechanism (each chunk opens IMMEDIATE before touching the main schema).
"""

import asyncio
import contextlib
import unittest

from conftest import DEADLOCK_TIMEOUT, ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data.db import upserts
from gamelib_mcp.data.db.upserts import bulk_upsert_steam_library, upsert_game_platform

SYNCED_AT = "2026-08-03T22:40:00+00:00"


def _steam_rows(count: int, *, start_appid: int = 900_000) -> list[dict]:
    return [
        {
            "appid": start_appid + i,
            "name": f"Contention Test Game {i}",
            "playtime_minutes": i,
            "playtime_2weeks_minutes": None,
            "rtime_last_played": None,
        }
        for i in range(count)
    ]


class BulkUpsertUnderConcurrentCommitsTests(ToolDBTestCase):
    async def test_bulk_upsert_completes_while_another_platform_commits(self):
        """The production shape: Steam's chunked upsert interleaved with another
        platform sync committing single-row upserts the whole time."""
        other_game = await seed_game("Rival Platform Game")
        await add_platform(other_game, "gog")

        bulk_done = asyncio.Event()
        rival_commits = 0

        async def rival_sync():
            nonlocal rival_commits
            while not bulk_done.is_set():
                await upsert_game_platform(
                    other_game, "gog", playtime_minutes=rival_commits, from_source=True
                )
                rival_commits += 1

        rival = asyncio.create_task(rival_sync())
        try:
            # Small chunks force many read-then-write transactions, maximizing
            # the interleaving a full refresh produces.
            written = await asyncio.wait_for(
                bulk_upsert_steam_library(_steam_rows(120), SYNCED_AT, chunk_size=10),
                timeout=DEADLOCK_TIMEOUT,
            )
        finally:
            bulk_done.set()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(rival, timeout=DEADLOCK_TIMEOUT)

        self.assertEqual(written, 120)
        self.assertGreater(rival_commits, 0, "rival writer never committed — no contention exercised")

        # Every row landed despite the contention.
        from gamelib_mcp.data import db as db_module

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                """SELECT COUNT(*) AS n FROM game_platforms gp
                   JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                   WHERE gp.platform = 'steam' AND gpi.identifier_type = 'steam_appid'
                     AND CAST(gpi.identifier_value AS INTEGER) >= 900000"""
            )
        self.assertEqual(row["n"], 120)


class _RecordingConnection:
    """Delegating proxy that records every SQL statement executed."""

    def __init__(self, conn, statements: list[str]):
        self._conn = conn
        self._statements = statements

    def _record(self, sql: str) -> None:
        self._statements.append(" ".join(sql.split()))

    async def execute(self, sql, *args, **kwargs):
        self._record(sql)
        return await self._conn.execute(sql, *args, **kwargs)

    async def executemany(self, sql, *args, **kwargs):
        self._record(sql)
        return await self._conn.executemany(sql, *args, **kwargs)

    async def execute_fetchall(self, sql, *args, **kwargs):
        self._record(sql)
        return await self._conn.execute_fetchall(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class BulkUpsertTransactionShapeTests(ToolDBTestCase):
    async def test_each_chunk_begins_immediate_before_reading_main_schema(self):
        """Drift guard on the mechanism: the write lock is taken before the
        chunk's first read of the live tables, for every chunk."""
        from gamelib_mcp.data import db as db_module

        statements: list[str] = []
        real_get_db = db_module.get_db

        @contextlib.asynccontextmanager
        async def recording_get_db():
            async with real_get_db() as conn:
                yield _RecordingConnection(conn, statements)

        original = upserts.get_db
        upserts.get_db = recording_get_db
        try:
            # Deadlock guard, not a latency assertion (conftest convention —
            # this was the one await in this file without one). Three ten-row
            # chunks on a private DB finish in well under a second even on a
            # fully saturated 4-core box (measured 0.6s for this whole file
            # under CPU stress), so 10s is ~20x margin — while the un-guarded
            # call is what burned two 10-minute deploy jobs when this test
            # wedged on the CI runner with nothing to show for it (issue #155).
            await asyncio.wait_for(
                bulk_upsert_steam_library(
                    _steam_rows(30, start_appid=910_000), SYNCED_AT, chunk_size=10
                ),
                timeout=DEADLOCK_TIMEOUT,
            )
        finally:
            upserts.get_db = original

        begins = [i for i, sql in enumerate(statements) if sql == "BEGIN IMMEDIATE"]
        self.assertEqual(len(begins), 3, f"expected one BEGIN IMMEDIATE per chunk: {statements}")

        main_reads = [
            i for i, sql in enumerate(statements) if "game_platform_identifiers" in sql
        ]
        self.assertTrue(main_reads, "resolution pass never read the main schema")
        self.assertLess(
            begins[0],
            main_reads[0],
            "chunk read the main schema before taking the write lock — the "
            "BUSY_SNAPSHOT-prone deferred shape is back",
        )


if __name__ == "__main__":
    unittest.main()
