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
