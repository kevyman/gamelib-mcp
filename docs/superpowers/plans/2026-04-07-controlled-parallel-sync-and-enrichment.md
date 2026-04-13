# Controlled Parallel Sync And Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let platform sync and enrichment families progress concurrently under explicit limits, and add an independent IGDB background worker so IGDB can start while Steam sync or Steam enrichment is already running.

**Architecture:** `gamelib_mcp/main.py` schedules refresh and enrichment independently. `gamelib_mcp/data/enrich_bg.py` becomes a concurrent worker supervisor instead of a serialized phase runner. DB-backed claim columns in `db.py` prevent duplicate work, and `igdb.py` gains a background backfill path that reuses the existing request gate and metadata application logic.

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, httpx, unittest, AsyncMock

---

## File Map

- Modify: `gamelib_mcp/main.py`
  Purpose: start refresh and enrichment independently; remove Steam-only gate semantics.
- Modify: `gamelib_mcp/data/enrich_bg.py`
  Purpose: replace sequential phases with bounded concurrent worker families and quiescence detection.
- Modify: `gamelib_mcp/data/igdb.py`
  Purpose: add IGDB background eligibility helpers, claim-aware worker logic, and platform-hint selection for canonical rows.
- Modify: `gamelib_mcp/data/db.py`
  Purpose: add claim columns via migration and helpers for claim acquisition/release.
- Modify: `gamelib_mcp/data/steam_store.py`
  Purpose: cooperate with claim lifecycle for store enrichment.
- Modify: `tests/test_startup_sync.py`
  Purpose: verify enrichment starts independently of Steam completion and IGDB can begin while Steam refresh is active.
- Modify: `tests/test_db_migration.py`
  Purpose: verify new claim columns exist and preserve existing behavior.
- Modify: `tests/test_igdb.py`
  Purpose: verify IGDB background backfill and duplicate-claim protection.
- Modify: `tests/test_enrich_bg.py` or create it if absent
  Purpose: verify concurrent worker-family behavior, quiescence, and stale-claim handling.

### Task 1: Add Claim Columns And Migration Coverage

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Modify: `tests/test_db_migration.py`

- [ ] **Step 1: Write the failing migration test for claim columns**

```python
def test_schema_contains_claim_columns(self) -> None:
    async def run() -> None:
        async with db.get_db() as conn:
            games_cols = await conn.execute_fetchall("PRAGMA table_info(games)")
            spd_cols = await conn.execute_fetchall("PRAGMA table_info(steam_platform_data)")
            gpe_cols = await conn.execute_fetchall("PRAGMA table_info(game_platform_enrichment)")

        self.assertIn("igdb_claimed_at", {row["name"] for row in games_cols})
        self.assertIn("hltb_claimed_at", {row["name"] for row in games_cols})
        self.assertIn("store_claimed_at", {row["name"] for row in spd_cols})
        self.assertIn("protondb_claimed_at", {row["name"] for row in spd_cols})
        self.assertIn("steamspy_claimed_at", {row["name"] for row in spd_cols})
        self.assertIn("opencritic_claimed_at", {row["name"] for row in gpe_cols})
        self.assertIn("metacritic_claimed_at", {row["name"] for row in gpe_cols})
    asyncio.run(run())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_migration.py -k claim_columns -v`
Expected: FAIL because the schema does not yet contain the claim columns.

- [ ] **Step 3: Add claim columns to the latest schema and migration path**

