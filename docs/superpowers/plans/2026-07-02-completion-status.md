# Completion Status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Delegation guidance (Sonnet 5 executor):** delegate to Haiku the mechanical work — test-file scaffolding copied from neighbors, `EXPECTED_TOOLS` bookkeeping, model-field additions, running suites, doc edits. Keep for yourself: the `PLAY_STATE_SQL` change (it feeds three rollup CTEs — subtle), the suggestion heuristic SQL, and the migration.

**Goal:** A user-set per-game completion status (`playing` / `completed` / `abandoned`), with an HLTB-vs-playtime heuristic that *suggests* candidates, making `get_backlog_stats` honest and stopping discovery from recommending abandoned games.

**Architecture:** One nullable TEXT column `games.completion_status` (NULL = unset; playtime inference remains the fallback). Set through the existing `update_game` tool (it already owns manual per-game edits and the `manual_overrides` machinery). A new read-only `suggest_completion_status` tool ranks candidates: `total playtime ≥ hltb_main` ⇒ probably `completed`; started-but-dormant ⇒ probably `abandoned`. The shared `PLAY_STATE_SQL` CASE learns one new branch — an explicit `completed` counts as `played` even when playtime is unknown — and discovery/backlog queries exclude `completed`/`abandoned`.

**Tech Stack:** Python 3.12, aiosqlite, FastMCP. No new dependencies, no new env vars.

## Global Constraints

- Schema version at plan time is **17**; this plan writes migration **v17→v18**. If another migration landed first, renumber everything to `SCHEMA_VERSION + 1` (check `gamelib_mcp/data/db/__init__.py:105`).
  - **Actual execution note:** by the time this plan was implemented, `SCHEMA_VERSION` was already 20 (wishlist deals + cross-platform IGDB re-claim migrations landed first), and the tool count was already 33. The migration in this branch is **v20→v21** (`_V20_SCHEMA_DDL`, `_migrate_v20_to_v21`), and the tool-count test bumped **33→34**. Everything else in this plan applies unchanged.
- Status vocabulary is exactly `{"playing", "completed", "abandoned"}` plus the sentinel `"none"` (accepted by `update_game` to reset to NULL). Reject anything else with a `ToolError` listing valid values.
- Suggestions never write — `suggest_completion_status` is strictly read-only; the human (via the AI) confirms each one through `update_game`.
- Test runner `.venv/bin/python -m pytest`; ruff + mypy must pass before each commit.

---

### Task 1: Migration v18 — `games.completion_status`

**Files:**
- Modify: `gamelib_mcp/data/db/schema.py` (append `_V18_SCHEMA_DDL`)
- Modify: `gamelib_mcp/data/db/__init__.py` (bump `SCHEMA_VERSION` to 18, add `_migrate_v17_to_v18`, append to `_MIGRATION_STEPS`, switch the three `_V17_SCHEMA_DDL` use sites — fresh-init and final-reconciliation in `_run_migrations`, plus `_rebuild_table_from_current_schema` — to `_V18_SCHEMA_DDL`)
- Test: `tests/test_db_migration.py`

**Interfaces:**
- Produces: nullable column `games.completion_status TEXT` with a CHECK constraint on the vocabulary.

- [ ] **Step 1: Failing migration test** (follow the existing per-version pattern in `tests/test_db_migration.py`):

```python
async def test_v17_to_v18_adds_completion_status(self):
    result = await migrate_db()   # against a v17 DB built with this file's helpers
    self.assertEqual(result.final_version, 18)
    async with get_db() as db:
        cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(games)")}
        self.assertIn("completion_status", cols)
        # CHECK constraint enforced
        with self.assertRaises(Exception):
            await db.execute(
                "INSERT INTO games (name, completion_status) VALUES ('x', 'finished')"
            )
```

- [ ] **Step 2: Run to verify failure.** `.venv/bin/python -m pytest tests/test_db_migration.py -q -k v18`

- [ ] **Step 3: Implement.** In `schema.py`:

```python
# v18 adds games.completion_status: user-declared play status. NULL means
# "unset — infer from playtime as before" (see tools/common.py PLAY_STATE_SQL).
# It is user-set only (update_game): no sync or enrichment writer touches it,
# so unlike other games columns it needs no manual_overrides guard to survive
# syncs — the override entry it still gets from update_game is just bookkeeping.
_V18_SCHEMA_DDL = _V17_SCHEMA_DDL.replace(
    "        is_farmed        INTEGER NOT NULL DEFAULT 0,",
    "        is_farmed        INTEGER NOT NULL DEFAULT 0,\n"
    "        completion_status TEXT CHECK (completion_status IN ('playing', 'completed', 'abandoned')),",
)
```

