"""Tests for gamelib_mcp.data.db.history.record_play_history_snapshots."""

from conftest import ToolDBTestCase, add_platform, seed_game

from gamelib_mcp.data import db as db_module


class RecordPlayHistorySnapshotsTests(ToolDBTestCase):
    async def _rows_for(self, game_id: int, platform: str) -> list[dict]:
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT snapshot_date, playtime_minutes FROM play_history
                   WHERE game_id = ? AND platform = ? ORDER BY snapshot_date""",
                (game_id, platform),
            )
        return [dict(zip(("snapshot_date", "playtime_minutes"), row)) for row in rows]

    async def test_snapshot_written_for_changed_playtime(self):
        game_id = await seed_game("Hades")
        await add_platform(game_id, "steam", playtime_minutes=100)

        n = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-02")

        self.assertEqual(n, 1)
        rows = await self._rows_for(game_id, "steam")
        self.assertEqual(rows, [{"snapshot_date": "2026-07-02", "playtime_minutes": 100}])

    async def test_no_snapshot_when_unchanged(self):
        game_id = await seed_game("Hades")
        await add_platform(game_id, "steam", playtime_minutes=100)

        first = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-02")
        second = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-03")

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        rows = await self._rows_for(game_id, "steam")
        self.assertEqual(rows, [{"snapshot_date": "2026-07-02", "playtime_minutes": 100}])

    async def test_same_day_resync_overwrites_todays_row(self):
        game_id = await seed_game("Hades")
        gp_id = await add_platform(game_id, "steam", playtime_minutes=100)

        first = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-02")
        await db_module.upsert_game_platform(
            game_id, "steam", playtime_minutes=130, owned=1
        )
        second = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-02")

        self.assertEqual(first, 1)
        self.assertEqual(second, 1)
        rows = await self._rows_for(game_id, "steam")
        self.assertEqual(rows, [{"snapshot_date": "2026-07-02", "playtime_minutes": 130}])
        self.assertIsNotNone(gp_id)

    async def test_null_playtime_not_snapshotted(self):
        game_id = await seed_game("Cyberpunk 2077")
        await add_platform(game_id, "gog", playtime_minutes=None)

        n = await db_module.record_play_history_snapshots("gog", snapshot_date="2026-07-02")

        self.assertEqual(n, 0)
        rows = await self._rows_for(game_id, "gog")
        self.assertEqual(rows, [])

    async def test_unowned_platform_row_not_snapshotted(self):
        game_id = await seed_game("Wishlisted Game")
        await add_platform(game_id, "steam", playtime_minutes=0, owned=0)

        n = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-02")

        self.assertEqual(n, 0)
        rows = await self._rows_for(game_id, "steam")
        self.assertEqual(rows, [])

    async def test_only_targets_requested_platform(self):
        game_id = await seed_game("Multi-plat Game")
        await add_platform(game_id, "steam", playtime_minutes=50)
        await add_platform(game_id, "switch2", playtime_minutes=75)

        n = await db_module.record_play_history_snapshots("steam", snapshot_date="2026-07-02")

        self.assertEqual(n, 1)
        self.assertEqual(await self._rows_for(game_id, "switch2"), [])


class SnapshotLastPlayedTests(ToolDBTestCase):
    """Each snapshot freezes the platform's last_played as of that observation.

    tools/history.py reads it to tell a real session from a correction to the
    stored total. It must be copied, not read live: game_platforms.last_played
    moves, and reading it would make a past window's answer change the next
    time the game is launched.
    """

    async def _rows(self, game_id: int, platform: str) -> list[tuple[str, int, str | None]]:
        async with db_module.get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT snapshot_date, playtime_minutes, last_played FROM play_history
                   WHERE game_id = ? AND platform = ? ORDER BY snapshot_date""",
                (game_id, platform),
            )
        return [(r["snapshot_date"], r["playtime_minutes"], r["last_played"]) for r in rows]

    async def _set_last_played(self, game_id: int, day: str | None) -> None:
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE game_platforms SET last_played = ? WHERE game_id = ? AND platform = 'ps5'",
                (day, game_id),
            )
            await db.commit()

    async def test_snapshot_records_current_last_played(self):
        game_id = await seed_game("Ghost of Tsushima")
        await add_platform(game_id, "ps5", playtime_minutes=46)
        await self._set_last_played(game_id, "2022-09-21")

        await db_module.record_play_history_snapshots("ps5", snapshot_date="2026-07-04")

        self.assertEqual(await self._rows(game_id, "ps5"), [("2026-07-04", 46, "2022-09-21")])

    async def test_earlier_snapshots_keep_their_own_value(self):
        # The whole point: an old snapshot must not inherit a newer date.
        game_id = await seed_game("Ghost of Tsushima")
        await add_platform(game_id, "ps5", playtime_minutes=46)
        await self._set_last_played(game_id, "2022-09-21")
        await db_module.record_play_history_snapshots("ps5", snapshot_date="2026-07-04")

        # A later sync picks up new play and advances the live column.
        await db_module.upsert_game_platform(game_id, "ps5", playtime_minutes=5100)
        await self._set_last_played(game_id, "2026-09-15")
        await db_module.record_play_history_snapshots("ps5", snapshot_date="2026-09-16")

        self.assertEqual(
            await self._rows(game_id, "ps5"),
            [("2026-07-04", 46, "2022-09-21"), ("2026-09-16", 5100, "2026-09-15")],
        )

    async def test_null_last_played_is_recorded_as_null(self):
        game_id = await seed_game("Some GOG Game")
        await add_platform(game_id, "gog", playtime_minutes=120)

        await db_module.record_play_history_snapshots("gog", snapshot_date="2026-07-04")

        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT last_played FROM play_history WHERE game_id = ?", (game_id,)
            )
        self.assertIsNone(row["last_played"])

    async def test_same_day_resync_refreshes_last_played_too(self):
        game_id = await seed_game("Blue Prince")
        await add_platform(game_id, "ps5", playtime_minutes=120)
        await self._set_last_played(game_id, "2026-07-01")
        await db_module.record_play_history_snapshots("ps5", snapshot_date="2026-07-04")

        await db_module.upsert_game_platform(game_id, "ps5", playtime_minutes=180)
        await self._set_last_played(game_id, "2026-07-04")
        await db_module.record_play_history_snapshots("ps5", snapshot_date="2026-07-04")

        self.assertEqual(await self._rows(game_id, "ps5"), [("2026-07-04", 180, "2026-07-04")])
