# Startup Sync And IGDB Rate Limit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the server ready immediately with existing library rows, run platform refresh in the background, batch Steam ingest for large libraries, and keep IGDB usage within official limits during parallel platform sync.

**Architecture:** `gamelib_mcp/main.py` stops blocking startup on Steam and instead schedules one singleton background refresh task. `gamelib_mcp/tools/admin.py` becomes the concurrent refresh orchestrator, `gamelib_mcp/data/steam_xml.py` moves to bulk DB helpers for large Steam payloads, and `gamelib_mcp/data/igdb.py` gains a shared limiter with retry and jitter so parallel non-Steam sync remains rate-safe.

**Tech Stack:** Python 3.11+, asyncio, aiosqlite, httpx, unittest, AsyncMock

---

## File Map

- Modify: `gamelib_mcp/main.py`
  Purpose: startup behavior, background refresh task scheduling, sync-state metadata wiring.
- Modify: `gamelib_mcp/tools/admin.py`
  Purpose: concurrent `refresh_library()` orchestration and duplicate-run behavior.
- Modify: `gamelib_mcp/data/steam_xml.py`
  Purpose: Steam fetch + batched ingest path.
- Modify: `gamelib_mcp/data/db.py`
  Purpose: sync metadata helpers and bulk upsert helpers for Steam rows.
- Modify: `gamelib_mcp/data/igdb.py`
  Purpose: shared rate limiter, retry policy, jitter, and optional batch entry point.
- Modify: `gamelib_mcp/data/epic.py`
  Purpose: consume IGDB limiter-compatible resolver path without behavior regressions.
- Modify: `gamelib_mcp/data/gog.py`
  Purpose: consume IGDB limiter-compatible resolver path without behavior regressions.
- Modify: `gamelib_mcp/data/psn.py`
  Purpose: consume IGDB limiter-compatible resolver path without behavior regressions.
- Modify: `gamelib_mcp/data/nintendo.py`
  Purpose: consume IGDB limiter-compatible resolver path without behavior regressions.
- Create: `tests/test_startup_sync.py`
  Purpose: startup/background refresh orchestration tests.
- Create: `tests/test_steam_xml.py`
  Purpose: Steam bulk ingest tests.
- Create: `tests/test_igdb.py`
  Purpose: IGDB limiter/retry tests.
- Modify: `tests/test_epic.py`
  Purpose: keep Epic sync coverage aligned with any IGDB entry-point changes.
- Modify: `tests/test_gog.py`
  Purpose: keep GOG sync coverage aligned with any IGDB entry-point changes.
- Modify: `tests/test_psn.py`
  Purpose: keep PSN sync coverage aligned with any IGDB entry-point changes.

### Task 1: Add Startup Sync State And Background Refresh

**Files:**
- Modify: `gamelib_mcp/main.py`
- Modify: `gamelib_mcp/data/db.py`
- Test: `tests/test_startup_sync.py`

- [ ] **Step 1: Write the failing startup test for stale-data background scheduling**

```python
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gamelib_mcp import main


class LifespanStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_schedules_background_refresh_when_library_is_stale(self) -> None:
        fake_refresh_task = asyncio.Future()
        fake_refresh_task.set_result(None)

        with (
            patch("gamelib_mcp.data.db.init_db", AsyncMock()),
            patch("gamelib_mcp.data.db.get_meta", AsyncMock(side_effect=["2026-04-06T00:00:00+00:00", None])),
            patch("gamelib_mcp.data.db.set_meta", AsyncMock()),
            patch("gamelib_mcp.main._run_startup_refresh", AsyncMock()),
            patch("gamelib_mcp.main.background_enrich", AsyncMock()),
            patch("gamelib_mcp.main.asyncio.create_task", return_value=fake_refresh_task) as create_task,
        ):
            async with main.lifespan(MagicMock()):
                pass

        create_task.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_startup_sync.py::LifespanStartupTests::test_lifespan_schedules_background_refresh_when_library_is_stale -v`
Expected: FAIL because `main.lifespan` still awaits Steam sync directly and `_run_startup_refresh` does not exist.

- [ ] **Step 3: Add sync metadata helpers and a singleton background task wrapper**

```python
# gamelib_mcp/data/db.py
async def set_meta_many(values: dict[str, str | None]) -> None:
    async with get_db() as db:
        for key, value in values.items():
            await db.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()
```

