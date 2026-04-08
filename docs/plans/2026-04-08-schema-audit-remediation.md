# Schema Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the concrete schema and operational issues found in the SQLite audit without taking on risky data-model redesign work.

**Architecture:** Keep the live schema shape mostly intact and focus on three targeted remediations: correct fresh database initialization so v5 columns always exist, make database path selection deterministic so the app does not silently operate on the wrong file, and repair plus prevent invalid `is_primary` identifier state. Leave duplicate game-name consolidation out of scope for this pass because that requires product rules for when same-name rows should merge versus remain separate.

**Tech Stack:** Python 3.12, SQLite, aiosqlite, existing migration helpers in `gamelib_mcp/data/db.py`, pytest/unittest test suite in `tests/`.

---

### Task 1: Lock in regression coverage for fresh-init and DB path selection

**Files:**
- Modify: `tests/test_db_migration.py`
- Modify: `tests/test_startup_sync.py`
- Reference: `gamelib_mcp/data/db.py`

- [ ] **Step 1: Add a fresh-database migration test that asserts the full v5 column set**

Add a test in `tests/test_db_migration.py` that creates a brand-new temporary SQLite file, runs `migrate_db()`, and then asserts `PRAGMA user_version = 5` plus both v5-only columns exist on `game_platform_enrichment`.

```python
async def test_fresh_db_initializes_with_v5_columns(self):
    async with aiosqlite.connect(self.db_path) as db:
        await _configure_connection(db, enable_wal=True)
        result = await _run_migrations(db)

        version = await _get_user_version(db)
        cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(game_platform_enrichment)")}

    self.assertEqual(version, 5)
    self.assertEqual(result.final_version, 5)
    self.assertIn("opencritic_url", cols)
    self.assertIn("opencritic_num_reviews", cols)
```

- [ ] **Step 2: Run the new migration test and verify it fails before the fix**

Run: `python3 -m pytest tests/test_db_migration.py -k fresh_db_initializes_with_v5_columns -v`

Expected before fix: FAIL because `opencritic_url` and `opencritic_num_reviews` are missing on a fresh database even though `user_version` is 5.

- [ ] **Step 3: Add a DB path resolution test that proves repo-root fallbacks are not used when `./data/gamelib.db` is the intended project database**

Add a focused test in `tests/test_startup_sync.py` or `tests/test_db_migration.py` that patches the environment and filesystem checks around `_db_path()` to cover:

```python
def test_db_path_prefers_database_url(self):
    with patch.dict(os.environ, {"DATABASE_URL": "file:./data/gamelib.db"}, clear=False):
        self.assertEqual(_db_path(), "./data/gamelib.db")

def test_db_path_defaults_to_project_data_db(self):
    with patch.dict(os.environ, {}, clear=True):
        with patch("gamelib_mcp.data.db.os.path.exists", return_value=False):
            self.assertEqual(_db_path(), "data/gamelib.db")
```

- [ ] **Step 4: Run the targeted DB path tests**

Run: `python3 -m pytest tests/test_startup_sync.py -k db_path -v`

Expected before fix: FAIL if the code still falls back to repo-root `gamelib.db`.

- [ ] **Step 5: Commit the failing-test baseline**

```bash
git add tests/test_db_migration.py tests/test_startup_sync.py
git commit -m "test: capture schema init and db path regressions"
```

---

### Task 2: Fix fresh initialization so new databases are truly schema v5

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Test: `tests/test_db_migration.py`

- [ ] **Step 1: Introduce a single canonical latest-schema DDL constant**

Replace the current duplication where `_V4_SCHEMA_DDL` is treated as the fresh-init schema. Either rename `_V4_SCHEMA_DDL` to `_LATEST_SCHEMA_DDL` after folding in the v5 columns, or add a new `_V5_SCHEMA_DDL` constant and make it the only “fresh database” DDL.

```python
_V5_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS game_platform_enrichment (
        game_platform_id       INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        platform_release_date  TEXT,
        metacritic_score       INTEGER,
        metacritic_url         TEXT,
        metacritic_claimed_at  TEXT,
        opencritic_id          INTEGER,
        opencritic_url         TEXT,
        opencritic_score       INTEGER,
        opencritic_tier        TEXT,
        opencritic_percent_rec REAL,
        opencritic_num_reviews INTEGER,
        opencritic_cached_at   TEXT,
        opencritic_claimed_at  TEXT,
        metacritic_cached_at   TEXT
    );
"""
```

- [ ] **Step 2: Make `_run_migrations()` initialize fresh databases from the latest schema constant**

Update the fresh path in `gamelib_mcp/data/db.py` so it executes the latest DDL instead of `_V4_SCHEMA_DDL`.

```python
if detected_state == "fresh":
    await db.executescript(_V5_SCHEMA_DDL)
    await _set_user_version(db, SCHEMA_VERSION)
    await db.commit()
```

- [ ] **Step 3: Make the final reconciliation step use the latest schema constant too**

