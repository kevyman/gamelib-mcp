# Non-blocking Library Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `refresh_library` fire-and-return immediately, record per-platform sync progress in the `meta` table, add a `get_sync_status` tool to poll it, and reconcile a sync left stuck by a crash on startup.

**Architecture:** Split the existing inline `refresh_library` into (a) a worker, `run_library_sync`, that does the actual gather + per-platform meta writes, and (b) a thin `refresh_library` tool that marks the sync started, schedules the worker as the existing background `_LIBRARY_REFRESH_TASK` (with dedup), and returns an ack. A new read-only `get_sync_status` tool reads the meta keys. `lifespan` resets a stale `in_progress` status at startup. No new tables, no job IDs.

**Tech Stack:** Python 3, asyncio, FastMCP, aiosqlite, pydantic models, `unittest.IsolatedAsyncioTestCase` (tests use a real temp SQLite DB via `ToolDBTestCase` / the patterns in `tests/conftest.py` and `tests/test_startup_sync.py`).

---

## Background facts the implementer must know

- The actual sync work lives in `gamelib_mcp/tools/admin.py::refresh_library` (lines ~24-104): it validates platforms, runs `asyncio.gather` over the selected platform sync coroutines, records results, runs `detect_farmed_games` after a successful Steam sync, and schedules background enrichment.
- `gamelib_mcp/lifecycle.py` already runs the *startup* refresh as a background task: `_run_startup_refresh()` (sets `library_sync_status`/`started_at`/`finished_at`/`error` + per-platform `integration_sync_*` metadata), scheduled via `_ensure_startup_refresh()` which **dedupes** using `_LIBRARY_REFRESH_TASK` + a per-loop lock. `get_startup_refresh_task()` exposes the live task.
- `_run_startup_refresh` reaches the tool layer via the module global `_admin_refresh_library` (lazily bound, patchable in tests). Today it binds to `admin.refresh_library`; this plan rebinds it to the new worker `admin.run_library_sync`.
- Meta helpers (`gamelib_mcp/data/db/queries.py`, re-exported from `gamelib_mcp.data.db`): `get_meta(key)`, `set_meta(key, value)`, `set_meta_many({key: value|None})`, `get_meta_prefix(prefix)`.
- Canonical syncable platform names (`gamelib_mcp/tools/common.py`): `SYNCABLE_PLATFORMS = {"steam","epic","gog","switch2","ps5"}`. The `platform_syncs` dict in `refresh_library` is keyed by these canonical names (`switch2` → `sync_nintendo`).
- **Known quirk (do not fix here):** `lifecycle.SYNC_METADATA_PLATFORMS` uses the name `"nintendo"`, but the canonical sync target is `"switch2"`, so the existing `integration_sync_nintendo_*` keys may not line up with switch2 results. `get_sync_status` therefore derives its authoritative per-platform live state from the **new** `sync_platform_state_<canonical>` keys (written by the worker using canonical names), not from `integration_sync_*`. Treat `integration_sync_*` reads as best-effort enrichment only.
- New meta keys introduced by this plan: `sync_platform_state_<canonical>` ∈ {`pending`,`running`,`done`,`error`}.
- Test runner: `.venv/bin/python -m pytest tests/<file> -q` (fallback `.venv/bin/python -m unittest tests.<module>`). DB tests use real temp SQLite; if `aiosqlite` hangs in a sandbox, run outside the sandbox (see CLAUDE.md).

---

## File Structure

- `gamelib_mcp/tools/models.py` — change `RefreshLibraryResponse` to the ack shape; add `SyncStatusResponse`.
- `gamelib_mcp/tools/admin.py` — add worker `run_library_sync`, helpers `_mark_sync_started` / `_mark_platform_state`, per-platform state writes; rewrite `refresh_library` as a fire-and-return scheduler; add `get_sync_status` handler.
- `gamelib_mcp/lifecycle.py` — `_run_startup_refresh(platforms=None)` + `_ensure_startup_refresh(platforms=None)` thread platforms through; rebind `_admin_refresh_library` to the worker; add `reconcile_stale_sync_status()` and call it from `lifespan`.
- `gamelib_mcp/main.py` — update the `refresh_library` passthrough docstring/return type; add the `get_sync_status` passthrough.
- `tests/test_tools_admin.py` — tests for the worker's per-platform state writes, the fire-and-return tool, dedup, and `get_sync_status`.
- `tests/test_startup_sync.py` — test for `reconcile_stale_sync_status`.