```python
# gamelib_mcp/data/db.py
CREATE TABLE games (
    id                INTEGER PRIMARY KEY,
    igdb_id           INTEGER UNIQUE,
    name              TEXT NOT NULL,
    sort_name         TEXT,
    release_date      TEXT,
    genres            TEXT,
    tags              TEXT,
    short_description TEXT,
    hltb_main         REAL,
    hltb_extra        REAL,
    hltb_complete     REAL,
    hltb_cached_at    TEXT,
    igdb_cached_at   TEXT,
    igdb_claimed_at  TEXT,
    hltb_claimed_at  TEXT
)

CREATE TABLE steam_platform_data (
    game_platform_id     INTEGER PRIMARY KEY,
    last_played_date     TEXT,
    rtime_last_played    INTEGER,
    store_cached_at     TEXT,
    store_claimed_at    TEXT,
    steam_review_score   INTEGER,
    steam_review_desc    TEXT,
    protondb_cached_at  TEXT,
    protondb_claimed_at TEXT,
    protondb_tier        TEXT,
    steamspy_cached_at  TEXT,
    steamspy_claimed_at TEXT
)

CREATE TABLE game_platform_enrichment (
    game_platform_id          INTEGER PRIMARY KEY,
    platform_release_date     TEXT,
    opencritic_score          REAL,
    opencritic_cached_at     TEXT,
    opencritic_claimed_at    TEXT,
    opencritic_url            TEXT,
    metacritic_score          REAL,
    metacritic_cached_at     TEXT,
    metacritic_claimed_at    TEXT
)
```

- [ ] **Step 4: Add reusable claim helper(s)**

```python
# gamelib_mcp/data/db.py
def _claim_cutoff_iso(minutes: int = 15) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
```

```python
async def clear_claim(table: str, claim_column: str, row_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            f"UPDATE {table} SET {claim_column} = NULL WHERE id = ?",
            (row_id,),
        )
        await db.commit()
```

- [ ] **Step 5: Run migration tests**

Run: `python3 -m pytest tests/test_db_migration.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gamelib_mcp/data/db.py tests/test_db_migration.py
git commit -m "feat: add enrichment claim columns"
```

### Task 2: Start Refresh And Enrichment Independently At Startup

**Files:**
- Modify: `gamelib_mcp/main.py`
- Modify: `tests/test_startup_sync.py`

- [ ] **Step 1: Write the failing startup test for independent enrichment scheduling**

```python
async def test_stale_startup_starts_enrichment_without_waiting_for_steam(self) -> None:
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    refresh_started = asyncio.Event()
    enrich_started = asyncio.Event()

    async def slow_refresh() -> dict:
        refresh_started.set()
        await asyncio.Future()

    async def fake_enrich() -> None:
        enrich_started.set()

    with (
        patch("gamelib_mcp.data.db.init_db", AsyncMock()),
        patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
        patch("gamelib_mcp.main._run_startup_refresh", AsyncMock(side_effect=slow_refresh)),
        patch("gamelib_mcp.data.enrich_bg.background_enrich", AsyncMock(side_effect=fake_enrich)),
    ):
        cm = lifespan(object())
        await cm.__aenter__()
        await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
        await asyncio.wait_for(enrich_started.wait(), timeout=0.1)
        await cm.__aexit__(None, None, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_startup_sync.py -k independent_enrichment -v`
Expected: FAIL because startup still waits for the Steam-ready event before calling `background_enrich()`.

- [ ] **Step 3: Remove Steam-gated enrichment scheduling**

```python
# gamelib_mcp/main.py
if needs_refresh:
    logger.info("Library stale or missing — scheduling background refresh...")
    await _ensure_startup_refresh()

from .data.enrich_bg import background_enrich
asyncio.create_task(background_enrich())
```

- [ ] **Step 4: Update stale-startup tests to assert IGDB/enrichment can begin during refresh**

```python
async def test_stale_startup_does_not_wait_for_steam_before_enrichment(self) -> None:
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    refresh_release = asyncio.Event()
    enrich_started = asyncio.Event()

    async def slow_refresh() -> dict:
        await refresh_release.wait()
        return {"steam": {"games_upserted": 1}}

    async def fake_enrich() -> None:
        enrich_started.set()

    with (
        patch("gamelib_mcp.data.db.init_db", AsyncMock()),
        patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
        patch("gamelib_mcp.main._run_startup_refresh", AsyncMock(side_effect=slow_refresh)),
        patch("gamelib_mcp.data.enrich_bg.background_enrich", AsyncMock(side_effect=fake_enrich)) as enrich,
    ):
        cm = lifespan(object())
        await cm.__aenter__()
        await asyncio.wait_for(enrich_started.wait(), timeout=0.1)
        enrich.assert_awaited_once()
        refresh_release.set()
        await cm.__aexit__(None, None, None)
```