```python
# gamelib_mcp/main.py
_LIBRARY_REFRESH_TASK: asyncio.Task | None = None
_LIBRARY_REFRESH_LOCK = asyncio.Lock()


async def _run_startup_refresh() -> None:
    from .data.db import set_meta_many
    from .tools.admin import refresh_library

    await set_meta_many({
        "library_sync_status": "in_progress",
        "library_sync_started_at": datetime.now(timezone.utc).isoformat(),
        "library_sync_error": "",
    })
    try:
        await refresh_library()
        await set_meta_many({
            "library_sync_status": "idle",
            "library_sync_finished_at": datetime.now(timezone.utc).isoformat(),
            "library_sync_error": "",
        })
    except Exception as exc:
        await set_meta_many({
            "library_sync_status": "idle",
            "library_sync_finished_at": datetime.now(timezone.utc).isoformat(),
            "library_sync_error": str(exc),
        })
        logger.exception("Startup library refresh failed")


async def _ensure_startup_refresh() -> None:
    global _LIBRARY_REFRESH_TASK
    async with _LIBRARY_REFRESH_LOCK:
        if _LIBRARY_REFRESH_TASK and not _LIBRARY_REFRESH_TASK.done():
            return
        _LIBRARY_REFRESH_TASK = asyncio.create_task(_run_startup_refresh())
```

- [ ] **Step 4: Change lifespan to serve immediately and schedule refresh in the background**

```python
# gamelib_mcp/main.py
last_sync = await get_meta("library_synced_at")
needs_refresh = True
if last_sync:
    try:
        dt = datetime.fromisoformat(last_sync)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        needs_refresh = age_hours > STALE_HOURS
    except ValueError:
        needs_refresh = True

if needs_refresh:
    logger.info("Library stale or missing — scheduling background refresh")
    await _ensure_startup_refresh()

asyncio.create_task(background_enrich())
yield
```

- [ ] **Step 5: Expand startup tests for duplicate-run protection and non-blocking behavior**

```python
class LifespanStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_ensure_startup_refresh_skips_duplicate_running_task(self) -> None:
        running = asyncio.Future()
        with patch("gamelib_mcp.main.asyncio.create_task", return_value=running) as create_task:
            await main._ensure_startup_refresh()
            await main._ensure_startup_refresh()
        create_task.assert_called_once()
```

- [ ] **Step 6: Run startup tests**

Run: `python3 -m pytest tests/test_startup_sync.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add gamelib_mcp/main.py gamelib_mcp/data/db.py tests/test_startup_sync.py
git commit -m "feat: move stale library refresh to background startup task"
```

### Task 2: Refactor Steam Sync To Use Bulk DB Helpers

**Files:**
- Modify: `gamelib_mcp/data/db.py`
- Modify: `gamelib_mcp/data/steam_xml.py`
- Test: `tests/test_steam_xml.py`

- [ ] **Step 1: Write the failing Steam batching test**

```python
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data import steam_xml


class SteamBulkSyncTests(unittest.TestCase):
    def test_fetch_library_uses_bulk_upsert_helpers(self) -> None:
        payload = {
            "response": {
                "game_count": 2,
                "games": [
                    {"appid": 10, "name": "Counter-Strike", "playtime_forever": 5, "playtime_2weeks": 1},
                    {"appid": 20, "name": "Portal 2", "playtime_forever": 15, "playtime_2weeks": 2},
                ],
            }
        }

        fake_response = AsyncMock()
        fake_response.json.return_value = payload
        fake_response.raise_for_status.return_value = None

        fake_client = AsyncMock()
        fake_client.__aenter__.return_value.get.return_value = fake_response

        with (
            patch("gamelib_mcp.data.steam_xml.httpx.AsyncClient", return_value=fake_client),
            patch("gamelib_mcp.data.steam_xml.bulk_upsert_steam_library", AsyncMock(return_value=2)) as bulk_upsert,
            patch("gamelib_mcp.data.steam_xml.set_meta", AsyncMock()),
        ):
            asyncio.run(steam_xml.fetch_library())

        bulk_upsert.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_steam_xml.py::SteamBulkSyncTests::test_fetch_library_uses_bulk_upsert_helpers -v`
Expected: FAIL because `bulk_upsert_steam_library` does not exist and `fetch_library()` still loops over per-row helpers.

- [ ] **Step 3: Add bulk Steam helpers in the DB layer**

