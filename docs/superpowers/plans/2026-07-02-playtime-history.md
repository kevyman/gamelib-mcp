# Generalized Playtime History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Delegation guidance (Sonnet 5 executor):** delegate to Haiku the migration-test scaffolding, `EXPECTED_TOOLS` bookkeeping, running suites, and doc edits. Keep for yourself: the snapshot-dedup SQL, the delta computation (baseline semantics are the one subtle thing in this plan), and the switch2 union.

**Goal:** Answer "what did I play this month/year?" — a `play_history` table snapshotting cumulative per-game playtime at each sync, plus a `get_play_history` tool computing per-window deltas, with switch2 served from the richer `nintendo_play_summary` daily data that already exists.

**Architecture:** `nintendo_play_summary` proved the per-day model but is Nintendo-specific and stores *daily minutes*. Steam/Epic/PSN report only *cumulative totals* per sync, so the generic table stores cumulative snapshots — at most one row per (game, platform, UTC day), written only when the total changed (dormant libraries add zero rows). One central hook after each platform sync (`record_play_history_snapshots(platform)`) keeps provider modules untouched. Reads compute window deltas as `latest_in_window − baseline_before_window`; switch2 deltas instead SUM real daily rows from `nintendo_play_summary`, which is strictly more accurate.

**Tech Stack:** Python 3.12, aiosqlite, FastMCP. No new dependencies or env vars.

## Global Constraints

- Schema version at plan time is **17**; this plan writes migration **v17→v18**. If another migration landed first, renumber to `SCHEMA_VERSION + 1` (check `gamelib_mcp/data/db/__init__.py:105`).
- Snapshots are **cumulative totals**, never deltas — deltas are derived at read time. Forward-only: history starts the day this ships (like Parental Controls; state that in the tool docstring).
- The snapshot hook must never fail a sync: wrap it so an exception logs a warning and the sync result is unaffected.
- A NULL platform playtime is *not* a snapshot of 0 — rows are only written for `playtime_minutes IS NOT NULL`.
- Test runner `.venv/bin/python -m pytest`; ruff + mypy gate each commit.

---

### Task 1: Migration v18 — `play_history` table

**Files:**
- Modify: `gamelib_mcp/data/db/schema.py` (append `_V18_SCHEMA_DDL`)
- Modify: `gamelib_mcp/data/db/__init__.py` (bump `SCHEMA_VERSION`, add step, extend `_MIGRATION_STEPS`, swap the three `_V17_SCHEMA_DDL` use sites — two in `_run_migrations`, one in `_rebuild_table_from_current_schema` — to `_V18_SCHEMA_DDL`)
- Test: `tests/test_db_migration.py`

**Interfaces:**
- Produces: `play_history(game_id, platform, snapshot_date, playtime_minutes)` with `PRIMARY KEY(game_id, platform, snapshot_date)`.

- [ ] **Step 1: Failing migration test** (existing per-version pattern in the file):

```python
async def test_v17_to_v18_adds_play_history(self):
    result = await migrate_db()
    self.assertEqual(result.final_version, 18)
    async with get_db() as db:
        cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(play_history)")}
        self.assertEqual(cols, {"game_id", "platform", "snapshot_date", "playtime_minutes"})
```

- [ ] **Step 2: Verify failure.** `.venv/bin/python -m pytest tests/test_db_migration.py -q -k v18`

- [ ] **Step 3: Implement.** `schema.py`:

```python
# v18 adds play_history: cumulative per-(game, platform) playtime snapshots,
# at most one row per UTC day, written after each platform sync only when the
# total changed. Deltas ("what did I play this month") are derived at read
# time; switch2 windows are served from nintendo_play_summary's real daily
# rows instead (see data/play_history.py). Forward-only, like
# nintendo_play_summary — there is no retroactive source to backfill from.
_V18_SCHEMA_DDL = _V17_SCHEMA_DDL + """
    CREATE TABLE IF NOT EXISTS play_history (
        game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        platform         TEXT NOT NULL,
        snapshot_date    TEXT NOT NULL,
        playtime_minutes INTEGER NOT NULL,
        PRIMARY KEY (game_id, platform, snapshot_date)
    );

    CREATE INDEX IF NOT EXISTS idx_play_history_date ON play_history(snapshot_date);
"""
```