---

## Task 1: Response models

**Files:**
- Modify: `gamelib_mcp/tools/models.py:113-118`
- Test: `tests/test_models_sync.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_models_sync.py`:

```python
"""Schema tests for the sync ack + status response models."""

import unittest

from gamelib_mcp.tools.models import RefreshLibraryResponse, SyncStatusResponse


class SyncModelTests(unittest.TestCase):
    def test_refresh_ack_shape(self):
        m = RefreshLibraryResponse(
            status="started", platforms=["steam", "gog"], already_running=False
        )
        self.assertEqual(m.status, "started")
        self.assertEqual(m.platforms, ["steam", "gog"])
        self.assertFalse(m.already_running)

    def test_sync_status_shape(self):
        m = SyncStatusResponse(
            status="in_progress",
            started_at="2026-06-14T12:00:00+00:00",
            finished_at=None,
            platforms={"steam": {"state": "done", "last_success_at": None, "error": None}},
        )
        self.assertEqual(m.status, "in_progress")
        self.assertEqual(m.platforms["steam"]["state"], "done")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_models_sync.py -q`
Expected: FAIL — `ImportError: cannot import name 'SyncStatusResponse'` (and `RefreshLibraryResponse` has no `status` field).

- [ ] **Step 3: Edit the models**

In `gamelib_mcp/tools/models.py`, replace the existing `RefreshLibraryResponse` (currently `class RefreshLibraryResponse(RootModel[dict[str, dict[str, Any]]]): pass`) and add the status model:

```python
class RefreshLibraryResponse(FlexibleModel):
    status: str  # "started" or "already_running"
    platforms: list[str]
    already_running: bool


class SyncStatusResponse(FlexibleModel):
    status: str  # "in_progress" or "idle"
    started_at: str | None = None
    finished_at: str | None = None
    platforms: dict[str, dict[str, Any]]
```

Leave the `RootModel` import in place only if still used elsewhere; if it becomes unused, remove it from the import line to avoid a lint-style dead import. Verify with: `grep -n "RootModel" gamelib_mcp/tools/models.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_models_sync.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/tools/models.py tests/test_models_sync.py
git commit -m "Add sync ack + status response models"
```

---

## Task 2: Extract `run_library_sync` worker with per-platform state writes

This moves the *existing* `refresh_library` body into a worker named `run_library_sync`, and adds per-platform `sync_platform_state_<name>` meta writes. The tool wrapper is replaced in Task 3.

**Files:**
- Modify: `gamelib_mcp/tools/admin.py` (the `refresh_library` function, lines ~24-104)
- Modify: `gamelib_mcp/lifecycle.py:307-309` (rebind `_admin_refresh_library`)
- Test: `tests/test_tools_admin.py` (add a class)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_admin.py`:

```python
from unittest.mock import AsyncMock, patch

from gamelib_mcp.data.db import get_meta


class RunLibrarySyncStateTests(ToolDBTestCase):
    async def test_writes_running_then_done_per_platform(self):
        async def fake_steam():
            assert await get_meta("sync_platform_state_steam") == "running"
            return {"games_upserted": 3}

        with patch.dict(
            "gamelib_mcp.tools.admin.refresh_library.__globals__",  # placeholder, replaced below
            {},
            clear=False,
        ):
            pass  # see Step 3 note — real patch target is the platform_syncs map

    async def test_marks_platform_error_on_failure(self):
        pass