Update the end of `_run_migrations()` so the idempotent `CREATE TABLE IF NOT EXISTS ...` pass uses the same latest-schema constant.

```python
await db.executescript(_V5_SCHEMA_DDL)
```

- [ ] **Step 4: Remove or correct any misleading comments/messages that imply a fresh DB is v5 while building from v4 DDL**

Ensure the migration progress message and nearby comments are accurate after the constant change.

- [ ] **Step 5: Run the focused migration tests**

Run: `python3 -m pytest tests/test_db_migration.py -k "fresh_db_initializes_with_v5_columns or migration" -v`

Expected after fix: PASS, with the fresh DB containing `opencritic_url` and `opencritic_num_reviews`.

- [ ] **Step 6: Commit the migration fix**

```bash
git add gamelib_mcp/data/db.py tests/test_db_migration.py
git commit -m "fix: initialize fresh sqlite databases at schema v5"
```

---

### Task 3: Make database path selection deterministic and project-safe

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Modify: `CLAUDE.md`
- Optionally modify: `.env.example`
- Test: `tests/test_startup_sync.py`

- [ ] **Step 1: Change `_db_path()` so the default project DB is `data/gamelib.db`**

Keep `DATABASE_URL` as the first priority, but remove the repo-root `gamelib.db` / `steam.db` fallback behavior.

```python
def _db_path() -> str:
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
        _ENV_LOADED = True

    configured = os.getenv("DATABASE_URL")
    if configured:
        return configured.removeprefix("file:")

    return "data/gamelib.db"
```

- [ ] **Step 2: If compatibility with legacy root-level DBs must be preserved, make it explicit and noisy**

If you decide not to remove the fallback entirely, add an explicit guard that only uses repo-root DBs when an opt-in env var is set, for example:

```python
if os.getenv("ALLOW_LEGACY_DB_FALLBACK") == "1" and os.path.exists("gamelib.db"):
    return "gamelib.db"
```

Do not silently keep the current behavior.

- [ ] **Step 3: Update repository docs so the runtime path rule matches the code**

Adjust `CLAUDE.md` and, if present, `.env.example` so the documented default is the same as `_db_path()`.

```markdown
- `DATABASE_URL` — SQLite path. Default project DB is `file:./data/gamelib.db`.
- Root-level `./gamelib.db` is not used unless explicitly opted into for legacy recovery.
```

- [ ] **Step 4: Run targeted tests for path resolution**

Run: `python3 -m pytest tests/test_startup_sync.py -k db_path -v`

Expected after fix: PASS, with `_db_path()` resolving to `data/gamelib.db` when no env var is set.

- [ ] **Step 5: Commit the path hardening**

```bash
git add gamelib_mcp/data/db.py CLAUDE.md tests/test_startup_sync.py .env.example
git commit -m "fix: default sqlite path to project data database"
```

If `.env.example` does not exist or does not need changes, omit it from `git add`.

---

### Task 4: Repair and guard `game_platform_identifiers.is_primary` consistency

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Modify: `tests/test_db_migration.py`
- Optionally add: `tests/test_library_sync.py`

- [ ] **Step 1: Add a regression test for duplicate-primary identifier rows**

Create a test that inserts two identifier rows for the same `(game_platform_id, identifier_type)` with `is_primary = 1`, runs the repair/migration helper, and asserts exactly one row remains primary.

```python
async def test_identifier_primary_repair_demotes_extra_rows(self):
    await db.execute(
        "INSERT INTO game_platform_identifiers (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) VALUES (?, ?, ?, 1, ?)",
        (platform_id, "steam_appid", "100", now),
    )
    await db.execute(
        "INSERT INTO game_platform_identifiers (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at) VALUES (?, ?, ?, 1, ?)",
        (platform_id, "steam_appid", "101", now),
    )

    await _repair_identifier_primary_flags(db)

    rows = await db.execute_fetchall(
        "SELECT identifier_value, is_primary FROM game_platform_identifiers WHERE game_platform_id = ? AND identifier_type = ? ORDER BY id",
        (platform_id, "steam_appid"),
    )
    self.assertEqual([row[1] for row in rows], [1, 0])
```

- [ ] **Step 2: Implement a deterministic repair helper in `gamelib_mcp/data/db.py`**

Add a helper that keeps one primary row per `(game_platform_id, identifier_type)` and demotes the rest. Pick a stable rule such as lowest `id` wins, or `last_seen_at DESC, id ASC` if that better matches sync semantics.

```python
async def _repair_identifier_primary_flags(db: aiosqlite.Connection) -> None:
    await db.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY game_platform_id, identifier_type
                       ORDER BY is_primary DESC, id ASC
                   ) AS rn
            FROM game_platform_identifiers
        )
        UPDATE game_platform_identifiers
        SET is_primary = CASE
            WHEN id IN (SELECT id FROM ranked WHERE rn = 1) THEN 1
            ELSE 0
        END
        WHERE id IN (SELECT id FROM ranked)
        """
    )
```

- [ ] **Step 3: Run the repair from migrations so existing databases are corrected on startup**