(The anchor `is_farmed ... DEFAULT 0,` appears in the v11-derived games DDL; verify with a quick read of the composed `_V17_SCHEMA_DDL` that exactly one games-table occurrence matches — the older standalone versions end with different suffixes. If the replace is ambiguous, write the v18 games table out in full instead.)

In `data/db/__init__.py`:

```python
async def _migrate_v17_to_v18(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.completion_status (user-set play status; NULL = infer)."""
    if progress is not None:
        progress("Migrating to v18: add games.completion_status.")
    cols = await _table_columns(db, "games")
    if "completion_status" not in cols:
        # ALTER TABLE cannot add a CHECK'd column with existing rows in old
        # SQLite versions; add plain — the CHECK lives in the canonical DDL
        # applied on rebuilds, and update_game validates the vocabulary anyway.
        await db.execute("ALTER TABLE games ADD COLUMN completion_status TEXT")
    await _set_user_version(db, 18)
    await db.commit()
```

Plus: `SCHEMA_VERSION = 18`, import `_V18_SCHEMA_DDL`, append `(17, _migrate_v17_to_v18),` to `_MIGRATION_STEPS`, and swap the three `_V17_SCHEMA_DDL` use sites.

- [ ] **Step 4: Run migration tests + full suite** — PASS.
- [ ] **Step 5: Commit** — `feat: v18 schema — games.completion_status`.

### Task 2: `update_game` sets/clears completion status

**Files:**
- Modify: `gamelib_mcp/data/db/upserts.py` (`GAME_EDITABLE_FIELDS` gains `"completion_status"`)
- Modify: `gamelib_mcp/tools/platforms.py` (`update_game` gains the parameter)
- Modify: `gamelib_mcp/main.py` (the `update_game` passthrough signature + docstring)
- Test: `tests/test_tools_platforms.py`; `tests/test_tool_registration.py` (the `update_game` entry in `EXPECTED_TOOLS` gains the new parameter name)

**Interfaces:**
- Produces: `update_game(..., completion_status: str | None = None, ...)`; `"none"` resets to NULL; values validated against the vocabulary.

- [ ] **Step 1: Failing tests** in `tests/test_tools_platforms.py` (reuse its DB fixture pattern):

```python
async def test_update_game_sets_completion_status(self):
    game_id = await upsert_game(None, "Hades")
    result = await update_game(game_id=game_id, completion_status="completed")
    self.assertEqual(result["updated"]["completion_status"], "completed")
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT completion_status FROM games WHERE id = ?", (game_id,))
    self.assertEqual(row["completion_status"], "completed")

async def test_update_game_completion_status_none_resets(self):
    game_id = await upsert_game(None, "Hades")
    await update_game(game_id=game_id, completion_status="completed")
    result = await update_game(game_id=game_id, completion_status="none")
    self.assertIsNone(result["updated"]["completion_status"])

async def test_update_game_rejects_bad_completion_status(self):
    game_id = await upsert_game(None, "Hades")
    with self.assertRaises(ToolError):
        await update_game(game_id=game_id, completion_status="finished")
```

- [ ] **Step 2: Verify failures** (unexpected keyword argument).

- [ ] **Step 3: Implement.** In `platforms.py::update_game`, alongside the other field mappings:

```python
COMPLETION_STATUSES = {"playing", "completed", "abandoned"}
...
    if completion_status is not None:
        normalized = completion_status.strip().lower()
        if normalized == "none":
            fields["completion_status"] = None
        elif normalized in COMPLETION_STATUSES:
            fields["completion_status"] = normalized
        else:
            raise ToolError(
                f"Unknown completion_status '{completion_status}'. "
                f"Valid: {sorted(COMPLETION_STATUSES)} or 'none' to reset"
            )
```

Note `apply_manual_game_fields` records overrides via `set(fields) & GAME_EDITABLE_FIELDS`, and its SQL writes NULL fine — but check its handling of a `None` value and the `_display` helper (no JSON/bool special-casing needed). Add `"completion_status"` to `GAME_EDITABLE_FIELDS`. Update the `main.py` passthrough (parameter + one docstring line: `completion_status: playing | completed | abandoned, or 'none' to reset to automatic inference`). Update `EXPECTED_TOOLS["update_game"]` parameters in `tests/test_tool_registration.py`.

- [ ] **Step 4: Run `tests/test_tools_platforms.py`, `tests/test_tool_registration.py`, full suite** — PASS.
- [ ] **Step 5: Commit** — `feat: update_game sets completion_status`.

### Task 3: Completion status flows into play_state, filters, and stats

