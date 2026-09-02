"""Every seedable schema version must migrate to current WITHOUT losing rows.

``_MIGRATION_STEPS`` registers 38 transitions. ``tests/test_db_migration.py``
covers the ones with interesting data semantics, and the rest are exercised
only as "the chain ran and did not raise" — from v1, so a step that drops and
recreates a table in the middle of the chain would still pass while silently
discarding whatever the previous steps had carried.

This walks in from every version that has a seedable snapshot of its own DDL
(``_V{N}_SCHEMA_DDL`` in ``gamelib_mcp.data.db.schema``, enumerated by
introspection so a newly added constant is picked up without editing this
file), plants one ``games`` row, runs the real ``init_db()``, and asserts the
database lands on ``SCHEMA_VERSION`` with the row intact.

Versions without a DDL constant stay chain-only: they are reached by running
the steps before them, and are covered here transitively by every earlier
seed point. NOT NULL columns are derived from the seeded snapshot at runtime
rather than hard-coded, so a future version that adds one does not turn this
into an insert error.
"""

import os
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import schema as schema_module

_PROBE_NAME = "Step Probe"
_DDL_CONSTANT = re.compile(r"^_V(\d+)_SCHEMA_DDL$")


def seedable_versions() -> list[int]:
    """Schema versions with a checked-in DDL snapshot, ascending."""
    found = []
    for attribute in dir(schema_module):
        match = _DDL_CONSTANT.match(attribute)
        if match and isinstance(getattr(schema_module, attribute), str):
            found.append(int(match.group(1)))
    return sorted(found)


def _required_games_columns(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """NOT NULL, non-default, non-PK columns of this version's ``games``."""
    required = []
    for _cid, name, declared_type, notnull, default, pk in conn.execute(
        "PRAGMA table_info(games)"
    ):
        if pk or name == "name" or not notnull or default is not None:
            continue
        required.append((name, (declared_type or "").upper()))
    return required


def _seed_version(path: Path, version: int) -> None:
    """Create a database at ``version`` holding one recognizable games row."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(getattr(schema_module, f"_V{version}_SCHEMA_DDL"))
        conn.execute(f"PRAGMA user_version = {version}")
        required = _required_games_columns(conn)
        columns = ["name", *(name for name, _ in required)]
        values = [
            _PROBE_NAME,
            *(0 if kind in ("INTEGER", "REAL") else "x" for _, kind in required),
        ]
        placeholders = ", ".join("?" * len(columns))
        conn.execute(
            f"INSERT INTO games ({', '.join(columns)}) VALUES ({placeholders})", values
        )
        conn.commit()
    finally:
        conn.close()


def _inspect(path: Path) -> tuple[int, int]:
    """Return (user_version, count of the probe row) after migrating."""
    conn = sqlite3.connect(path)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        surviving = conn.execute(
            "SELECT COUNT(*) FROM games WHERE name = ?", (_PROBE_NAME,)
        ).fetchone()[0]
        return version, surviving
    finally:
        conn.close()


class SeedableVersionSetTests(unittest.TestCase):
    def test_the_endpoints_of_the_chain_are_both_seedable(self):
        versions = seedable_versions()
        self.assertIn(1, versions, "v1 is the entry point of the migration chain")
        self.assertIn(
            db_module.SCHEMA_VERSION,
            versions,
            "the current schema must have a DDL snapshot — _run_migrations "
            "applies it directly for a fresh install",
        )

    def test_no_ddl_constant_claims_a_version_beyond_the_current_schema(self):
        # A stray _V40_SCHEMA_DDL would mean the DDL moved ahead of
        # SCHEMA_VERSION and fresh installs are being stamped a version behind.
        self.assertLessEqual(max(seedable_versions()), db_module.SCHEMA_VERSION)


class MigrationStepDataPreservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._previous_url = os.environ.get("DATABASE_URL")

    async def asyncTearDown(self) -> None:
        db_module._DB_READY_PATH = None
        db_module._FTS_READY_PATH = None
        if self._previous_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._previous_url
        self._tmpdir.cleanup()

    async def _migrate_from(self, version: int) -> tuple[int, int]:
        path = Path(self._tmpdir.name) / f"from-v{version}.sqlite"
        _seed_version(path, version)
        os.environ["DATABASE_URL"] = f"file:{path}"
        db_module._DB_READY_PATH = None
        db_module._FTS_READY_PATH = None
        try:
            await db_module.init_db()
        finally:
            db_module._DB_READY_PATH = None
            db_module._FTS_READY_PATH = None
        return _inspect(path)

    async def test_every_seedable_version_migrates_to_current_with_its_row(self):
        versions = seedable_versions()
        self.assertGreater(len(versions), 10, "introspection found almost nothing")
        for version in versions:
            with self.subTest(version=version):
                final_version, surviving = await self._migrate_from(version)
                self.assertEqual(
                    final_version,
                    db_module.SCHEMA_VERSION,
                    f"migrating from v{version} stopped at v{final_version}",
                )
                self.assertEqual(
                    surviving,
                    1,
                    f"the games row seeded at v{version} did not survive the "
                    "migration chain",
                )

    async def test_a_seeded_row_keeps_its_identity_columns(self):
        # The row-count assertion above would still pass if a step rebuilt the
        # table and re-minted rows; this pins that the SAME row comes out, with
        # the columns every later version relies on.
        path = Path(self._tmpdir.name) / "identity.sqlite"
        _seed_version(path, 1)
        os.environ["DATABASE_URL"] = f"file:{path}"
        db_module._DB_READY_PATH = None
        db_module._FTS_READY_PATH = None
        try:
            await db_module.init_db()
        finally:
            db_module._DB_READY_PATH = None
            db_module._FTS_READY_PATH = None
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT id, name, content_type, is_primary_library_item "
                "FROM games WHERE name = ?",
                (_PROBE_NAME,),
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], _PROBE_NAME)
        # v13 introduced DLC classification; a pre-v13 row must be defaulted
        # into the primary-library-item bucket, not left NULL where the
        # is_primary rollups would silently drop it.
        self.assertEqual(row[2], "base_game")
        self.assertEqual(row[3], 1)


if __name__ == "__main__":
    unittest.main()