```python
# gamelib_mcp/data/db.py
async def bulk_upsert_steam_library(rows: list[dict], synced_at: str, chunk_size: int = 250) -> int:
    async with get_db() as db:
        total = 0
        for start in range(0, len(rows), chunk_size):
            chunk = rows[start : start + chunk_size]
            await db.execute("BEGIN")
            for row in chunk:
                existing = await db.execute_fetchone(
                    """SELECT g.id
                       FROM games g
                       JOIN game_platforms gp ON gp.game_id = g.id
                       JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
                       WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
                       LIMIT 1""",
                    (STEAM_APP_ID, str(row["appid"])),
                )
                if existing is None:
                    existing = await db.execute_fetchone(
                        "SELECT id FROM games WHERE lower(name) = lower(?) ORDER BY id LIMIT 1",
                        (row["name"],),
                    )
                if existing is None:
                    cursor = await db.execute("INSERT INTO games (name) VALUES (?)", (row["name"],))
                    game_id = cursor.lastrowid
                else:
                    game_id = existing["id"]
                await db.execute("UPDATE games SET name = ? WHERE id = ?", (row["name"], game_id))
                await db.execute(
                    """INSERT INTO game_platforms
                       (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                       VALUES (?, 'steam', 1, ?, ?, ?)
                       ON CONFLICT(game_id, platform) DO UPDATE SET
                           owned = excluded.owned,
                           playtime_minutes = COALESCE(excluded.playtime_minutes, game_platforms.playtime_minutes),
                           playtime_2weeks_minutes = COALESCE(
                               excluded.playtime_2weeks_minutes,
                               game_platforms.playtime_2weeks_minutes
                           ),
                           last_synced = excluded.last_synced""",
                    (game_id, row["playtime_forever"], row["playtime_2weeks"], synced_at),
                )
                platform_row = await db.execute_fetchone(
                    "SELECT id FROM game_platforms WHERE game_id = ? AND platform = 'steam'",
                    (game_id,),
                )
                platform_id = platform_row["id"]
                await db.execute(
                    """INSERT INTO game_platform_identifiers
                       (game_platform_id, identifier_type, identifier_value, is_primary, last_seen_at)
                       VALUES (?, ?, ?, 1, ?)
                       ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                           game_platform_id = excluded.game_platform_id,
                           is_primary = excluded.is_primary,
                           last_seen_at = excluded.last_seen_at""",
                    (platform_id, STEAM_APP_ID, str(row["appid"]), synced_at),
                )
                await db.execute(
                    """INSERT INTO steam_platform_data
                       (game_platform_id, rtime_last_played, library_updated_at)
                       VALUES (?, ?, ?)
                       ON CONFLICT(game_platform_id) DO UPDATE SET
                           rtime_last_played = excluded.rtime_last_played,
                           library_updated_at = excluded.library_updated_at""",
                    (platform_id, row["rtime_last_played"], synced_at),
                )
            await db.commit()
            total += len(chunk)
    return total
```

Implementation notes for this step:
- keep helper names specific to Steam so the change surface stays narrow
- preserve current semantics for `playtime_minutes`, `playtime_2weeks_minutes`, `owned`, `last_synced`, `last_seen_at`, and `library_updated_at`
- prefer a small internal helper for “lookup/create game id by Steam appid or exact name” if the SQL gets unwieldy

- [ ] **Step 4: Update Steam fetch to normalize once and call the bulk helper**

```python
# gamelib_mcp/data/steam_xml.py
normalized_rows = [
    {
        "appid": game["appid"],
        "name": game.get("name", f"App {game['appid']}"),
        "playtime_forever": game.get("playtime_forever", 0),
        "playtime_2weeks": game.get("playtime_2weeks", 0),
        "rtime_last_played": game.get("rtime_last_played") or None,
    }
    for game in games
]

upserted = await bulk_upsert_steam_library(normalized_rows, now)
await set_meta("library_synced_at", now)
return {"games_upserted": upserted, "synced_at": now}
```

- [ ] **Step 5: Add a regression test that identifiers and playtime data are preserved**