`data/db/__init__.py`:

```python
async def _migrate_v17_to_v18(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add play_history (cumulative playtime snapshots; see schema.py note)."""
    if progress is not None:
        progress("Migrating to v18: add play_history.")
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS play_history (
            game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            platform         TEXT NOT NULL,
            snapshot_date    TEXT NOT NULL,
            playtime_minutes INTEGER NOT NULL,
            PRIMARY KEY (game_id, platform, snapshot_date)
        );

        CREATE INDEX IF NOT EXISTS idx_play_history_date ON play_history(snapshot_date);
        """
    )
    await _set_user_version(db, 18)
    await db.commit()
```

Plus `SCHEMA_VERSION = 18`, the `_MIGRATION_STEPS` entry `(17, _migrate_v17_to_v18),`, the import, and the three DDL-constant swaps.

- [ ] **Step 4: Run migration tests + full suite** — PASS.
- [ ] **Step 5: Commit** — `feat: v18 schema — play_history snapshots`.

### Task 2: Snapshot writer `record_play_history_snapshots`

**Files:**
- Create: `gamelib_mcp/data/db/history.py`
- Modify: `gamelib_mcp/data/db/__init__.py` (re-export, matching how other submodule functions are re-exported by the façade)
- Test: `tests/test_play_history.py`

**Interfaces:**
- Produces: `async record_play_history_snapshots(platform: str, snapshot_date: str | None = None) -> int` — returns rows written; `snapshot_date` (YYYY-MM-DD) defaults to today UTC, parameterized for tests.

- [ ] **Step 1: Failing tests** in a new `tests/test_play_history.py` (copy the temp-DB fixture setup from `tests/test_db_queries.py`):

```python
async def test_snapshot_written_for_changed_playtime(self):
    game_id = await upsert_game(None, "Hades")
    await upsert_game_platform(game_id, "steam", playtime_minutes=100, owned=1)
    n = await record_play_history_snapshots("steam", snapshot_date="2026-07-02")
    self.assertEqual(n, 1)

async def test_no_snapshot_when_unchanged(self):
    # snapshot once, then again same value on a later day -> 0 new rows
    ...
    await record_play_history_snapshots("steam", snapshot_date="2026-07-02")
    n = await record_play_history_snapshots("steam", snapshot_date="2026-07-03")
    self.assertEqual(n, 0)

async def test_same_day_resync_overwrites_todays_row(self):
    # 100 then 130 on the same date -> one row holding 130
    ...

async def test_null_playtime_not_snapshotted(self):
    game_id = await upsert_game(None, "Cyberpunk 2077")
    await upsert_game_platform(game_id, "gog", owned=1)   # NULL playtime
    n = await record_play_history_snapshots("gog", snapshot_date="2026-07-02")
    self.assertEqual(n, 0)
```

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement** — one set-based statement, no per-game Python loop:

```python
"""play_history writes: cumulative snapshots deduped against the latest row."""

from datetime import datetime, timezone

from . import get_db


async def record_play_history_snapshots(
    platform: str, snapshot_date: str | None = None
) -> int:
    """Snapshot current game_platforms playtimes for one platform.

    Inserts (or same-day-updates) a row per owned game whose current
    playtime_minutes differs from its most recent snapshot. Cheap enough to
    run after every sync: unchanged games match the NOT-different guard and
    produce no writes.
    """
    day = snapshot_date or datetime.now(timezone.utc).date().isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO play_history (game_id, platform, snapshot_date, playtime_minutes)
            SELECT gp.game_id, gp.platform, ?, gp.playtime_minutes
            FROM game_platforms gp
            WHERE gp.platform = ?
              AND gp.owned = 1
              AND gp.playtime_minutes IS NOT NULL
              AND gp.playtime_minutes IS NOT (
                  SELECT ph.playtime_minutes FROM play_history ph
                  WHERE ph.game_id = gp.game_id AND ph.platform = gp.platform
                  ORDER BY ph.snapshot_date DESC LIMIT 1
              )
            ON CONFLICT(game_id, platform, snapshot_date)
                DO UPDATE SET playtime_minutes = excluded.playtime_minutes
            """,
            (day, platform),
        )
        await db.commit()
        return cursor.rowcount
```