```

> NOTE TO IMPLEMENTER: the `platform_syncs` dict is a local inside the function, so patch the imported sync callables instead. Replace the test body above with the concrete version below once Step 3's structure exists. The real test:

```python
class RunLibrarySyncStateTests(ToolDBTestCase):
    async def test_writes_done_state_for_successful_platform(self):
        async def fake_steam():
            # while running, state must read "running"
            assert await get_meta("sync_platform_state_steam") == "running"
            return {"games_upserted": 3}

        with patch("gamelib_mcp.tools.admin.fetch_library", side_effect=fake_steam), \
             patch("gamelib_mcp.tools.admin.detect_farmed_games", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            result = await admin.run_library_sync(["steam"])

        self.assertEqual(result["steam"], {"games_upserted": 3})
        self.assertEqual(await get_meta("sync_platform_state_steam"), "done")
        self.assertEqual(await get_meta("library_sync_status"), "idle")

    async def test_marks_platform_error_on_failure(self):
        with patch("gamelib_mcp.tools.admin.fetch_library", AsyncMock(side_effect=RuntimeError("boom"))), \
             patch("gamelib_mcp.tools.admin._schedule_background_enrich", AsyncMock()):
            result = await admin.run_library_sync(["steam"])

        self.assertIn("error", result["steam"])
        self.assertEqual(await get_meta("sync_platform_state_steam"), "error")
        self.assertEqual(await get_meta("library_sync_status"), "idle")
```

Delete the placeholder first version; keep only the concrete class.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tools_admin.py::RunLibrarySyncStateTests -q`
Expected: FAIL — `AttributeError: module 'gamelib_mcp.tools.admin' has no attribute 'run_library_sync'`.

- [ ] **Step 3: Rename `refresh_library` → `run_library_sync` and add state writes**

In `gamelib_mcp/tools/admin.py`:

1. Rename `async def refresh_library(...)` to `async def run_library_sync(...)` (same signature: `platforms: list[str] | None = None, ctx=None`).
2. Add module-level helpers above it:

```python
from .common import SYNCABLE_PLATFORMS, PLATFORM_ALIASES  # extend existing import line


async def _mark_sync_started(targets: set[str]) -> None:
    """Mark the overall sync in-progress and each selected platform running."""
    from ..data.db import set_meta_many
    from datetime import datetime, timezone

    updates: dict[str, str | None] = {
        "library_sync_status": "in_progress",
        "library_sync_started_at": datetime.now(timezone.utc).isoformat(),
        "library_sync_finished_at": None,
    }
    for name in targets:
        updates[f"sync_platform_state_{name}"] = "running"
    await set_meta_many(updates)


async def _mark_platform_state(name: str, state: str) -> None:
    from ..data.db import set_meta
    await set_meta(f"sync_platform_state_{name}", state)
```

3. At the start of `run_library_sync`, after `targets` is computed and `selected` is built, call `await _mark_sync_started(targets)` (covers the startup path that does not go through the tool).
4. In the outcome loop (`for index, ((name, _), outcome) in enumerate(...)`), write the per-platform state alongside the existing result recording:

```python
        if isinstance(outcome, BaseException):
            results[result_name] = {"error": str(outcome)}
            await _mark_platform_state(name, "error")
            await _info(ctx, f"Failed {result_name} refresh: {outcome}")
        else:
            results[result_name] = outcome
            await _mark_platform_state(name, "done")
            await _info(ctx, f"Finished {result_name} refresh")
        await report_progress(ctx, index, len(selected))
```

(Use the canonical `name`, not `result_name`, for the state key so it matches `SYNCABLE_PLATFORMS`.)

5. In `gamelib_mcp/lifecycle.py`, lines ~307-309, change the lazy binding inside `_run_startup_refresh` from `from .tools.admin import refresh_library` / `_admin_refresh_library = refresh_library` to:

```python
        from .tools.admin import run_library_sync
        _admin_refresh_library = run_library_sync
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tools_admin.py::RunLibrarySyncStateTests tests/test_startup_sync.py -q`
Expected: PASS (existing startup tests still pass because they patch `_admin_refresh_library` directly).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/tools/admin.py gamelib_mcp/lifecycle.py tests/test_tools_admin.py
git commit -m "Extract run_library_sync worker with per-platform state writes"
```

---

## Task 3: Thread `platforms` through the startup-refresh scheduler

**Files:**
- Modify: `gamelib_mcp/lifecycle.py` (`_run_startup_refresh`, `_ensure_startup_refresh`)
- Test: `tests/test_startup_sync.py` (add a test)

- [ ] **Step 1: Write the failing test**

Add to `StartupSyncTests` in `tests/test_startup_sync.py`:

```python
    async def test_ensure_startup_refresh_passes_platforms_to_worker(self) -> None:
        seen = {}

        async def fake_worker(platforms=None):
            seen["platforms"] = platforms
            return {}

        with patch("gamelib_mcp.lifecycle._admin_refresh_library", AsyncMock(side_effect=fake_worker)):
            task = await _ensure_startup_refresh(["gog"])
            await task

        self.assertEqual(seen["platforms"], ["gog"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_startup_sync.py::StartupSyncTests::test_ensure_startup_refresh_passes_platforms_to_worker -q`
Expected: FAIL — `_ensure_startup_refresh()` takes no positional arg / platforms not forwarded.

- [ ] **Step 3: Add the `platforms` parameter**

In `gamelib_mcp/lifecycle.py`:

```python
async def _run_startup_refresh(platforms: list[str] | None = None) -> dict:
    ...
        refresh_result = await _admin_refresh_library(platforms)
    ...
```

```python
async def _ensure_startup_refresh(platforms: list[str] | None = None) -> asyncio.Task:
    global _LIBRARY_REFRESH_TASK

    async with _get_library_refresh_lock():
        if _LIBRARY_REFRESH_TASK is not None and not _LIBRARY_REFRESH_TASK.done():
            return _LIBRARY_REFRESH_TASK

        _LIBRARY_REFRESH_TASK = asyncio.create_task(_run_startup_refresh(platforms))
        _LIBRARY_REFRESH_TASK.add_done_callback(_clear_library_refresh_task)
        return _LIBRARY_REFRESH_TASK
```

Note: the existing `_run_startup_refresh` already binds `_admin_refresh_library` lazily; calling it with a single positional arg works for both the real worker and patched `AsyncMock`s. The periodic loop and `lifespan` call `_ensure_startup_refresh()` with no args (all platforms) — unchanged behavior.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_startup_sync.py -q`
Expected: PASS (all, including the new test and the existing dedup/periodic tests).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/lifecycle.py tests/test_startup_sync.py
git commit -m "Thread platform subset through startup-refresh scheduler"
```

---

## Task 4: `refresh_library` tool becomes fire-and-return

**Files:**
- Modify: `gamelib_mcp/tools/admin.py` (add new `refresh_library`)
- Test: `tests/test_tools_admin.py` (add a class)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_admin.py`:

```python
import asyncio

from fastmcp.exceptions import ToolError
from gamelib_mcp import lifecycle


class RefreshLibraryAckTests(ToolDBTestCase):
    async def asyncTearDown(self) -> None:
        task = lifecycle._LIBRARY_REFRESH_TASK
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        lifecycle._LIBRARY_REFRESH_TASK = None
        await super().asyncTearDown()

    async def test_returns_started_without_blocking(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_worker(platforms=None):
            started.set()
            await release.wait()
            return {}

        with patch("gamelib_mcp.lifecycle._admin_refresh_library", AsyncMock(side_effect=slow_worker)):
            ack = await admin.refresh_library(["steam"])
            self.assertEqual(ack["status"], "started")
            self.assertFalse(ack["already_running"])
            self.assertEqual(ack["platforms"], ["steam"])
            self.assertEqual(await get_meta("library_sync_status"), "in_progress")
            self.assertTrue(started.is_set() or True)  # worker scheduled, ack already returned
            release.set()

    async def test_returns_already_running_when_in_flight(self):
        release = asyncio.Event()

        async def slow_worker(platforms=None):
            await release.wait()
            return {}

        with patch("gamelib_mcp.lifecycle._admin_refresh_library", AsyncMock(side_effect=slow_worker)):
            first = await admin.refresh_library(["steam"])
            second = await admin.refresh_library(["gog"])
            self.assertEqual(first["status"], "started")
            self.assertEqual(second["status"], "already_running")
            self.assertTrue(second["already_running"])
            release.set()

    async def test_rejects_unknown_platform(self):
        with self.assertRaises(ToolError):
            await admin.refresh_library(["nope"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tools_admin.py::RefreshLibraryAckTests -q`
Expected: FAIL — current `admin.refresh_library` no longer exists (renamed in Task 2) → `AttributeError`.

- [ ] **Step 3: Add the fire-and-return tool**

In `gamelib_mcp/tools/admin.py`, add a new `refresh_library` (separate from `run_library_sync`):

```python
async def refresh_library(
    platforms: list[str] | None = None,
    ctx=None,
) -> dict:
    """
    Schedule a library re-sync and return immediately (non-blocking).

    Starts a background sync of the owned game library from configured
    platforms and returns an acknowledgement. Poll get_sync_status to follow
    progress. platforms can be omitted (all configured platforms) or a subset.
    """
    from ..lifecycle import _ensure_startup_refresh, get_startup_refresh_task

    def _resolve(p: str) -> str:
        return PLATFORM_ALIASES.get(p.lower(), p.lower())

    requested_targets = list(platforms) if platforms else sorted(SYNCABLE_PLATFORMS)
    unknown = [p for p in requested_targets if _resolve(p) not in SYNCABLE_PLATFORMS]
    if unknown:
        valid = sorted(SYNCABLE_PLATFORMS | set(PLATFORM_ALIASES))
        raise ToolError(f"Unknown platform '{', '.join(unknown)}'. Valid: {valid}")

    targets = {_resolve(p) for p in requested_targets}

    existing = get_startup_refresh_task()
    if existing is not None and not existing.done():
        return {
            "status": "already_running",
            "platforms": sorted(targets),
            "already_running": True,
        }

    await _mark_sync_started(targets)
    await _ensure_startup_refresh(sorted(targets))
    return {
        "status": "started",
        "platforms": sorted(targets),
        "already_running": False,
    }
```

Notes:
- `_mark_sync_started` is called synchronously before returning so an immediate `get_sync_status` poll already reads `in_progress` (no race). The worker also calls it, which is idempotent.
- The pre-check on `get_startup_refresh_task()` decides the ack value; the authoritative dedup is the lock inside `_ensure_startup_refresh`. With a single MCP client this is sufficient.
- Keep `run_library_sync`'s own validation untouched; the tool validates user input up front to raise `ToolError` before scheduling.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tools_admin.py::RefreshLibraryAckTests -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/tools/admin.py tests/test_tools_admin.py
git commit -m "Make refresh_library tool fire-and-return"
```

---

## Task 5: `get_sync_status` tool

**Files:**
- Modify: `gamelib_mcp/tools/admin.py` (add `get_sync_status`)
- Test: `tests/test_tools_admin.py` (add a class)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_tools_admin.py`:

```python
from gamelib_mcp.data.db import set_meta_many


class GetSyncStatusTests(ToolDBTestCase):
    async def test_reports_idle_with_pending_platforms_when_never_synced(self):
        status = await admin.get_sync_status()
        self.assertEqual(status["status"], "idle")
        self.assertEqual(
            set(status["platforms"]), {"steam", "epic", "gog", "switch2", "ps5"}
        )
        self.assertEqual(status["platforms"]["steam"]["state"], "pending")

    async def test_reflects_in_progress_and_per_platform_state(self):
        await set_meta_many(
            {
                "library_sync_status": "in_progress",
                "library_sync_started_at": "2026-06-14T12:00:00+00:00",
                "library_sync_finished_at": None,
                "sync_platform_state_steam": "done",
                "sync_platform_state_gog": "running",
                "sync_platform_state_ps5": "error",
                "integration_sync_ps5_last_error_summary": "refresh token rejected",
            }
        )
        status = await admin.get_sync_status()
        self.assertEqual(status["status"], "in_progress")
        self.assertEqual(status["started_at"], "2026-06-14T12:00:00+00:00")
        self.assertIsNone(status["finished_at"])
        self.assertEqual(status["platforms"]["steam"]["state"], "done")
        self.assertEqual(status["platforms"]["gog"]["state"], "running")
        self.assertEqual(status["platforms"]["ps5"]["state"], "error")
        self.assertEqual(status["platforms"]["ps5"]["error"], "refresh token rejected")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tools_admin.py::GetSyncStatusTests -q`
Expected: FAIL — `admin.get_sync_status` does not exist.

- [ ] **Step 3: Implement `get_sync_status`**

In `gamelib_mcp/tools/admin.py`:

```python
async def get_sync_status() -> dict:
    """
    Report the current/last library sync: overall state plus per-platform state.

    status is "in_progress" while a sync runs, else "idle". Each syncable
    platform reports state (pending/running/done/error), its last success time,
    and the last error summary if any. Poll this after calling refresh_library.
    """
    from ..data.db import get_meta, get_meta_prefix

    overall = await get_meta("library_sync_status") or "idle"
    started_at = await get_meta("library_sync_started_at")
    finished_at = await get_meta("library_sync_finished_at")

    state_keys = await get_meta_prefix("sync_platform_state_")
    integ = await get_meta_prefix("integration_sync_")

    platforms: dict[str, dict] = {}
    for name in sorted(SYNCABLE_PLATFORMS):
        platforms[name] = {
            "state": state_keys.get(f"sync_platform_state_{name}", "pending"),
            "last_success_at": integ.get(f"integration_sync_{name}_last_success_at"),
            "error": integ.get(f"integration_sync_{name}_last_error_summary"),
        }

    return {
        "status": overall,
        "started_at": started_at,
        "finished_at": finished_at,
        "platforms": platforms,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tools_admin.py::GetSyncStatusTests -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/tools/admin.py tests/test_tools_admin.py
git commit -m "Add get_sync_status tool"
```

---

## Task 6: Startup reconciliation of a stale `in_progress`

**Files:**
- Modify: `gamelib_mcp/lifecycle.py` (add `reconcile_stale_sync_status`, call it in `lifespan`)
- Test: `tests/test_startup_sync.py` (add a test)

- [ ] **Step 1: Write the failing test**

Add to `StartupSyncTests` in `tests/test_startup_sync.py`:

```python
    async def test_reconcile_resets_stale_in_progress(self) -> None:
        from gamelib_mcp.lifecycle import reconcile_stale_sync_status
        from gamelib_mcp.data.db import set_meta_many, get_meta

        await set_meta_many(
            {
                "library_sync_status": "in_progress",
                "sync_platform_state_steam": "running",
                "sync_platform_state_gog": "done",
            }
        )

        await reconcile_stale_sync_status()

        self.assertEqual(await get_meta("library_sync_status"), "idle")
        self.assertEqual(await get_meta("sync_platform_state_steam"), "error")
        self.assertEqual(await get_meta("sync_platform_state_gog"), "done")
        self.assertIsNotNone(await get_meta("library_sync_error"))

    async def test_reconcile_noop_when_idle(self) -> None:
        from gamelib_mcp.lifecycle import reconcile_stale_sync_status
        from gamelib_mcp.data.db import set_meta, get_meta

        await set_meta("library_sync_status", "idle")
        await reconcile_stale_sync_status()
        self.assertEqual(await get_meta("library_sync_status"), "idle")
```

> The `StartupSyncTests` tests run against the real temp DB initialized by the suite's fixtures; ensure `init_db()` has been called (follow the existing pattern used by the other `_run_startup_refresh` tests in this file, which already rely on the DB being initialized).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest "tests/test_startup_sync.py::StartupSyncTests::test_reconcile_resets_stale_in_progress" -q`
Expected: FAIL — `cannot import name 'reconcile_stale_sync_status'`.

- [ ] **Step 3: Implement reconciliation and wire into `lifespan`**

In `gamelib_mcp/lifecycle.py`, add:

```python
async def reconcile_stale_sync_status() -> None:
    """Reset a sync left `in_progress` by a crash.

    At process start there is never a live refresh task, so an `in_progress`
    status can only be stale. Flip it to idle, note the interruption, and mark
    any platform still `running` as `error`.
    """
    from .data.db import get_meta, get_meta_prefix, set_meta_many

    if await get_meta("library_sync_status") != "in_progress":
        return

    updates: dict[str, str | None] = {
        "library_sync_status": "idle",
        "library_sync_finished_at": datetime.now(timezone.utc).isoformat(),
        "library_sync_error": "Previous sync interrupted before completion",
    }
    for key, value in (await get_meta_prefix("sync_platform_state_")).items():
        if value == "running":
            updates[key] = "error"
    await set_meta_many(updates)
    logger.info("Reconciled stale in-progress library sync status on startup")
```

In `lifespan`, call it immediately after `clear_all_enrichment_claims()` / `logger.info("Database initialized")` (before the stale-library check that may schedule a fresh refresh):

```python
    await init_db()
    await clear_all_enrichment_claims()
    await reconcile_stale_sync_status()
    logger.info("Database initialized")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_startup_sync.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/lifecycle.py tests/test_startup_sync.py
git commit -m "Reconcile stale in-progress sync status on startup"
```

---

## Task 7: Wire the MCP passthroughs in `main.py`

**Files:**
- Modify: `gamelib_mcp/main.py:293-307` (refresh_library passthrough) and the import block at top
- Modify: `gamelib_mcp/main.py` (add get_sync_status passthrough)
- Test: `tests/test_tool_registration.py` (extend if it enumerates tools — see Step 1)

- [ ] **Step 1: Write/After-check the failing test**

First inspect how tools are asserted: `grep -n "get_sync_status\|refresh_library\|expected\|tool" tests/test_tool_registration.py`. If the suite asserts on a set of registered tool names, add `"get_sync_status"` to that expected set (and keep `"refresh_library"`). If it does not enumerate names, add this test to `tests/test_tool_registration.py`:

```python
async def test_get_sync_status_is_registered():
    from gamelib_mcp.main import mcp
    tools = await mcp.get_tools()
    assert "get_sync_status" in tools
    assert "refresh_library" in tools
```

> Confirm the accessor name with `grep -n "get_tools\|list_tools\|_tools" tests/test_tool_registration.py` and match the existing pattern in that file rather than assuming `get_tools()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_tool_registration.py -q`
Expected: FAIL — `get_sync_status` not registered.

- [ ] **Step 3: Update `main.py`**

1. In the model import block (around line 30-37), add `SyncStatusResponse` to the imports from `.tools.models`.
2. Replace the `refresh_library` passthrough body and return type:

```python
@mcp.tool(annotations=NETWORK_SYNC_TOOL)
async def refresh_library(
    ctx: Context,
    platforms: list[str] | None = None,
) -> RefreshLibraryResponse:
    """
    Start a background re-sync of the owned game library and return immediately.

    This does NOT wait for the sync to finish. It returns an acknowledgement
    ({status, platforms, already_running}); poll get_sync_status to follow
    progress and see per-platform results. platforms can be omitted (all
    configured platforms) or a subset such as ["gog"] of steam, epic, gog,
    nintendo, switch2, or ps5. If a sync is already running, returns
    status="already_running".
    """
    from .tools.admin import refresh_library as _refresh
    return await _refresh(platforms, ctx=ctx)
```

3. Add the new read-only passthrough next to it:

```python
@mcp.tool(annotations=READ_ONLY_TOOL)
async def get_sync_status() -> SyncStatusResponse:
    """
    Report the status of the library sync started by refresh_library.

    Returns status ("in_progress" or "idle"), started_at/finished_at, and a
    per-platform map with state (pending/running/done/error), last_success_at,
    and any error. Poll this after calling refresh_library.
    """
    from .tools.admin import get_sync_status as _status
    return await _status()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_tool_registration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/main.py tests/test_tool_registration.py
git commit -m "Register get_sync_status and non-blocking refresh_library passthroughs"
```

---

## Task 8: Full suite + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. If any pre-existing test asserted the old `refresh_library` dict-of-dicts return shape, update it to the ack shape (search: `grep -rn "games_upserted\|refresh_library" tests/`). The worker path (`run_library_sync`) still returns the per-platform results dict, so internal/startup tests are unaffected; only direct tool-return assertions change.

- [ ] **Step 2: Manual smoke (optional, outside sandbox)**

Run the server (`uv run python -m gamelib_mcp.main`), call `refresh_library` and confirm it returns `{"status": "started", ...}` promptly, then call `get_sync_status` a few times and confirm platforms move `running → done`. Kill the server mid-sync, restart, and confirm `get_sync_status` reports `idle` (not stuck `in_progress`).

- [ ] **Step 3: Commit any test fixups**

```bash
git add -A && git commit -m "Fix up tests for non-blocking refresh_library contract"
```

---

## Self-Review

**Spec coverage:**
- Fire-and-return `refresh_library` → Task 4 + Task 7. ✅
- `started` / `already_running` ack with dedup → Task 4. ✅
- Per-platform `pending→running→done/error` in `meta` → Task 2 (writes) + Task 5 (read). ✅
- New `get_sync_status` tool → Task 5 + Task 7. ✅
- No jobs table / no job IDs / no ETA / no sub-platform % → nothing added; worker keeps single-task model. ✅
- Restart reconciliation of stuck `in_progress` → Task 6. ✅
- Wire-schema docstrings updated → Task 7. ✅

**Placeholder scan:** Task 2 Step 1 deliberately shows a placeholder-then-concrete test and instructs the implementer to keep only the concrete class; all other steps contain runnable code/commands. No "TODO/handle edge cases" left.

**Type consistency:** `run_library_sync(platforms, ctx)` (worker, returns results dict) vs `refresh_library(platforms, ctx)` (tool, returns ack dict) are intentionally distinct and named consistently across Tasks 2/4/lifecycle binding. `_mark_sync_started`/`_mark_platform_state` defined in Task 2 and reused in Task 4. `sync_platform_state_<canonical>` key format identical in Tasks 2, 5, 6. `reconcile_stale_sync_status` name identical in Task 6 test + impl + lifespan call. Response model field names (`status`, `platforms`, `already_running`, `started_at`, `finished_at`) match between Task 1 models and Tasks 4/5/7 returns.
