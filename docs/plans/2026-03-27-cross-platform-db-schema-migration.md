# Cross-Platform DB Schema + Migration Implementation Plan

> **For Claude:** Use `${SUPERPOWERS_SKILLS_ROOT}/skills/collaboration/executing-plans/SKILL.md` to implement this plan task-by-task.

**Goal:** Replace the existing single-platform Steam schema with a cross-platform schema (`games` + `game_platforms`) and provide a one-shot migration script that preserves all existing data.

**Architecture:** `init_db()` in `db.py` defines the new schema. A standalone `steam_mcp/migrate.py` script renames existing tables to `_old`, runs `init_db()` to create fresh tables, migrates all rows (mapping `playtime_forever` → `game_platforms.playtime_minutes`), then drops the old tables. Query helpers in `db.py` are updated to match the new schema.

**Tech Stack:** Python 3.12, aiosqlite, SQLite (WAL mode). No test framework — verify with `python -m steam_mcp.migrate` and manual SQLite inspection.

---

### Task 1: Rewrite `init_db()` in `steam_mcp/data/db.py`

**Files:**
- Modify: `steam_mcp/data/db.py:38-121`

**Step 1: Replace the `games` DDL inside `executescript`**

Replace the existing `CREATE TABLE IF NOT EXISTS games (...)` block with:

```sql
CREATE TABLE IF NOT EXISTS games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    appid            INTEGER UNIQUE,
    igdb_id          INTEGER UNIQUE,
    name             TEXT NOT NULL,
    sort_name        TEXT,
    release_date     TEXT,
    genres           TEXT,
    tags             TEXT,
    short_description TEXT,
    metacritic_score INTEGER,
    hltb_main        REAL,
    hltb_extra       REAL,
    hltb_complete    REAL,
    protondb_tier    TEXT,
    opencritic_score INTEGER,
    steam_review_score INTEGER,
    steam_review_desc  TEXT,
    store_enriched   INTEGER DEFAULT 0,
    store_enriched_at TEXT,
    store_cached_at  TEXT,
    hltb_cached_at   TEXT,
    metacritic_cached_at TEXT,
    protondb_cached_at TEXT,
    steamspy_cached_at TEXT,
    rtime_last_played INTEGER,
    is_farmed        INTEGER DEFAULT 0,
    library_updated_at TEXT
);
```

Removed columns: `playtime_forever`, `playtime_2weeks`.
`appid` is now `UNIQUE` (nullable) instead of `PRIMARY KEY`.

**Step 2: Add `game_platforms` DDL to the same `executescript` block (after `games`)**

```sql
CREATE TABLE IF NOT EXISTS game_platforms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id          INTEGER NOT NULL REFERENCES games(id),
    platform         TEXT NOT NULL,
    owned            INTEGER NOT NULL DEFAULT 1,
    playtime_minutes INTEGER,
    last_synced      TEXT,
    UNIQUE(game_id, platform)
);
```

**Step 3: Update `ratings` DDL — change FK from `appid INTEGER` to `game_id INTEGER`**

Replace:
```sql
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    appid INTEGER,
    source TEXT NOT NULL,
    raw_score REAL,
    normalized_score REAL,
    review_text TEXT,
    synced_at TEXT NOT NULL,
    UNIQUE(appid, source)
);
```

With:
```sql
CREATE TABLE IF NOT EXISTS ratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id INTEGER REFERENCES games(id),
    source TEXT NOT NULL,
    raw_score REAL,
    normalized_score REAL,
    review_text TEXT,
    synced_at TEXT NOT NULL,
    UNIQUE(game_id, source)
);
```

**Step 4: Delete all the ALTER TABLE migration blocks below `executescript`**

Remove lines 101–121 entirely (the three `if "col" not in cols:` blocks). They reference old columns and will never apply to the new schema.

**Step 5: Verify the file parses**

```bash
python -c "import steam_mcp.data.db"
```

Expected: no output, no errors.

**Step 6: Commit**

```bash
git add steam_mcp/data/db.py
git commit -m "feat: replace games schema with cross-platform design, add game_platforms table"
```

---

### Task 2: Update `recompute_tag_affinity()` in `db.py`

**Files:**
- Modify: `steam_mcp/data/db.py:123-180`

The function currently joins `ratings r` on `g.appid = r.appid`. Update it to join on `g.id = r.game_id`.

**Step 1: Update the JOIN in the SELECT query**

Change:
```python
rows = await db.execute_fetchall("""
    SELECT r.appid, r.source, r.normalized_score, g.tags
    FROM ratings r
    JOIN games g ON g.appid = r.appid
    WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
""")
```