(`IS NOT` is SQLite's null-safe inequality — a first-ever snapshot, where the subquery yields NULL, still inserts. Re-export from `data/db/__init__.py`.)

- [ ] **Step 4: Tests pass; ruff + mypy pass.**
- [ ] **Step 5: Commit** — `feat: play_history snapshot writer`.

### Task 3: Hook snapshots into library sync

**Files:**
- Modify: `gamelib_mcp/tools/admin.py` (`run_library_sync` — after each platform's sync coroutine succeeds, snapshot that platform)
- Test: `tests/test_tools_admin.py`

**Interfaces:**
- Consumes: `record_play_history_snapshots` (Task 2).
- Produces: every successful `refresh_library` (startup, periodic, manual, single-platform) leaves up-to-date snapshots. Sync result dicts gain `"play_history_rows": int` per platform (0 when nothing changed).

- [ ] **Step 1: Failing test** in `tests/test_tools_admin.py`, using its established patch pattern (`patch("gamelib_mcp.tools.admin.sync_epic", ...)` etc.): run `refresh_library(platform="epic")` with a mock sync that upserts one game with playtime, then assert a `play_history` row exists and the result carries `play_history_rows`.

- [ ] **Step 2: Verify failure.**

- [ ] **Step 3: Implement.** Read `run_library_sync` (`tools/admin.py:53`) first: it builds its platform→fn dict via `resolve_platform_functions("sync", ...)`. After each platform sync completes without raising, add:

```python
try:
    history_rows = await record_play_history_snapshots(platform_name)
except Exception:
    logger.warning("play_history snapshot failed for %s", platform_name, exc_info=True)
    history_rows = None
if isinstance(result, dict) and history_rows is not None:
    result["play_history_rows"] = history_rows
```

The steam path runs through `bulk_upsert_steam_library` inside its sync fn — the hook still goes in `run_library_sync` after the fn returns, uniform across platforms (including switch2: harmless duplicate signal alongside `nintendo_play_summary`, and it covers any future platform for free).

- [ ] **Step 4: Full suite** — PASS (watch `tests/test_models_sync.py`/sync-shape assertions; extend expected keys if they enumerate result fields).
- [ ] **Step 5: Commit** — `feat: snapshot play history after each platform sync`.

### Task 4: `get_play_history` tool

**Files:**
- Create: `gamelib_mcp/tools/history.py`
- Modify: `gamelib_mcp/main.py` (passthrough, `@mcp.tool(annotations=READ_ONLY_TOOL)`)
- Modify: `gamelib_mcp/tools/models.py` (`PlayHistoryEntry`, `PlayHistoryResponse`)
- Test: `tests/test_tools_history.py`; `tests/test_tool_registration.py` (add tool, bump count 32→33)

**Interfaces:**
- Consumes: `play_history`, `nintendo_play_summary`, `game_platform_identifiers` (`nintendo_title_id` — constant lives in `data/nintendo.py:37`), `validate_platform`/`clamp_limit` from `tools/common.py`.
- Produces:

```python
async def get_play_history(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    platform: str | None = None,
    limit: int = 20,
) -> dict:
    """
    What you actually played in a time window, per game, most-played first.

    Defaults to the last `days` days; or pass explicit ISO start_date/end_date
    (inclusive). Non-Nintendo platforms are computed from cumulative sync
    snapshots, so granularity is per-sync-day and history only exists from the
    day this feature was deployed. switch2 uses real per-day Parental Controls
    data (nintendo_play_summary). Returns per-game minutes, per-platform
    totals, and the window used.
    """
```

Behavior contract:
1. Window: `end = end_date or today-UTC`, `start = start_date or end − days`; validate ISO format and `start <= end` with `ToolError`.
2. Generic delta per (game, platform) from `play_history`, computed in one query:

```sql
WITH bounds AS (
    SELECT game_id, platform,
           -- last cumulative value at or before window end
           (SELECT ph2.playtime_minutes FROM play_history ph2
            WHERE ph2.game_id = ph.game_id AND ph2.platform = ph.platform
              AND ph2.snapshot_date <= :end
            ORDER BY ph2.snapshot_date DESC LIMIT 1) AS end_total,
           -- baseline: last value strictly before window start; if none,
           -- the first value inside the window (delta then starts there)
           COALESCE(
               (SELECT ph3.playtime_minutes FROM play_history ph3
                WHERE ph3.game_id = ph.game_id AND ph3.platform = ph.platform
                  AND ph3.snapshot_date < :start
                ORDER BY ph3.snapshot_date DESC LIMIT 1),
               (SELECT ph4.playtime_minutes FROM play_history ph4
                WHERE ph4.game_id = ph.game_id AND ph4.platform = ph.platform
                  AND ph4.snapshot_date >= :start AND ph4.snapshot_date <= :end
                ORDER BY ph4.snapshot_date ASC LIMIT 1)
           ) AS start_total
    FROM play_history ph
    WHERE ph.snapshot_date >= :start AND ph.snapshot_date <= :end
      AND ph.platform != 'switch2'
    GROUP BY ph.game_id, ph.platform
)
SELECT b.game_id, b.platform, g.name,
       MAX(0, b.end_total - b.start_total) AS minutes_played
FROM bounds b JOIN games g ON g.id = b.game_id
WHERE b.end_total - b.start_total > 0
```

Document the baseline caveat in the tool docstring: a game's *first-ever* snapshot inside the window contributes only growth *after* that snapshot (its prior total is unattributable), and `MAX(0, …)` absorbs upstream total corrections.
3. switch2 rows from real daily data (`period_type = 'day'`, `period_key` between start/end), joined to games via the `nintendo_title_id` identifier:

```sql
SELECT gp.game_id, 'switch2' AS platform, g.name,
       SUM(nps.playtime_minutes) AS minutes_played
FROM nintendo_play_summary nps
JOIN game_platform_identifiers gpi
  ON gpi.identifier_type = 'nintendo_title_id'
 AND gpi.identifier_value = nps.application_id
JOIN game_platforms gp ON gp.id = gpi.game_platform_id
JOIN games g ON g.id = gp.game_id
WHERE nps.period_type = 'day'
  AND nps.period_key >= :start AND nps.period_key <= :end
GROUP BY gp.game_id
HAVING minutes_played > 0
```

(Unmatched `application_id`s — played but not in the library join — are summed into a `"switch2_unmatched_minutes"` field rather than dropped silently.)
4. Merge, filter by resolved `platform` if given (skipping whichever query doesn't apply), sort by `minutes_played` desc, clamp with `clamp_limit(limit)`.
5. Response: `{"window": {"start": ..., "end": ...}, "total_minutes": ..., "total_hours": ..., "by_platform": {platform: minutes}, "games": [{game_id, name, platform, minutes_played, hours_played}], "switch2_unmatched_minutes": ...}`.

- [ ] **Step 1: Failing tests**: seeded snapshots across dates produce correct deltas (including baseline-before-window and first-snapshot-in-window cases); switch2 daily rows aggregate; platform filter; empty window returns zeros; bad dates raise ToolError.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** per contract; models; passthrough; registration updates.
- [ ] **Step 4: Full suite + ruff + mypy** — PASS.
- [ ] **Step 5: Commit** — `feat: get_play_history tool`.

### Task 5: Docs

- [ ] `CLAUDE.md`: `play_history` in the DB table list (with the cumulative-snapshot/forward-only note), tool entry, and a Key Design Patterns bullet. Haiku-delegable.
- [ ] Full suite + ruff + mypy; commit — `docs: playtime history`.

## Explicit non-goals (YAGNI)

- No backfill from Steam's `playtime_2weeks_minutes` (a 2-week rolling total can't be decomposed into days; snapshots start clean).
- No year-in-review formatting — that's a prompt over `get_play_history(days=365)`, not code.
- No pruning/compaction; the changed-rows-only guard keeps growth tiny (revisit only if the table ever matters).
