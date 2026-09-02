"""A fresh install and a migrated install must end up with the same schema.

``_run_migrations`` has two paths. A database with no ``games`` table is
"fresh": it gets ``_V39_SCHEMA_DDL`` executed straight over it and is stamped
at ``SCHEMA_VERSION``, never touching the 38 chained ``_migrate_vN_to_vN+1``
steps. Production came up the other way, one step at a time. Nothing until now
compared the results, so a column added to the DDL but not to a migration step
(or the reverse) would ship silently and only surface as a missing-column error
on the deployed database — the one path no test exercises.

What is compared, per object:
  * the set of (type, name) in ``sqlite_master`` (excluding SQLite internals),
  * per table, the SET of (column, declared type, notnull, pk, default),
  * per table, the set of implicit index signatures (origin, unique, columns)
    so a UNIQUE/PK constraint present on only one path is caught even though
    its auto-index is named ``sqlite_autoindex_*``,
  * per named index, the ordered list of column names,
  * per trigger and view, its whitespace-normalized SQL.

Column ORDER within a table is deliberately excluded: ``ALTER TABLE ADD
COLUMN`` appends, so the migrated database carries columns in the order they
were introduced while a fresh one carries the DDL's order. Five tables differ
that way today (games, game_platforms, game_assessments,
game_platform_enrichment, steam_platform_data) and it is harmless — SQLite
addresses columns by name, and every query in this codebase names them.
"""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.db import schema as schema_module


def _reset_db_module_state() -> None:
    db_module._DB_READY_PATH = None
    db_module._FTS_READY_PATH = None


async def _build(path: Path, *, from_v1: bool) -> None:
    """Materialize a database at ``path``, fresh or via the migration chain."""
    if from_v1:
        conn = sqlite3.connect(path)
        try:
            conn.executescript(schema_module._V1_SCHEMA_DDL)
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        finally:
            conn.close()

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"file:{path}"
    _reset_db_module_state()
    try:
        await db_module.init_db()
    finally:
        _reset_db_module_state()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


class _Snapshot:
    """Order-insensitive structural fingerprint of one SQLite file."""

    def __init__(self, path: Path, label: str) -> None:
        self.label = label
        conn = sqlite3.connect(path)
        try:
            self.user_version = conn.execute("PRAGMA user_version").fetchone()[0]
            rows = conn.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            self.objects = {(kind, name) for kind, name, _ in rows}
            self.columns: dict[str, set[tuple]] = {}
            self.column_order: dict[str, list[str]] = {}
            self.constraint_indexes: dict[str, set[tuple]] = {}
            self.index_columns: dict[str, tuple] = {}
            self.object_sql: dict[str, str] = {}
            for kind, name, sql in rows:
                if kind == "table":
                    info = list(conn.execute(f'PRAGMA table_info("{name}")'))
                    self.columns[name] = {
                        (row[1], (row[2] or "").lower(), row[3], row[5], row[4])
                        for row in info
                    }
                    self.column_order[name] = [row[1] for row in info]
                    self.constraint_indexes[name] = {
                        (
                            entry[3],  # origin: c(reate index) / u(nique) / pk
                            entry[2],  # unique flag
                            self._index_columns(conn, entry[1]),
                        )
                        for entry in conn.execute(f'PRAGMA index_list("{name}")')
                    }
                elif kind == "index":
                    self.index_columns[name] = self._index_columns(conn, name)
                elif kind in ("trigger", "view"):
                    self.object_sql[name] = " ".join((sql or "").split())
        finally:
            conn.close()

    @staticmethod
    def _index_columns(conn: sqlite3.Connection, index_name: str) -> tuple:
        # row[2] is the column name, or None for an expression component.
        return tuple(
            row[2] for row in conn.execute(f'PRAGMA index_info("{index_name}")')
        )