- [ ] **Step 5: Run startup tests**

Run: `python3 -m pytest tests/test_startup_sync.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gamelib_mcp/main.py tests/test_startup_sync.py
git commit -m "feat: decouple startup enrichment from steam refresh"
```

### Task 3: Add Claim-Aware Selection Helpers

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Modify: `tests/test_enrich_bg.py` or create it

- [ ] **Step 1: Write the failing claim helper test**

```python
async def test_claim_helper_prevents_double_claim(self) -> None:
    first = await db.claim_game_ids_for_igdb(limit=1)
    second = await db.claim_game_ids_for_igdb(limit=1)
    self.assertEqual(len(first), 1)
    self.assertEqual(second, [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_enrich_bg.py -k double_claim -v`
Expected: FAIL because no claim helpers exist.

- [ ] **Step 3: Add per-family claim helpers**

```python
# gamelib_mcp/data/db.py
async def claim_game_ids_for_igdb(limit: int, stale_before: str) -> list[int]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT id
               FROM games
               WHERE igdb_cached_at IS NULL
                 AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)
               ORDER BY id
               LIMIT ?""",
            (stale_before, limit),
        )
        ids = [row["id"] for row in rows]
        if not ids:
            return []

        now = datetime.now(timezone.utc).isoformat()
        claimed: list[int] = []
        for game_id in ids:
            cursor = await db.execute(
                """UPDATE games
                   SET igdb_claimed_at = ?
                   WHERE id = ?
                     AND igdb_cached_at IS NULL
                     AND (igdb_claimed_at IS NULL OR igdb_claimed_at < ?)""",
                (now, game_id, stale_before),
            )
            if cursor.rowcount:
                claimed.append(game_id)
        await db.commit()
        return claimed
```

```python
async def release_game_claim(game_id: int, column: str) -> None:
    async with get_db() as db:
        await db.execute(
            f"UPDATE games SET {column} = NULL WHERE id = ?",
            (game_id,),
        )
        await db.commit()
```

- [ ] **Step 4: Mirror the same pattern for store/protondb/steamspy/opencritic/metacritic/HLTB**

```python
async def claim_steam_platform_ids_for_store(limit: int, stale_before: str) -> list[int]:
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT spd.game_platform_id AS id
               FROM steam_platform_data spd
               JOIN game_platforms gp ON gp.id = spd.game_platform_id
               JOIN games g ON g.id = gp.game_id
               WHERE spd.store_cached_at IS NULL
                 AND (spd.store_claimed_at IS NULL OR spd.store_claimed_at < ?)
                 AND g.is_farmed = 0
               ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
               LIMIT ?""",
            (stale_before, limit),
        )
        ids = [row["id"] for row in rows]
        now = datetime.now(timezone.utc).isoformat()
        claimed: list[int] = []
        for platform_id in ids:
            cursor = await db.execute(
                """UPDATE steam_platform_data
                   SET store_claimed_at = ?
                   WHERE game_platform_id = ?
                     AND store_cached_at IS NULL
                     AND (store_claimed_at IS NULL OR store_claimed_at < ?)""",
                (now, platform_id, stale_before),
            )
            if cursor.rowcount:
                claimed.append(platform_id)
        await db.commit()
        return claimed
```

- [ ] **Step 5: Run claim helper tests**

Run: `python3 -m pytest tests/test_enrich_bg.py -k claim -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gamelib_mcp/data/db.py tests/test_enrich_bg.py
git commit -m "feat: add claim-aware enrichment selection helpers"
```

### Task 4: Refactor Background Enrichment Into A Concurrent Supervisor

**Files:**
- Modify: `gamelib_mcp/data/enrich_bg.py`
- Modify: `tests/test_enrich_bg.py` or create it