**Files:**
- Modify: `gamelib_mcp/tools/common.py` (`PLAY_STATE_SQL`)
- Modify: `gamelib_mcp/tools/library.py` (rollup CTE selects `g.completion_status`; new filter values; `_row_to_summary` surfaces the field)
- Modify: `gamelib_mcp/tools/discover.py` (rollup CTE selects it; exclusion condition)
- Modify: `gamelib_mcp/tools/stats.py` (rollup CTE selects it; new summary counts; abandoned games excluded from backlog hours)
- Modify: `gamelib_mcp/tools/detail.py` (surface `completion_status` in the detail payload, near its play_state derivation at `detail.py:112-118`)
- Modify: `gamelib_mcp/tools/models.py` (`GameSummary.completion_status: str | None`; `BacklogStatsResponse` gains `playing`, `completed`, `abandoned` ints)
- Test: `tests/test_tools_library.py`, `tests/test_tools_discover.py`, `tests/test_tools_stats.py`, `tests/test_tools_detail.py`

**Interfaces:**
- Consumes: `games.completion_status` (Task 1), set via Task 2.
- Produces: `play_state` returns `'played'` for `completed` games even with NULL playtime; `get_library_stats(filter=...)` accepts `playing|completed|abandoned`; `get_backlog_stats` response gains the three counts and excludes abandoned from `backlog_hours_hltb`/`unplayed` recommendations context; `discover_games` never returns completed/abandoned games.

- [ ] **Step 1: Failing tests** (spread across the four tool test files; each file already has fixtures inserting games + platforms — a Haiku subagent can scaffold from neighbors):

```python
# test_tools_stats.py
async def test_completed_game_with_unknown_playtime_counts_as_played(self):
    game_id = await upsert_game(None, "Chrono Trigger")   # e.g. a GOG game, NULL playtime
    await upsert_game_platform(game_id, "gog", owned=1)
    await update_game(game_id=game_id, completion_status="completed")
    stats = await get_backlog_stats()
    self.assertEqual(stats["played"], 1)
    self.assertEqual(stats["completed"], 1)

async def test_abandoned_game_excluded_from_backlog_hours(self):
    game_id = await upsert_game(None, "Starfield", hltb_main=40.0)
    await upsert_game_platform(game_id, "steam", playtime_minutes=0, owned=1)
    await update_game(game_id=game_id, completion_status="abandoned")
    stats = await get_backlog_stats()
    self.assertEqual(stats["backlog_hours_hltb"], 0)
    self.assertEqual(stats["abandoned"], 1)

# test_tools_discover.py
async def test_discover_excludes_abandoned_and_completed(self):
    # insert two unplayed games with tags; mark one abandoned
    ...
    results = await discover_games()
    names = [r["name"] for r in results["results"]]
    self.assertNotIn("Starfield", names)

# test_tools_library.py
async def test_library_filter_completed(self):
    ...
    result = await get_library_stats(filter="completed")
    self.assertEqual([r["name"] for r in result["results"]], ["Chrono Trigger"])
```

- [ ] **Step 2: Verify failures.**

- [ ] **Step 3: Implement.**

`tools/common.py` — the CASE gains one branch (order matters: an explicit completed wins over farmed/NULL inference; `abandoned` deliberately does NOT force `played` — an abandoned-at-0-minutes game stays `unplayed` but is excluded from recommendations/backlog by *filters*, keeping play_state purely "was it played"):

```python
PLAY_STATE_SQL = f"""CASE
        WHEN g.completion_status = 'completed' THEN 'played'
        WHEN g.is_farmed = 1            THEN 'unplayed'
        WHEN {PLAYTIME_SUM_SQL} IS NULL THEN 'unknown'
        WHEN {PLAYTIME_SUM_SQL} = 0     THEN 'unplayed'
        ELSE 'played'
    END"""
```

Each of the three rollup CTEs (`library.py`, `discover.py`, `stats.py` — they are deliberately separate; edit all three) adds `g.completion_status,` to its SELECT list.

`discover.py` — next to the existing `inner_conditions.append("play_state IN ('unplayed', 'unknown')")` add:

```python
inner_conditions.append(
    "(completion_status IS NULL OR completion_status NOT IN ('completed', 'abandoned'))"
)
```

`library.py` — extend the filter dispatch (`library.py:329-333` pattern):

```python
elif filter == "playing":
    conditions.append("completion_status = 'playing'")
elif filter == "completed":
    conditions.append("completion_status = 'completed'")
elif filter == "abandoned":
    conditions.append("completion_status = 'abandoned'")
```

and surface `completion_status` in `_row_to_summary` (`library.py:440` area) mirroring how `play_state` is passed through. Update the `filter` docstring in the `main.py` `get_library_stats` passthrough.

`stats.py` — summary SELECT gains:

```python
SUM(CASE WHEN completion_status = 'playing'   THEN 1 ELSE 0 END) AS playing,
SUM(CASE WHEN completion_status = 'completed' THEN 1 ELSE 0 END) AS completed,
SUM(CASE WHEN completion_status = 'abandoned' THEN 1 ELSE 0 END) AS abandoned,
```