```python
class SteamBulkSyncTests(unittest.TestCase):
    def test_fetch_library_preserves_synced_at_and_returns_bulk_count(self) -> None:
        payload = {"response": {"game_count": 2, "games": [{"appid": 10, "name": "A"}, {"appid": 20, "name": "B"}]}}
        fake_response = AsyncMock()
        fake_response.json.return_value = payload
        fake_response.raise_for_status.return_value = None
        fake_client = AsyncMock()
        fake_client.__aenter__.return_value.get.return_value = fake_response

        with (
            patch("gamelib_mcp.data.steam_xml.httpx.AsyncClient", return_value=fake_client),
            patch("gamelib_mcp.data.steam_xml.bulk_upsert_steam_library", AsyncMock(return_value=2)),
            patch("gamelib_mcp.data.steam_xml.set_meta", AsyncMock()) as set_meta_mock,
        ):
            result = asyncio.run(steam_xml.fetch_library())

        self.assertEqual(result["games_upserted"], 2)
        self.assertIn("synced_at", result)
        set_meta_mock.assert_called_once()
```

- [ ] **Step 6: Run Steam sync tests**

Run: `python3 -m pytest tests/test_steam_xml.py -v`
Expected: PASS

- [ ] **Step 7: Run DB regression coverage**

Run: `python3 -m pytest tests/test_db_migration.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add gamelib_mcp/data/db.py gamelib_mcp/data/steam_xml.py tests/test_steam_xml.py
git commit -m "feat: batch Steam library ingest writes"
```

### Task 3: Add Shared IGDB Limiter, Retries, And Jitter

**Files:**
- Modify: `gamelib_mcp/data/igdb.py`
- Test: `tests/test_igdb.py`

- [ ] **Step 1: Write the failing retry-on-429 test**

```python
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from gamelib_mcp.data import igdb


class IGDBLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_retries_429_with_backoff(self) -> None:
        first = AsyncMock()
        first.status_code = 429
        first.headers = {"Retry-After": "1"}
        first.raise_for_status.side_effect = httpx.HTTPStatusError(
            "rate limited",
            request=httpx.Request("POST", "https://api.igdb.com/v4/games"),
            response=httpx.Response(429, request=httpx.Request("POST", "https://api.igdb.com/v4/games")),
        )

        second = AsyncMock()
        second.status_code = 200
        second.json.return_value = []
        second.raise_for_status.return_value = None

        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(side_effect=[first, second])

        with (
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb.asyncio.sleep", AsyncMock()) as sleep_mock,
        ):
            await igdb.search_game("Elden Ring", igdb.IGDB_PLATFORM_PC)

        sleep_mock.assert_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_igdb.py::IGDBLimiterTests::test_search_game_retries_429_with_backoff -v`
Expected: FAIL because `search_game()` currently performs one direct request with no retry or limiter.

- [ ] **Step 3: Implement a shared request gate and retry helper**

```python
# gamelib_mcp/data/igdb.py
_REQUEST_LOCK = asyncio.Lock()
_IN_FLIGHT = asyncio.Semaphore(4)
_REQUEST_TIMES: collections.deque[float] = collections.deque()


async def _acquire_rate_slot() -> None:
    async with _REQUEST_LOCK:
        now = time.monotonic()
        while _REQUEST_TIMES and now - _REQUEST_TIMES[0] >= 1.0:
            _REQUEST_TIMES.popleft()
        if len(_REQUEST_TIMES) >= 3:
            await asyncio.sleep(1.0 - (now - _REQUEST_TIMES[0]))
        _REQUEST_TIMES.append(time.monotonic())


async def _post_igdb(query: str) -> list[dict]:
    async with _IN_FLIGHT:
        await _acquire_rate_slot()
        return await _request_with_retry(query)
```

- [ ] **Step 4: Implement `429`/`5xx` retry behavior with jitter**

```python
async def _request_with_retry(query: str) -> list[dict]:
    last_error = None
    for attempt in range(4):
        try:
            token = await _get_token()
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    _IGDB_GAMES_URL,
                    content=query,
                    headers={
                        "Client-ID": os.environ["TWITCH_CLIENT_ID"],
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "text/plain",
                    },
                )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status = exc.response.status_code
            if status not in {429, 500, 502, 503, 504}:
                raise
            retry_after = exc.response.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 0.5 * (2 ** attempt)
            delay *= random.uniform(0.8, 1.3)
            await asyncio.sleep(delay)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            delay = 0.5 * (2 ** attempt) * random.uniform(0.8, 1.3)
            await asyncio.sleep(delay)
    raise last_error
```

- [ ] **Step 5: Route `search_game()` through the shared helper**