- [ ] **Step 1: Write the failing overlap test for independent worker families**

```python
async def test_background_enrich_runs_worker_families_concurrently(self) -> None:
    started = {"store": asyncio.Event(), "igdb": asyncio.Event()}
    release = asyncio.Event()

    async def fake_store_worker() -> int:
        started["store"].set()
        await release.wait()
        return 1

    async def fake_igdb_worker() -> int:
        started["igdb"].set()
        await release.wait()
        return 1

    with (
        patch("gamelib_mcp.data.enrich_bg._run_store_workers", AsyncMock(side_effect=fake_store_worker)),
        patch("gamelib_mcp.data.enrich_bg._run_igdb_workers", AsyncMock(side_effect=fake_igdb_worker)),
        patch("gamelib_mcp.data.enrich_bg._run_hltb_workers", AsyncMock(return_value=0)),
        patch("gamelib_mcp.data.enrich_bg._run_protondb_workers", AsyncMock(return_value=0)),
        patch("gamelib_mcp.data.enrich_bg._run_steamspy_workers", AsyncMock(return_value=0)),
        patch("gamelib_mcp.data.enrich_bg._run_opencritic_workers", AsyncMock(return_value=0)),
        patch("gamelib_mcp.data.enrich_bg._run_metacritic_workers", AsyncMock(return_value=0)),
    ):
        task = asyncio.create_task(enrich_bg.background_enrich())
        await asyncio.wait_for(started["store"].wait(), timeout=0.1)
        await asyncio.wait_for(started["igdb"].wait(), timeout=0.1)
        release.set()
        await asyncio.wait_for(task, timeout=0.1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_enrich_bg.py -k concurrently -v`
Expected: FAIL because `background_enrich()` still executes families sequentially.

- [ ] **Step 3: Introduce a worker-family supervisor**

```python
# gamelib_mcp/data/enrich_bg.py
async def background_enrich() -> None:
    results = await asyncio.gather(
        _run_store_workers(),
        _run_hltb_workers(),
        _run_protondb_workers(),
        _run_steamspy_workers(),
        _run_opencritic_workers(),
        _run_metacritic_workers(),
        _run_igdb_workers(),
        return_exceptions=True,
    )
    logger.info("Background enrichment complete: %r", results)
```

- [ ] **Step 4: Add quiescence polling so workers do not exit before concurrent refresh inserts more rows**

```python
_IDLE_POLLS = 3
_IDLE_SLEEP_SECONDS = 1.0

async def _run_until_quiescent(run_batch: Callable[[], Awaitable[int]]) -> int:
    idle_polls = 0
    total = 0
    while idle_polls < _IDLE_POLLS:
        processed = await run_batch()
        total += processed
        if processed:
            idle_polls = 0
            continue
        idle_polls += 1
        await asyncio.sleep(_IDLE_SLEEP_SECONDS)
    return total
```

- [ ] **Step 5: Keep per-family pacing and concurrency local**

```python
_STORE_CONCURRENCY = 4
_IGDB_WORKER_CONCURRENCY = 2
_BATCH_SIZE = 3
```

- [ ] **Step 6: Run enrichment supervisor tests**

Run: `python3 -m pytest tests/test_enrich_bg.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gamelib_mcp/data/enrich_bg.py tests/test_enrich_bg.py
git commit -m "feat: parallelize enrichment families with bounded workers"
```

### Task 5: Add An Independent IGDB Background Worker

**Files:**
- Modify: `gamelib_mcp/data/igdb.py`
- Modify: `gamelib_mcp/data/enrich_bg.py`
- Modify: `tests/test_igdb.py`
- Modify: `tests/test_startup_sync.py`

- [ ] **Step 1: Write the failing IGDB backfill test**