and both `unplayed_with_hltb`/`backlog_hours_hltb` CASEs gain `AND (completion_status IS NULL OR completion_status NOT IN ('completed','abandoned'))`. Apply the same exclusion to the three "best unplayed" queries (`WHERE play_state = 'unplayed'` → add the same clause). Add the three counts to the return dict and to `BacklogStatsResponse`.

`detail.py` — include `"completion_status": row["completion_status"]` in the payload beside `play_state`; when set, prefer it for the derived `play_state` copy there too (mirror the SQL rule: completed ⇒ played).

`models.py` — `GameSummary.completion_status: str | None = None`; `BacklogStatsResponse` gains `playing: int`, `completed: int`, `abandoned: int`.

- [ ] **Step 4: Run the four tool test files + full suite** — PASS. Check `test_tools_detail.py` for detail-payload key assertions that need the new key.
- [ ] **Step 5: Commit** — `feat: completion status drives play_state, filters, and backlog stats`.

### Task 4: `suggest_completion_status` tool

**Files:**
- Create: `gamelib_mcp/tools/completion.py`
- Modify: `gamelib_mcp/main.py` (passthrough, `@mcp.tool(annotations=READ_ONLY_TOOL)`)
- Modify: `gamelib_mcp/tools/models.py` (`CompletionSuggestion`, `CompletionSuggestionsResponse`)
- Test: `tests/test_tools_completion.py`; `tests/test_tool_registration.py` (add tool, bump count 32→33)

**Interfaces:**
- Consumes: `PLAY_STATE_SQL` / `PLAYTIME_SUM_SQL` from `tools/common.py`, `games.completion_status`, `game_platforms.last_played`, `steam_platform_data.rtime_last_played`.
- Produces:

```python
async def suggest_completion_status(limit: int = 25) -> dict:
    """
    Suggest completion statuses for games you haven't classified yet.

    Read-only heuristic — nothing is written. Confirm a suggestion with
    update_game(game_id=..., completion_status=...). Two signals:
    - completed: total playtime >= HLTB main-story hours
    - abandoned: >= 2h played, under half of HLTB main, and no activity
      for 12+ months (Steam rtime_last_played / game_platforms.last_played)
    Results are ordered by confidence (playtime/HLTB ratio distance).
    """
```

Response entries: `{game_id, name, suggested_status, reason, playtime_hours, hltb_main, last_played}` — `reason` is a human sentence like `"Played 62h of a 38h game"` / `"Played 5h of 40h, last touched 2024-11-02"`.

- [ ] **Step 1: Failing tests** covering: over-HLTB unset game suggests `completed`; already-classified games are skipped; dormant underplayed game suggests `abandoned`; games with NULL hltb_main and NULL last-played produce no suggestion; `limit` respected.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement.** One query, one pass:

```python
_SUGGESTION_SQL = f"""
WITH rollup AS (
    SELECT g.id AS game_id, g.name, g.hltb_main,
           {PLAYTIME_SUM_SQL} AS playtime_minutes,
           MAX(gp.last_played) AS last_played,
           MAX(spd.rtime_last_played) AS rtime_last_played
    FROM games g
    JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    WHERE g.completion_status IS NULL
      AND g.is_primary_library_item = 1
      AND g.is_farmed = 0
    GROUP BY g.id
)
SELECT * FROM rollup
WHERE playtime_minutes IS NOT NULL AND playtime_minutes > 0 AND hltb_main IS NOT NULL
"""
```

Classification in Python (clearer to test than SQL): `completed` when `playtime_minutes >= hltb_main * 60`; `abandoned` when `playtime_minutes >= 120` and `playtime_minutes < hltb_main * 60 * 0.5` and the freshest of (`last_played` ISO string, `rtime_last_played` epoch) is older than 365 days (skip the abandoned suggestion when both are NULL). Sort: completed suggestions by ratio descending, then abandoned by staleness; clamp with `clamp_limit` from `tools/common.py`.

- [ ] **Step 4: Full suite + ruff + mypy** — PASS.
- [ ] **Step 5: Commit** — `feat: suggest_completion_status heuristic tool`.

### Task 5: Docs

- [ ] `CLAUDE.md`: add the tool to the `tools/` list; add a "Completion status" bullet under Key Design Patterns (user-set, never synced; NULL = inferred; completed ⇒ played; abandoned excluded from backlog/discovery via filters, not play_state). Haiku-delegable.
- [ ] Full suite + ruff + mypy; commit — `docs: completion status`.

## Explicit non-goals (YAGNI)

- No per-platform completion (you finish a *game*).
- No auto-apply of suggestions, ever — the write path stays `update_game` only.
- No date-completed tracking (add a column later if year-in-review needs it; see the playtime-history roadmap item).