To:
```python
rows = await db.execute_fetchall("""
    SELECT r.game_id, r.source, r.normalized_score, g.tags
    FROM ratings r
    JOIN games g ON g.id = r.game_id
    WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
""")
```

**Step 2: Update the variable name in the accumulation loop**

Change:
```python
appid = row["appid"]
```
To:
```python
game_id = row["game_id"]
```

And:
```python
tag_data[tag_lower]["appids"].add(appid)
```
To:
```python
tag_data[tag_lower]["appids"].add(game_id)
```

**Step 3: Verify**

```bash
python -c "import steam_mcp.data.db"
```

Expected: no output, no errors.

**Step 4: Commit**

```bash
git add steam_mcp/data/db.py
git commit -m "fix: update tag_affinity JOIN to use games.id / ratings.game_id"
```

---

### Task 3: Add query helpers to `db.py`

**Files:**
- Modify: `steam_mcp/data/db.py` (append after `set_meta`)

These helpers are needed by the migration script and future platform sync modules.

**Step 1: Add `get_game_by_appid()`**

```python
async def get_game_by_appid(appid: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        return await db.execute_fetchone(
            "SELECT * FROM games WHERE appid = ?", (appid,)
        )
```

**Step 2: Add `upsert_game()` — returns the `games.id`**

```python
async def upsert_game(
    appid: int | None,
    name: str,
    **fields,
) -> int:
    """Insert or update a game row. Returns games.id."""
    async with get_db() as db:
        if appid is not None:
            await db.execute(
                """INSERT INTO games (appid, name) VALUES (?, ?)
                   ON CONFLICT(appid) DO UPDATE SET name=excluded.name""",
                (appid, name),
            )
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE appid = ?", (appid,)
            )
        else:
            await db.execute(
                "INSERT OR IGNORE INTO games (name) VALUES (?)", (name,)
            )
            row = await db.execute_fetchone(
                "SELECT id FROM games WHERE name = ? AND appid IS NULL", (name,)
            )
        game_id = row["id"]
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            await db.execute(
                f"UPDATE games SET {cols} WHERE id = ?",
                (*fields.values(), game_id),
            )
        await db.commit()
        return game_id
```

**Step 3: Add `upsert_game_platform()`**

```python
async def upsert_game_platform(
    game_id: int,
    platform: str,
    playtime_minutes: int | None = None,
    owned: int = 1,
) -> None:
    """Insert or update a game_platforms row."""
    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        await db.execute(
            """INSERT INTO game_platforms (game_id, platform, owned, playtime_minutes, last_synced)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(game_id, platform) DO UPDATE SET
                   owned=excluded.owned,
                   playtime_minutes=excluded.playtime_minutes,
                   last_synced=excluded.last_synced""",
            (game_id, platform, owned, playtime_minutes, now),
        )
        await db.commit()
```

**Step 4: Verify**

```bash
python -c "import steam_mcp.data.db"
```

Expected: no output, no errors.

**Step 5: Commit**

```bash
git add steam_mcp/data/db.py
git commit -m "feat: add upsert_game, upsert_game_platform, get_game_by_appid helpers"
```

---

### Task 4: Create `steam_mcp/migrate.py`

**Files:**
- Create: `steam_mcp/migrate.py`

**Step 1: Write the migration script**

