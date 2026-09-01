#!/usr/bin/env python
"""Restore drill: prove a backup restores, migrates and holds data — off to the side.

Backups that have never been restored are Schrödinger's backups (audit
2026-07-06 §5, still open on 2026-09-01). This script takes a backup file (the
nightly ``gamelib-nightly.bak`` from ``deploy.md`` → "Database backups", the
off-machine copy, or a pre-migration ``gamelib.db.pre-v{N}.bak``), copies it
into a scratch directory, and walks the same path a fresh container would:

1. ``PRAGMA integrity_check`` and ``foreign_key_check`` on the copy;
2. the schema version it carries;
3. ``migrate_db()`` — the app's own startup migration — so an older backup is
   proven to migrate forward, not merely to open;
4. row counts for the tables that hold data nothing can re-sync
   (``nintendo_play_summary``, ``ratings``, ``play_history``,
   ``game_assessments``, manual overrides live on ``games``/``game_platforms``).

It never opens the production database and never writes outside the scratch
directory. Exit status 0 means the drill passed; the JSON report says why
otherwise.

Usage::

    .venv/bin/python scripts/restore_drill.py /path/to/gamelib-nightly.bak
    .venv/bin/python scripts/restore_drill.py backup.bak --keep /tmp/drill --json

``--keep DIR`` leaves the restored copy in DIR for manual inspection; without
it the copy is deleted when the drill ends.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Tables whose rows cannot be re-synced from a provider, listed first so a
# glance at the report answers "is the irreplaceable data there?".
IRREPLACEABLE_TABLES = (
    "nintendo_play_summary",
    "ratings",
    "play_history",
    "game_assessments",
    "game_wishlist",
)
OTHER_TABLES = (
    "games",
    "game_platforms",
    "game_platform_identifiers",
    "game_prices",
    "tag_affinity",
    "scrape_config",
)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    present = _table_names(conn)
    counts: dict[str, int] = {}
    for table in (*IRREPLACEABLE_TABLES, *OTHER_TABLES):
        if table in present:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return counts


async def _migrate(copy: Path) -> dict[str, Any]:
    """Run the app's own migration against the scratch copy, then undo the env."""
    from gamelib_mcp.data import db as db_module

    prev = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"file:{copy}"
    # migrate_db caches "this path is ready" in module globals; clear them on the
    # way in and out so neither a prior caller nor a later one sees the drill.
    for name in ("_DB_READY_PATH", "_FTS_READY_PATH"):
        if hasattr(db_module, name):
            setattr(db_module, name, None)
    try:
        result = await db_module.migrate_db()
    finally:
        for name in ("_DB_READY_PATH", "_FTS_READY_PATH"):
            if hasattr(db_module, name):
                setattr(db_module, name, None)
        if prev is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = prev
    return {
        "schema_version": db_module.SCHEMA_VERSION,
        "migrated": bool(getattr(result, "migrated", False)),
        "from_version": getattr(result, "from_version", None),
    }


async def run_drill(backup: Path, keep_dir: Path | None = None) -> dict[str, Any]:
    """Restore ``backup`` into a scratch copy and verify it end to end.

    Returns the report dict; ``report["passed"]`` is the verdict and
    ``report["checks"]`` lists every check with its outcome. Raises nothing for
    a failed check — callers read the report — but propagates I/O errors for a
    missing or unreadable backup.
    """
    started = time.monotonic()
    backup = backup.expanduser().resolve()
    if not backup.is_file():
        raise FileNotFoundError(f"backup not found: {backup}")

    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    tmp_holder: tempfile.TemporaryDirectory[str] | None = None
    if keep_dir is None:
        tmp_holder = tempfile.TemporaryDirectory(prefix="gamelib-restore-drill-")
        dest_dir = Path(tmp_holder.name)
    else:
        dest_dir = keep_dir.expanduser().resolve()
        dest_dir.mkdir(parents=True, exist_ok=True)
    copy = dest_dir / "restored.sqlite"

    try:
        shutil.copyfile(backup, copy)
        # A .backup/VACUUM INTO file is a single self-contained database; a raw
        # copy of a live WAL-mode DB would need its -wal/-shm siblings too, and
        # that is exactly the mistake the drill exists to surface.
        for sibling in (backup.with_name(backup.name + "-wal"), backup.with_name(backup.name + "-shm")):
            if sibling.exists():
                check("no stray wal/shm beside backup", False, str(sibling))

        conn = sqlite3.connect(copy)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            check("integrity_check", integrity == "ok", integrity)
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
            check("foreign_key_check", not fk, len(fk))
            version_before = conn.execute("PRAGMA user_version").fetchone()[0]
            counts_before = _counts(conn)
        finally:
            conn.close()
        check("has games rows", counts_before.get("games", 0) > 0, counts_before.get("games", 0))

        migration = await _migrate(copy)
        conn = sqlite3.connect(copy)
        try:
            version_after = conn.execute("PRAGMA user_version").fetchone()[0]
            counts_after = _counts(conn)
            integrity_after = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        check(
            "migrated to current schema",
            version_after == migration["schema_version"],
            {"before": version_before, "after": version_after, "current": migration["schema_version"]},
        )
        check("integrity_check after migration", integrity_after == "ok", integrity_after)
        lost = {
            table: (counts_before[table], counts_after.get(table))
            for table in counts_before
            if counts_after.get(table, 0) < counts_before[table]
        }
        check("no table lost rows in migration", not lost, lost or None)

        passed = all(c["ok"] for c in checks)
        report: dict[str, Any] = {
            "backup": str(backup),
            "backup_bytes": backup.stat().st_size,
            "restored_to": str(copy) if keep_dir is not None else None,
            "user_version_before": version_before,
            "user_version_after": version_after,
            "schema_version": migration["schema_version"],
            "counts": counts_after,
            "checks": checks,
            "passed": passed,
            "elapsed_s": round(time.monotonic() - started, 2),
        }
        return report
    finally:
        if tmp_holder is not None:
            tmp_holder.cleanup()


def _print_summary(report: dict[str, Any]) -> None:
    verdict = "PASS" if report["passed"] else "FAIL"
    print(f"restore drill: {verdict}  ({report['elapsed_s']}s)")
    print(f"  backup: {report['backup']} ({report['backup_bytes']:,} bytes)")
    print(
        f"  schema: v{report['user_version_before']} -> v{report['user_version_after']}"
        f" (current v{report['schema_version']})"
    )
    print("  rows:")
    for table, n in report["counts"].items():
        tag = "  irreplaceable" if table in IRREPLACEABLE_TABLES else ""
        print(f"    {table:28} {n:>8}{tag}")
    for c in report["checks"]:
        if not c["ok"]:
            print(f"  FAILED: {c['check']}: {c['detail']}")
    if report.get("restored_to"):
        print(f"  restored copy kept at {report['restored_to']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("backup", type=Path, help="backup file (.bak from sqlite3 .backup / VACUUM INTO)")
    parser.add_argument("--keep", type=Path, default=None, metavar="DIR", help="keep the restored copy in DIR")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON instead of the summary")
    args = parser.parse_args(argv)

    report = asyncio.run(run_drill(args.backup, args.keep))
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_summary(report)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
