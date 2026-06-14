# Non-blocking library sync with pollable status

**Date:** 2026-06-14
**Status:** Approved (pending implementation plan)

## Problem

When the AI calls `refresh_library`, the tool `await`s `asyncio.gather(...)` over all
platform syncs before returning (`gamelib_mcp/tools/admin.py:71`). Platform syncs are
network-bound on external APIs and can take minutes. The MCP client sits blocked on the
call until the tool times out, even though the work itself succeeds in the background.

The original framing assumed there was no async job infrastructure. That framing is
wrong: the codebase already runs the *startup* refresh as a background `asyncio.Task`
(`lifecycle._run_startup_refresh`), already records status in the `meta` table
(`library_sync_status`, `library_sync_started_at/finished_at/error`, and per-platform
`integration_sync_<platform>_last_*` keys), already dedupes in-flight refreshes
(`_ensure_startup_refresh`), and already exposes status via `get_integration_status`.

The actual defect is narrow: the **`refresh_library` tool blocks** when it should
fire-and-return, and there is no focused tool for the AI to poll the sync it just
started. We are *not* building a generic job system.

## Goals

- `refresh_library` returns immediately after scheduling the sync.
- The AI can poll a focused tool to learn whether the sync is running, and the
  per-platform state (pending / running / done / error).
- A sync interrupted by a server crash does not leave status stuck at `in_progress`.

## Non-goals (explicitly cut — YAGNI)

- **No `jobs` table.** There is at most one library sync at a time; the `meta` table
  already holds the state.
- **No job IDs / "list all running jobs" endpoint.** There is only ever one sync.
- **No estimated-time-to-finish.** The dominant cost is a single opaque network fetch
  per platform; any ETA would be fabricated.
- **No sub-platform progress percentages.** Each sync is `fetch_*` (one slow, opaque
  network call) → loop-and-upsert (fast, local). ~98% of wall-clock time is in the
  un-subdividable fetch, so "47% through Steam" is not obtainable and would not help.

## Design

### 1. `refresh_library` becomes fire-and-return

The tool kicks off the sync as a background `asyncio.Task` (reusing the task/lock
machinery in `lifecycle.py`) and returns immediately with a small ack:

```json
{ "status": "started", "platforms": ["steam","epic","gog","switch2","ps5"], "already_running": false }
```

If a sync is already in flight, it returns without starting a second one:

```json
{ "status": "already_running", "platforms": [...], "already_running": true }
```

This extends the existing `_ensure_startup_refresh` dedup to the tool path. Because there
is at most one library sync at a time, a single global status record is the whole story —
no job ID is returned or needed.

The signature/docstring of the `@mcp.tool()` passthrough in `main.py` is the wire schema
and must be updated to describe the new non-blocking contract and point callers at
`get_sync_status`.

### 2. Per-platform progress written to `meta`

The background sync writes status as it progresses, reusing the existing outcome loop in
`refresh_library` (`admin.py:77-85`):

- **On start:** set `library_sync_status = in_progress`, `library_sync_started_at`, and
  mark each *selected* platform `running` via a per-run key
  `sync_platform_state_<platform>`. Platforms not selected for this run are left at /
  reset to `pending` for the purpose of this run's view (see status shape below).
- **As each `gather` outcome settles:** write that platform `done` or `error`. Error
  summary + classification reuse the existing `build_platform_sync_metadata` /
  `classify_platform_sync_error` helpers and the `integration_sync_<platform>_last_*`
  keys.
- **On finish:** set `library_sync_status = idle`, `library_sync_finished_at`.

Note on concurrency: `asyncio.gather` starts all selected platforms at once, so they flip
to `running` together and complete in network-return order. The realistic per-platform
lifecycle is therefore `pending → running → done|error`, not an ordered pipeline. That is
intended.

Meta keys involved:

- Existing: `library_sync_status`, `library_sync_started_at`, `library_sync_finished_at`,
  `library_sync_error`, `integration_sync_<platform>_last_attempt_at`,
  `_last_finished_at`, `_last_success_at`, `_last_error_summary`,
  `_last_error_classification`.
- New: `sync_platform_state_<platform>` ∈ {`pending`,`running`,`done`,`error`} — the
  per-run live state for this sync.

### 3. New tool `get_sync_status`

A thin read over the meta keys above. No DB writes. Shape:

```json
{
  "status": "in_progress",
  "started_at": "2026-06-14T12:00:00+00:00",
  "finished_at": null,
  "platforms": {
    "steam":   {"state": "done",    "last_success_at": "2026-06-14T12:00:30+00:00", "error": null},
    "epic":    {"state": "done",    "last_success_at": "2026-06-14T12:00:31+00:00", "error": null},
    "gog":     {"state": "running", "last_success_at": "2026-06-13T...",            "error": null},
    "switch2": {"state": "pending", "last_success_at": null,                        "error": null},
    "ps5":     {"state": "error",   "last_success_at": null, "error": "refresh token rejected"}
  }
}
```

`status` is `in_progress` or `idle` (mirrors `library_sync_status`). `platforms` covers
the syncable platform set. A new `get_sync_status` `@mcp.tool()` passthrough is added in
`main.py`.

### 4. Restart reconciliation

State lives in `meta` but the task is in-process, so a crash mid-sync would otherwise
leave `library_sync_status = in_progress` forever. On startup, `lifespan` reconciles:
if `library_sync_status == in_progress` and there is no live refresh task, reset it to
`idle`, set `library_sync_error` to a note indicating the previous sync was interrupted,
and mark any `sync_platform_state_<platform>` still at `running` as `error` (interrupted)
or back to `pending`. This runs before a fresh startup refresh is scheduled so the new run
overwrites cleanly. Stuck `in_progress` is not acceptable.

## Affected files

- `gamelib_mcp/tools/admin.py` — `refresh_library` fire-and-return + per-platform meta
  writes in the outcome loop.
- `gamelib_mcp/lifecycle.py` — background-task scheduling reused/extended for the tool
  path; startup reconciliation of stuck `in_progress`.
- `gamelib_mcp/tools/` — new `get_sync_status` handler (module placement decided in the
  plan; likely `admin.py` or a small dedicated module).
- `gamelib_mcp/main.py` — updated `refresh_library` passthrough docstring/contract; new
  `get_sync_status` passthrough.
- Tests — non-blocking return, dedup/`already_running`, per-platform state transitions,
  `get_sync_status` shape, and crash-reconciliation on startup.

## Testing

- `refresh_library` returns promptly with `started` / `already_running` without awaiting
  the syncs.
- Concurrent calls do not start a second sync (dedup).
- Per-platform meta transitions `pending → running → done`, and `→ error` on failure with
  the right classification.
- `get_sync_status` reflects in-progress and idle states and the per-platform map.
- Startup with a stale `in_progress` and no live task resets to `idle` and clears stuck
  `running` platform states.