```python
"""One-shot migration: old single-table Steam schema → cross-platform schema.

Run with: python -m steam_mcp.migrate
"""

import asyncio
import os

import aiosqlite

from steam_mcp.data.db import _db_path, init_db


async def migrate() -> None:
    db_path = _db_path()
    print(f"Migrating database: {db_path}")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=OFF")

        # Step 1: Check if migration already done
        tables = {
            row[0]
            for row in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "game_platforms" in tables:
            print("game_platforms table already exists — migration already applied.")
            return

        if "games" not in tables:
            print("No games table found — running init_db() on empty database.")
            await init_db()
            return

        print("Step 1: Renaming existing tables to *_old …")
        await db.execute("ALTER TABLE games RENAME TO games_old")
        if "ratings" in tables:
            await db.execute("ALTER TABLE ratings RENAME TO ratings_old")
        if "tag_affinity" in tables:
            await db.execute("ALTER TABLE tag_affinity RENAME TO tag_affinity_old")
        await db.commit()

    # Step 2: Create fresh schema
    print("Step 2: Creating new schema …")
    await init_db()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=OFF")

        # Step 3: Migrate games rows
        print("Step 3: Migrating games …")
        old_games = await db.execute_fetchall("SELECT * FROM games_old")
        old_cols = {desc[0] for desc in (await db.execute("SELECT * FROM games_old LIMIT 0")).description}

        for row in old_games:
            # Build insert, preserving all columns that still exist in new schema
            keep = [
                "appid", "name", "genres", "tags", "short_description",
                "metacritic_score", "hltb_main", "hltb_extra",
                "protondb_tier", "steam_review_score", "steam_review_desc",
                "store_cached_at", "hltb_cached_at", "metacritic_cached_at",
                "protondb_cached_at", "steamspy_cached_at",
                "rtime_last_played", "is_farmed", "library_updated_at",
            ]
            present = [c for c in keep if c in old_cols and row[c] is not None]
            if not present:
                continue
            cols_sql = ", ".join(present)
            placeholders = ", ".join("?" for _ in present)
            values = [row[c] for c in present]
            await db.execute(
                f"INSERT OR IGNORE INTO games ({cols_sql}) VALUES ({placeholders})",
                values,
            )

        await db.commit()

        # Step 4: Create game_platforms rows for Steam
        print("Step 4: Creating game_platforms rows for Steam …")
        for row in old_games:
            game = await db.execute_fetchone(
                "SELECT id FROM games WHERE appid = ?", (row["appid"],)
            )
            if game is None:
                continue
            playtime = row["playtime_forever"] if "playtime_forever" in old_cols else None
            await db.execute(
                """INSERT OR IGNORE INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, last_synced)
                   VALUES (?, 'steam', 1, ?, datetime('now'))""",
                (game["id"], playtime),
            )
        await db.commit()

        # Step 5: Migrate ratings
        if "ratings_old" in {row[0] for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}:
            print("Step 5: Migrating ratings …")
            old_ratings = await db.execute_fetchall("SELECT * FROM ratings_old")
            for row in old_ratings:
                game = await db.execute_fetchone(
                    "SELECT id FROM games WHERE appid = ?", (row["appid"],)
                )
                if game is None:
                    continue
                await db.execute(
                    """INSERT OR IGNORE INTO ratings
                       (game_id, source, raw_score, normalized_score, review_text, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        game["id"],
                        row["source"],
                        row["raw_score"],
                        row["normalized_score"],
                        row["review_text"],
                        row["synced_at"],
                    ),
                )
            await db.commit()
        else:
            print("Step 5: No ratings_old table — skipping.")

        # Step 6: Migrate tag_affinity (no FK change needed)
        tables_now = {row[0] for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "tag_affinity_old" in tables_now:
            print("Step 6: Migrating tag_affinity …")
            await db.execute("DELETE FROM tag_affinity")
            await db.execute(
                "INSERT INTO tag_affinity SELECT * FROM tag_affinity_old"
            )
            await db.commit()
        else:
            print("Step 6: No tag_affinity_old table — skipping.")

        # Step 7: Drop *_old tables
        print("Step 7: Dropping *_old tables …")
        for t in ("games_old", "ratings_old", "tag_affinity_old"):
            await db.execute(f"DROP TABLE IF EXISTS {t}")
        await db.commit()

    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
```

**Step 2: Run the migration against a copy of the real DB to verify**

```bash
cp steam.db steam.db.bak
python -m steam_mcp.migrate
```

Expected output:
```
Migrating database: steam.db
Step 1: Renaming existing tables to *_old …
Step 2: Creating new schema …
Step 3: Migrating games …
Step 4: Creating game_platforms rows for Steam …
Step 5: Migrating ratings …
Step 6: Migrating tag_affinity …
Step 7: Dropping *_old tables …
Migration complete.
```

**Step 3: Spot-check the migrated data**

```bash
sqlite3 steam.db "SELECT g.id, g.appid, g.name, gp.platform, gp.playtime_minutes FROM games g JOIN game_platforms gp ON gp.game_id = g.id LIMIT 5;"
```

Expected: rows with Steam game names, `platform='steam'`, and playtime values from old `playtime_forever`.

```bash
sqlite3 steam.db "SELECT COUNT(*) FROM games; SELECT COUNT(*) FROM game_platforms; SELECT COUNT(*) FROM ratings;"
```

Expected: same game count as before migration; `game_platforms` count equals `games` count; ratings count unchanged.

**Step 4: Run migration a second time to verify idempotency guard**

```bash
python -m steam_mcp.migrate
```

Expected: `game_platforms table already exists — migration already applied.`

**Step 5: Commit**

```bash
git add steam_mcp/migrate.py
git commit -m "feat: add one-shot migration script for cross-platform schema"
```

---

### Task 5: Push branch

**Step 1: Push**

```bash
git push -u origin claude/fix-token-limit-issue-GVKUw
```

Expected: branch pushed, no errors.

---

## Done

Phase 1 complete. The new schema is live and all existing Steam data is preserved in the cross-platform structure.

Next plan: `2026-03-27-cross-platform-data-modules.md` (Phase 2 — platform sync modules: psn.py, epic.py, gog.py, nintendo.py, xbox.py, itchio.py).