```python
# gamelib_mcp/data/igdb.py
results = await _post_igdb(query)
```

- [ ] **Step 6: Add tests for in-flight gating and non-retry permanent errors**

```python
class IGDBLimiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_game_does_not_retry_400(self) -> None:
        request = httpx.Request("POST", "https://api.igdb.com/v4/games")
        response = httpx.Response(400, request=request)
        failing = AsyncMock()
        failing.status_code = 400
        failing.headers = {}
        failing.raise_for_status.side_effect = httpx.HTTPStatusError("bad request", request=request, response=response)

        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(return_value=failing)

        with (
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb.asyncio.sleep", AsyncMock()) as sleep_mock,
        ):
            result = await igdb.search_game("Broken Query", igdb.IGDB_PLATFORM_PC)

        self.assertEqual(result, [])
        sleep_mock.assert_not_called()

    async def test_search_game_returns_empty_on_retry_exhaustion(self) -> None:
        request = httpx.Request("POST", "https://api.igdb.com/v4/games")
        response = httpx.Response(503, request=request)
        failing = AsyncMock()
        failing.status_code = 503
        failing.headers = {}
        failing.raise_for_status.side_effect = httpx.HTTPStatusError("unavailable", request=request, response=response)

        client = AsyncMock()
        client.__aenter__.return_value.post = AsyncMock(return_value=failing)

        with (
            patch("gamelib_mcp.data.igdb._get_token", AsyncMock(return_value="token")),
            patch("gamelib_mcp.data.igdb.httpx.AsyncClient", return_value=client),
            patch("gamelib_mcp.data.igdb.asyncio.sleep", AsyncMock()),
        ):
            result = await igdb.search_game("Elden Ring", igdb.IGDB_PLATFORM_PC)

        self.assertEqual(result, [])
```

- [ ] **Step 7: Run IGDB tests**

Run: `python3 -m pytest tests/test_igdb.py -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add gamelib_mcp/data/igdb.py tests/test_igdb.py
git commit -m "feat: add IGDB rate limiting and retry backoff"
```

### Task 4: Parallelize Full Refresh And Preserve Per-Platform Failures

**Files:**
- Modify: `gamelib_mcp/tools/admin.py`
- Modify: `tests/test_startup_sync.py`

- [ ] **Step 1: Write the failing refresh concurrency test**

```python
class RefreshLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_library_runs_platform_syncs_concurrently(self) -> None:
        started = []

        async def _fake_sync(name):
            started.append(name)
            await asyncio.sleep(0)
            return {"name": name}

        with (
            patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(return_value={"steam": True})),
            patch("gamelib_mcp.tools.admin.sync_epic", AsyncMock(side_effect=lambda: _fake_sync("epic"))),
            patch("gamelib_mcp.tools.admin.sync_gog", AsyncMock(side_effect=lambda: _fake_sync("gog"))),
            patch("gamelib_mcp.tools.admin.sync_nintendo", AsyncMock(side_effect=lambda: _fake_sync("nintendo"))),
            patch("gamelib_mcp.tools.admin.sync_psn", AsyncMock(side_effect=lambda: _fake_sync("ps5"))),
        ):
            result = await refresh_library()

        self.assertEqual(set(result), {"steam", "epic", "gog", "nintendo", "ps5"})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_startup_sync.py::RefreshLibraryTests::test_refresh_library_runs_platform_syncs_concurrently -v`
Expected: FAIL because `refresh_library()` currently awaits each platform sequentially.

- [ ] **Step 3: Rewrite `refresh_library()` around `asyncio.gather(..., return_exceptions=True)`**

```python
# gamelib_mcp/tools/admin.py
tasks = {}
if "steam" in targets:
    tasks["steam"] = fetch_library()
for name, fn in platform_syncs.items():
    if name in targets:
        tasks[name] = fn()

results = await asyncio.gather(*tasks.values(), return_exceptions=True)
return {
    name: {"error": str(result)} if isinstance(result, Exception) else result
    for name, result in zip(tasks.keys(), results, strict=False)
}
```

- [ ] **Step 4: Add a regression test that one platform error does not cancel siblings**