Call the repair helper at the end of `_run_migrations()` before the final commit so current databases get normalized without requiring a manual script.

```python
await _repair_identifier_primary_flags(db)
await db.executescript(_V5_SCHEMA_DDL)
await db.commit()
```

- [ ] **Step 4: Tighten write paths so future upserts do not recreate the inconsistency**

Review identifier insert/update flows, especially `upsert_game_platform_identifier()` and Steam/Epic/Nintendo sync paths, and ensure when a row is written as primary the sibling rows for the same `(game_platform_id, identifier_type)` are demoted in the same transaction.

```python
await db.execute(
    """
    UPDATE game_platform_identifiers
    SET is_primary = 0
    WHERE game_platform_id = ? AND identifier_type = ? AND id != ?
    """,
    (game_platform_id, identifier_type, row_id),
)
```

- [ ] **Step 5: Run the targeted tests**

Run: `python3 -m pytest tests/test_db_migration.py -k identifier_primary -v`

Expected after fix: PASS, with only one primary identifier per `(game_platform_id, identifier_type)`.

- [ ] **Step 6: Commit the identifier consistency fix**

```bash
git add gamelib_mcp/data/db.py tests/test_db_migration.py
git commit -m "fix: keep one primary identifier per platform and type"
```

---

### Task 5: Remove the redundant lookup index and verify no query regression

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Test: `tests/test_db_migration.py`

- [ ] **Step 1: Drop the explicit duplicate lookup index from the latest schema DDL**

Remove:

```sql
CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
    ON game_platform_identifiers(identifier_type, identifier_value);
```

The unique constraint on `(identifier_type, identifier_value)` already creates `sqlite_autoindex_game_platform_identifiers_1`.

- [ ] **Step 2: Add a migration cleanup step for existing databases**

Add:

```python
await db.execute("DROP INDEX IF EXISTS idx_game_platform_identifiers_lookup")
```

Place it in `_run_migrations()` or a small helper that runs during reconciliation.

- [ ] **Step 3: Add a migration test that asserts the duplicate index no longer exists**

```python
indexes = {row[1] for row in await db.execute_fetchall("PRAGMA index_list(game_platform_identifiers)")}
self.assertNotIn("idx_game_platform_identifiers_lookup", indexes)
```

- [ ] **Step 4: Run the index-focused tests**

Run: `python3 -m pytest tests/test_db_migration.py -k "index or identifier" -v`

Expected after fix: PASS, with queries still using `sqlite_autoindex_game_platform_identifiers_1`.

- [ ] **Step 5: Commit the cleanup**

```bash
git add gamelib_mcp/data/db.py tests/test_db_migration.py
git commit -m "chore: remove redundant identifier lookup index"
```

---

### Task 6: Verify the full remediation set and document deferred follow-ups

**Files:**
- Modify: `docs/plans/2026-04-08-schema-audit-remediation.md`
- Optionally modify: `docs/plans/next-schema-followups.md`

- [ ] **Step 1: Run the focused database test set**

Run: `python3 -m pytest tests/test_db_migration.py tests/test_startup_sync.py -v`

Expected: PASS for the migration, path-selection, and identifier-integrity coverage added in this plan.

- [ ] **Step 2: Re-run the live audit spot checks against `./data/gamelib.db`**

Run:

```bash
sqlite3 ./data/gamelib.db "PRAGMA user_version; PRAGMA integrity_check; PRAGMA foreign_key_check;"
sqlite3 ./data/gamelib.db "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='game_platform_identifiers' ORDER BY name;"
sqlite3 ./data/gamelib.db "SELECT COUNT(*) FROM (SELECT game_platform_id, identifier_type FROM game_platform_identifiers GROUP BY game_platform_id, identifier_type HAVING SUM(CASE WHEN is_primary=1 THEN 1 ELSE 0 END) > 1);"
```

Expected:
- `user_version` is `5`
- `integrity_check` is `ok`
- `foreign_key_check` returns no rows
- redundant index is gone
- duplicate-primary count is `0`

- [ ] **Step 3: Record explicitly deferred items so they do not get silently forgotten**

Add a short note to the plan or a follow-up doc stating these are intentionally deferred:

```markdown
- Duplicate `games.name` groups require product rules for merge vs coexist.
- `games.sort_name` is unused and can be removed only after code search plus migration.
- Case-insensitive / substring search improvements should use FTS or explicit expression indexes in a separate performance pass.
```

- [ ] **Step 4: Commit verification and follow-up notes**

```bash
git add docs/plans/2026-04-08-schema-audit-remediation.md
git commit -m "docs: record schema remediation verification and follow-ups"
```

---

## Deferred Follow-ups (intentionally out of scope for this pass)

- **Duplicate `games.name` groups**: rows sharing the same name require product rules for merge vs. coexist before consolidation can happen.
- **`games.sort_name` column**: currently unused — can be removed only after a code search confirms no references and a migration is written.
- **Case-insensitive / substring search**: improvements should use FTS or explicit expression indexes in a separate performance pass.
