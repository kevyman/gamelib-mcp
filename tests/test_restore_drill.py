"""scripts/restore_drill.py restores a backup into a scratch copy and verifies it.

Runs the drill against a backup taken from a migrated, seeded test database:
the copy must integrity-check, migrate to SCHEMA_VERSION, keep every row, and
leave both the backup and the caller's DATABASE_URL untouched. A backup with
no games rows must fail the drill rather than pass vacuously.
"""

import asyncio
import importlib.util
import os
import sqlite3
import tempfile
from pathlib import Path

from conftest import DEADLOCK_TIMEOUT, ToolDBTestCase, seed_game

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import schema as schema_module

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "restore_drill.py"
_spec = importlib.util.spec_from_file_location("restore_drill", _SCRIPT)
assert _spec is not None and _spec.loader is not None
restore_drill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(restore_drill)


def _backup_of(db_path: str, dest: Path) -> Path:
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


class RestoreDrillTests(ToolDBTestCase):
    async def test_seeded_backup_passes_and_nothing_else_is_touched(self):
        await seed_game("Drill Probe", tags=["roguelike"])
        with tempfile.TemporaryDirectory() as tmp:
            backup = _backup_of(self._db_path, Path(tmp) / "nightly.bak")
            before_bytes = backup.read_bytes()
            prev_url = os.environ.get("DATABASE_URL")

            report = await asyncio.wait_for(
                restore_drill.run_drill(backup, keep_dir=Path(tmp) / "kept"), DEADLOCK_TIMEOUT
            )

            self.assertTrue(report["passed"], report["checks"])
            self.assertEqual(report["user_version_after"], report["schema_version"])
            self.assertGreaterEqual(report["counts"]["games"], 1)
            self.assertIn("nintendo_play_summary", report["counts"])
            self.assertEqual(backup.read_bytes(), before_bytes, "the drill must not modify the backup")
            self.assertEqual(os.environ.get("DATABASE_URL"), prev_url)
            self.assertTrue(Path(report["restored_to"]).exists())
            failed = [c for c in report["checks"] if not c["ok"]]
            self.assertEqual(failed, [])

    async def test_empty_backup_fails_the_drill(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = _backup_of(self._db_path, Path(tmp) / "empty.bak")
            report = await asyncio.wait_for(restore_drill.run_drill(backup), DEADLOCK_TIMEOUT)
            self.assertFalse(report["passed"])
            names = {c["check"] for c in report["checks"] if not c["ok"]}
            self.assertIn("has games rows", names)
            self.assertIsNone(report["restored_to"])

    async def test_missing_backup_raises(self):
        with self.assertRaises(FileNotFoundError):
            await restore_drill.run_drill(Path("/nonexistent/gamelib.bak"))

    async def test_old_backup_is_migrated_forward(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.bak"
            conn = sqlite3.connect(old)
            conn.executescript(schema_module._V25_SCHEMA_DDL)
            conn.execute("PRAGMA user_version = 25")
            conn.execute("INSERT INTO games (name) VALUES ('Old Probe')")
            conn.commit()
            conn.close()

            report = await asyncio.wait_for(restore_drill.run_drill(old), DEADLOCK_TIMEOUT)

            self.assertTrue(report["passed"], report["checks"])
            self.assertEqual(report["user_version_before"], 25)
            self.assertEqual(report["user_version_after"], db_module.SCHEMA_VERSION)
            self.assertGreater(len(report["applied_steps"]), 0)
            self.assertEqual(report["counts"]["games"], 1)

    async def test_corrupt_backup_fails_with_a_report_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "bogus.bak"
            bogus.write_bytes(b"this is not a sqlite file" * 40)
            report = await asyncio.wait_for(restore_drill.run_drill(bogus), DEADLOCK_TIMEOUT)
            self.assertFalse(report["passed"])
            failed = {c["check"] for c in report["checks"] if not c["ok"]}
            self.assertIn("readable sqlite database", failed)
            self.assertIn("migration ran", failed)

    async def test_stale_wal_from_a_previous_keep_run_is_discarded(self):
        await seed_game("Drill Probe", tags=["roguelike"])
        with tempfile.TemporaryDirectory() as tmp:
            backup = _backup_of(self._db_path, Path(tmp) / "nightly.bak")
            keep = Path(tmp) / "kept"
            keep.mkdir()
            # Simulate an interrupted earlier run: a leftover WAL beside the copy.
            (keep / "restored.sqlite").write_bytes(b"garbage")
            (keep / "restored.sqlite-wal").write_bytes(b"garbage")
            report = await asyncio.wait_for(restore_drill.run_drill(backup, keep_dir=keep), DEADLOCK_TIMEOUT)
            self.assertTrue(report["passed"], report["checks"])
            self.assertFalse((keep / "restored.sqlite-wal").exists() and (keep / "restored.sqlite-wal").read_bytes() == b"garbage")