class SchemaParityTests(unittest.IsolatedAsyncioTestCase):
    """The fresh-install DDL and the 38-step chain must converge."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        root = Path(cls._tmpdir.name)
        fresh_path = root / "fresh.sqlite"
        chained_path = root / "chained.sqlite"
        # asyncio.run per build gives each its own loop, so the aiosqlite
        # worker thread behind the migration is joined before the next starts.
        asyncio.run(_build(fresh_path, from_v1=False))
        asyncio.run(_build(chained_path, from_v1=True))
        cls.fresh = _Snapshot(fresh_path, "fresh")
        cls.chained = _Snapshot(chained_path, "chained-from-v1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def _report(self, only_fresh, only_chained, what: str) -> str:
        return (
            f"{what} differs between a fresh install and the migration chain.\n"
            f"  only in fresh:   {sorted(only_fresh, key=repr)}\n"
            f"  only in chained: {sorted(only_chained, key=repr)}"
        )

    def test_both_paths_land_on_the_current_schema_version(self):
        self.assertEqual(self.fresh.user_version, db_module.SCHEMA_VERSION)
        self.assertEqual(self.chained.user_version, db_module.SCHEMA_VERSION)

    def test_same_tables_indexes_views_and_triggers_exist(self):
        # Guard against a vacuous pass: an empty fingerprint would match itself.
        self.assertGreater(len(self.fresh.objects), 20)
        self.assertEqual(
            self.fresh.objects,
            self.chained.objects,
            self._report(
                self.fresh.objects - self.chained.objects,
                self.chained.objects - self.fresh.objects,
                "The set of schema objects",
            ),
        )

    def test_every_table_has_the_same_columns(self):
        for table in sorted(set(self.fresh.columns) & set(self.chained.columns)):
            with self.subTest(table=table):
                fresh_cols = self.fresh.columns[table]
                chained_cols = self.chained.columns[table]
                self.assertEqual(
                    fresh_cols,
                    chained_cols,
                    self._report(
                        fresh_cols - chained_cols,
                        chained_cols - fresh_cols,
                        f"Table {table!r} columns (name, type, notnull, pk, default)",
                    ),
                )

    def test_every_table_has_the_same_implicit_constraints(self):
        # UNIQUE/PRIMARY KEY constraints materialize as sqlite_autoindex_* —
        # excluded from the object-name comparison above, so compare their
        # shapes (origin, uniqueness, columns) instead of their generated names.
        for table in sorted(
            set(self.fresh.constraint_indexes) & set(self.chained.constraint_indexes)
        ):
            with self.subTest(table=table):
                fresh_idx = self.fresh.constraint_indexes[table]
                chained_idx = self.chained.constraint_indexes[table]
                self.assertEqual(
                    fresh_idx,
                    chained_idx,
                    self._report(
                        fresh_idx - chained_idx,
                        chained_idx - fresh_idx,
                        f"Table {table!r} index signatures (origin, unique, columns)",
                    ),
                )

    def test_every_named_index_covers_the_same_columns_in_order(self):
        for index in sorted(
            set(self.fresh.index_columns) & set(self.chained.index_columns)
        ):
            with self.subTest(index=index):
                self.assertEqual(
                    self.fresh.index_columns[index],
                    self.chained.index_columns[index],
                    f"Index {index!r} column order differs: "
                    f"fresh={self.fresh.index_columns[index]} "
                    f"chained={self.chained.index_columns[index]}",
                )

    def test_every_view_and_trigger_has_the_same_definition(self):
        for name in sorted(set(self.fresh.object_sql) & set(self.chained.object_sql)):
            with self.subTest(object=name):
                self.assertEqual(
                    self.fresh.object_sql[name],
                    self.chained.object_sql[name],
                    f"Definition of {name!r} differs:\n"
                    f"  fresh:   {self.fresh.object_sql[name]}\n"
                    f"  chained: {self.chained.object_sql[name]}",
                )

    def test_column_order_may_differ_but_never_the_column_set(self):
        # The one tolerated divergence, asserted rather than assumed: ALTER
        # TABLE ADD COLUMN appends, so the migrated database lists columns in
        # the order they were introduced. Five tables differ that way today.
        # Whatever the order, the same names must be present on both paths.
        for table in sorted(set(self.fresh.column_order) & set(self.chained.column_order)):
            with self.subTest(table=table):
                self.assertEqual(
                    sorted(self.fresh.column_order[table]),
                    sorted(self.chained.column_order[table]),
                )


if __name__ == "__main__":
    unittest.main()