```python
async def test_backfill_missing_games_uses_existing_request_gate(self) -> None:
    game_row = {"id": 7, "name": "Portal", "igdb_id": None}

    with (
        patch("gamelib_mcp.data.igdb.claim_game_ids_for_igdb", AsyncMock(return_value=[7])),
        patch("gamelib_mcp.data.igdb.load_games_for_igdb_backfill", AsyncMock(return_value=[game_row])),
        patch("gamelib_mcp.data.igdb.resolve_game", AsyncMock(return_value=igdb_game)),
        patch("gamelib_mcp.data.igdb._apply_igdb_metadata", AsyncMock()) as apply_metadata,
    ):
        count = await igdb.backfill_missing_games(limit=1)

    self.assertEqual(count, 1)
    apply_metadata.assert_awaited_once_with(7, igdb_game)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_igdb.py -k backfill_missing_games -v`
Expected: FAIL because no IGDB background backfill entry point exists.

- [ ] **Step 3: Add a claim-aware IGDB backfill API**

```python
# gamelib_mcp/data/igdb.py
async def backfill_missing_games(limit: int = 10) -> int:
    stale_before = _claim_cutoff_iso()
    game_ids = await claim_game_ids_for_igdb(limit=limit, stale_before=stale_before)
    if not game_ids:
        return 0

    rows = await load_games_for_igdb_backfill(game_ids)
    processed = 0
    for row in rows:
        try:
            platform_hint = await choose_igdb_platform_hint(row["id"])
            igdb_game = await resolve_game(row["name"], platform_hint)
            if igdb_game is not None:
                await _apply_igdb_metadata(row["id"], igdb_game)
                await upsert_backfill_platform_release_dates(row["id"], igdb_game)
            else:
                await mark_igdb_checked(row["id"])
            processed += 1
        finally:
            await release_game_claim(row["id"], "igdb_claimed_at")
    return processed
```

- [ ] **Step 4: Expose IGDB as one background worker family**

```python
# gamelib_mcp/data/enrich_bg.py
async def _run_igdb_workers() -> int:
    return await _run_until_quiescent(lambda: igdb.backfill_missing_games(limit=10))
```

- [ ] **Step 5: Add startup coverage proving IGDB can begin while Steam refresh is still active**

```python
async def test_igdb_worker_can_start_while_refresh_is_running(self) -> None:
    stale_at = (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat()
    refresh_started = asyncio.Event()
    refresh_release = asyncio.Event()
    igdb_started = asyncio.Event()

    async def slow_refresh() -> dict:
        refresh_started.set()
        await refresh_release.wait()
        return {"steam": {"games_upserted": 1}}

    async def fake_enrich() -> None:
        igdb_started.set()

    with (
        patch("gamelib_mcp.data.db.init_db", AsyncMock()),
        patch("gamelib_mcp.data.db.get_meta", AsyncMock(return_value=stale_at)),
        patch("gamelib_mcp.main._run_startup_refresh", AsyncMock(side_effect=slow_refresh)),
        patch("gamelib_mcp.data.enrich_bg.background_enrich", AsyncMock(side_effect=fake_enrich)),
    ):
        cm = lifespan(object())
        await cm.__aenter__()
        await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
        await asyncio.wait_for(igdb_started.wait(), timeout=0.1)
        self.assertFalse(refresh_release.is_set())
        refresh_release.set()
        await cm.__aexit__(None, None, None)
```

- [ ] **Step 6: Run IGDB and startup tests**

Run: `python3 -m pytest tests/test_igdb.py tests/test_startup_sync.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gamelib_mcp/data/igdb.py gamelib_mcp/data/enrich_bg.py tests/test_igdb.py tests/test_startup_sync.py
git commit -m "feat: add independent igdb background backfill"
```

### Task 6: Convert Existing Enrichment Families To Claim-Aware Batches

**Files:**
- Modify: `gamelib_mcp/data/enrich_bg.py`
- Modify: `gamelib_mcp/data/steam_store.py`
- Modify: `tests/test_enrich_bg.py`

- [ ] **Step 1: Write the failing no-duplicate-store-processing test**