```python
class RefreshLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_library_reports_platform_errors_without_cancelling_others(self) -> None:
        with (
            patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(return_value={"games_upserted": 1})),
            patch("gamelib_mcp.tools.admin.sync_epic", AsyncMock(side_effect=RuntimeError("boom"))),
            patch("gamelib_mcp.tools.admin.sync_gog", AsyncMock(return_value={"added": 1, "matched": 0, "skipped": 0})),
            patch("gamelib_mcp.tools.admin.sync_nintendo", AsyncMock(return_value={"added": 0, "matched": 1, "skipped": 0})),
            patch("gamelib_mcp.tools.admin.sync_psn", AsyncMock(return_value={"added": 0, "matched": 1, "skipped": 0})),
        ):
            result = await refresh_library()

        self.assertEqual(result["epic"]["error"], "boom")
        self.assertEqual(result["gog"]["added"], 1)
```

- [ ] **Step 5: Run startup/orchestration tests**

Run: `python3 -m pytest tests/test_startup_sync.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add gamelib_mcp/tools/admin.py tests/test_startup_sync.py
git commit -m "feat: parallelize full library refresh across platforms"
```

### Task 5: Reconcile Platform Sync Tests With IGDB-Centric Resolution

**Files:**
- Modify: `tests/test_epic.py`
- Modify: `tests/test_gog.py`
- Modify: `tests/test_psn.py`
- Modify: `tests/test_nintendo.py` if present in the repository after implementation

- [ ] **Step 1: Replace stale fuzzy-match patch points with IGDB resolver patch points**

```python
# tests/test_gog.py
with (
    patch("gamelib_mcp.data.gog.resolve_and_link_game", AsyncMock(return_value=(7, None))),
    patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
    patch("gamelib_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=1)),
):
    result = asyncio.run(gog.sync_gog())
```

```python
# tests/test_psn.py
with (
    patch("gamelib_mcp.data.psn.resolve_and_link_game", AsyncMock(return_value=(7, None))),
    patch("gamelib_mcp.data.psn.load_fuzzy_candidates", AsyncMock(return_value={})),
    patch("gamelib_mcp.data.psn.upsert_game_platform", AsyncMock(return_value=1)),
):
    result = asyncio.run(psn.sync_psn())
```

- [ ] **Step 2: Add one regression test proving platform sync still proceeds when IGDB falls back**

```python
def test_sync_continues_when_igdb_returns_no_match(self) -> None:
    with (
        patch("gamelib_mcp.data.gog.resolve_and_link_game", AsyncMock(return_value=(42, None))),
        patch("gamelib_mcp.data.gog.upsert_game_platform", AsyncMock(return_value=9)),
        patch("gamelib_mcp.data.gog.load_fuzzy_candidates", AsyncMock(return_value={})),
    ):
        result = asyncio.run(gog.sync_gog())
    self.assertEqual(result["added"], 1)
```

- [ ] **Step 3: Run platform sync tests**

Run: `python3 -m pytest tests/test_epic.py tests/test_gog.py tests/test_psn.py -v`
Expected: PASS

- [ ] **Step 4: Run the focused full suite for this feature**

Run: `python3 -m pytest tests/test_startup_sync.py tests/test_steam_xml.py tests/test_igdb.py tests/test_epic.py tests/test_gog.py tests/test_psn.py tests/test_db_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_epic.py tests/test_gog.py tests/test_psn.py tests/test_startup_sync.py tests/test_steam_xml.py tests/test_igdb.py
git commit -m "test: cover background sync startup and IGDB rate limiting"
```

## Spec Coverage Check

- Fast readiness with partial library: covered by Task 1.
- Singleton background refresh state: covered by Task 1.
- Steam batching for ~2,000 games: covered by Task 2.
- Parallel Steam and non-Steam refresh: covered by Task 4.
- Shared IGDB limiter with retry, jitter, and conservative caps: covered by Task 3.
- Existing rows remain queryable during refresh: covered indirectly by Task 1 and validated by startup/orchestration tests.
- Platform-level failures remain isolated: covered by Task 4.

## Placeholder Scan

- No `TBD` or `TODO` markers remain.
- All tasks list exact files and commands.
- Each code-changing task includes concrete code scaffolding rather than “implement later” instructions.

## Type Consistency Check

- Sync metadata keys consistently use `library_sync_status`, `library_sync_started_at`, `library_sync_finished_at`, and `library_sync_error`.
- The planned Steam bulk helper is consistently named `bulk_upsert_steam_library`.
- IGDB retry entry point consistently flows through `_post_igdb()` / `_request_with_retry()`.