```python
async def test_store_batch_skips_rows_already_claimed(self) -> None:
    with (
        patch("gamelib_mcp.data.enrich_bg.claim_steam_platform_ids_for_store", AsyncMock(side_effect=[[11], []])),
        patch("gamelib_mcp.data.enrich_bg.load_store_batch_rows", AsyncMock(return_value=[{"appid": 10, "name": "Portal 2"}])),
        patch("gamelib_mcp.data.enrich_bg.enrich_game", AsyncMock()) as enrich_game,
    ):
        processed = await enrich_bg._run_store_batch()

    self.assertEqual(processed, 1)
    enrich_game.assert_awaited_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_enrich_bg.py -k duplicate_store_processing -v`
Expected: FAIL because store batches are still selected by plain `SELECT` without claims.

- [ ] **Step 3: Convert each family to `claim -> load rows -> network -> finalize`**

```python
# gamelib_mcp/data/enrich_bg.py
async def _run_store_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_store(limit=50, stale_before=_claim_cutoff_iso())
    rows = await load_store_batch_rows(claimed_ids)
    if not rows:
        return 0
    async with httpx.AsyncClient() as client:
        for row in rows:
            success = True
            try:
                await enrich_game(row["appid"], client=client)
            except Exception:
                success = False
            await _finalize_store_claim(row["game_platform_id"], success)
    return len(rows)
```

```python
async def _finalize_store_claim(platform_id: int, success: bool) -> None:
    async with get_db() as db:
        if success:
            await db.execute(
                "UPDATE steam_platform_data SET store_claimed_at = NULL WHERE game_platform_id = ?",
                (platform_id,),
            )
        else:
            await db.execute(
                "UPDATE steam_platform_data SET store_claimed_at = NULL, store_cached_at = 'FAILED' WHERE game_platform_id = ?",
                (platform_id,),
            )
        await db.commit()
```

- [ ] **Step 4: Repeat for HLTB, ProtonDB, SteamSpy, OpenCritic, and Metacritic**

```python
async def _run_hltb_batch() -> int:
    claimed_ids = await claim_game_ids_for_hltb(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_hltb_batch_rows(claimed_ids)
    if not rows:
        return 0

    await asyncio.gather(
        *(get_hltb(row["game_id"], row["name"]) for row in rows),
        return_exceptions=True,
    )
    for row in rows:
        await release_game_claim(row["game_id"], "hltb_claimed_at")
    return len(rows)
```

- [ ] **Step 5: Preserve current source-specific delays and semaphore limits**

```python
await asyncio.sleep(_PROTON_DELAY)
await asyncio.sleep(_OPENCRITIC_DELAY)
await start_gate.wait_turn()
```

- [ ] **Step 6: Run enrichment tests**

Run: `python3 -m pytest tests/test_enrich_bg.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gamelib_mcp/data/enrich_bg.py gamelib_mcp/data/steam_store.py tests/test_enrich_bg.py
git commit -m "feat: add claim-aware bounded enrichment workers"
```

### Task 7: Full Regression Run

**Files:**
- Modify: none
- Test: `tests/test_db_migration.py`
- Test: `tests/test_startup_sync.py`
- Test: `tests/test_igdb.py`
- Test: `tests/test_enrich_bg.py`

- [ ] **Step 1: Run focused regression suite**

Run: `python3 -m pytest tests/test_db_migration.py tests/test_startup_sync.py tests/test_igdb.py tests/test_enrich_bg.py -v`
Expected: PASS

- [ ] **Step 2: Run broader enrichment-adjacent suite**

Run: `python3 -m pytest tests/test_epic.py tests/test_gog.py tests/test_psn.py tests/test_nintendo.py -v`
Expected: PASS

- [ ] **Step 3: Inspect for accidental behavioral regressions**

Run: `python3 -m pytest -q`
Expected: PASS, or any failures are unrelated pre-existing failures documented before merge.

- [ ] **Step 4: Commit**

```bash
git commit --allow-empty -m "chore: verify controlled parallel sync and enrichment"
```
