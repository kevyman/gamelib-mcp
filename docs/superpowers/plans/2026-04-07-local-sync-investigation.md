# Local Sync Investigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Isolate why local startup only partially populates the database, with Steam never syncing, Nintendo behaving inconsistently, IGDB lookups not persisting metadata, and OpenCritic returning `400` for every search.

**Architecture:** Treat each subsystem as an independent failure domain and instrument boundaries before fixing anything. Verify launch context, database path consistency, API request/response behavior, and persistence logic with focused reproductions instead of relying on startup logs alone.

**Tech Stack:** Python 3.12, SQLite, `uv`, `pytest`, FastMCP, httpx, IGDB, OpenCritic

---

### Task 1: Capture Startup Context Precisely

**Files:**
- Modify: `gamelib_mcp/main.py`
- Test: manual startup run

- [ ] **Step 1: Add temporary startup diagnostics**

Add temporary logging near startup to record:
- current working directory
- resolved database path
- whether `STEAM_API_KEY` is present
- whether `STEAM_ID` is present
- resolved Nintendo cookie path
- whether Nintendo cookies were found

- [ ] **Step 2: Run local startup once**

Run:

```bash
uv run python -m gamelib_mcp.main
```

Expected:
- logs include the new startup diagnostics
- values clearly show whether the process sees Steam env vars and Nintendo cookies

- [ ] **Step 3: Record the observed launch context**

Write down:
- actual `cwd`
- actual DB path
- Steam env presence
- Nintendo cookie path and presence

- [ ] **Step 4: Remove or demote temporary diagnostics once evidence is captured**

Keep only any diagnostic logging that remains useful long-term.


### Task 2: Prove the Steam Failure Path

**Files:**
- Modify: `gamelib_mcp/data/steam_xml.py`
- Test: focused runtime invocation

- [ ] **Step 1: Verify how Steam credentials are read**

Inspect whether Steam credentials are read:
- at import time
- at call time

- [ ] **Step 2: Reproduce the failure in the exact local launch context**

Run:

```bash
uv run python -c "import asyncio; from gamelib_mcp.data.steam_xml import fetch_library; print(asyncio.run(fetch_library()))"
```

Expected:
- either a direct credential error or a successful sync

- [ ] **Step 3: If needed, instrument the Steam fetch path minimally**

Log whether the function sees credentials at runtime without exposing secret values.

- [ ] **Step 4: Document whether the issue is launch-wrapper/env propagation, import-time capture, or both**

This task is complete only when one concrete root cause is identified.


### Task 3: Prove the Nintendo Startup Path

**Files:**
- Modify: `gamelib_mcp/data/nintendo.py`
- Test: focused runtime invocation

- [ ] **Step 1: Add a minimal credential-source diagnostic**

Log:
- resolved cookie path
- whether `NINTENDO_SESSION_TOKEN` is present
- whether cookies are present

- [ ] **Step 2: Reproduce Nintendo sync directly**

Run:

```bash
uv run python -c "import asyncio; from gamelib_mcp.data.nintendo import sync_nintendo; print(asyncio.run(sync_nintendo()))"
```

Expected:
- either a real sync result or a precise failure point

- [ ] **Step 3: Compare direct invocation with startup invocation**

Determine whether Nintendo differs between:
- startup refresh
- direct function call

- [ ] **Step 4: Remove or keep diagnostics based on value**

Retain only logs that help future debugging without adding noise.


### Task 4: Trace IGDB Lookup to Persistence

**Files:**
- Modify: `gamelib_mcp/data/igdb.py`
- Test: focused direct reproduction and SQLite inspection

- [ ] **Step 1: Choose one known Epic title already inserted into `games`**

Use a title present in the local DB, for example one visible in current samples.

- [ ] **Step 2: Instrument the IGDB path at component boundaries**

Capture:
- input title
- IGDB result count
- chosen match
- selected or inserted `game_id`
- `updates` dict passed into the `UPDATE games`
- post-update row contents for the target game

- [ ] **Step 3: Reproduce with a single direct resolve**

Run a one-off call to `resolve_and_link_game(...)` in the same local environment.

- [ ] **Step 4: Query SQLite immediately after the reproduction**

Verify whether `igdb_id`, `release_date`, `genres`, `tags`, and `igdb_cached_at` were written for that row.

- [ ] **Step 5: Identify the exact break point**

This task is complete only when one of these is proven:
- IGDB returns no results
- lookup succeeds but `resolve_game()` still returns `None`
- `_apply_igdb_metadata()` builds no updates
- the update commits to a different DB path
- rows are overwritten later


### Task 5: Verify DB Path Consistency Across Writers

**Files:**
- Modify: `gamelib_mcp/data/db.py`, `gamelib_mcp/data/igdb.py`, `gamelib_mcp/data/enrich_bg.py` if needed for diagnostics
- Test: direct SQL inspection plus one startup run

- [ ] **Step 1: Confirm the resolved DB path in all relevant code paths**

Check:
- startup migration
- Epic sync
- Nintendo sync
- IGDB metadata writes
- background enrichment

- [ ] **Step 2: Compare file timestamps and row changes after isolated operations**

Use SQLite queries to verify the same file is being modified.

- [ ] **Step 3: Rule out split-brain database behavior**

This task is complete only when all writers are proven to target the same SQLite file.


### Task 6: Inspect Background Enrichment Selection Logic

**Files:**
- Modify: `gamelib_mcp/data/enrich_bg.py`
- Test: direct SQL queries and one background-enrichment run

- [ ] **Step 1: Run the phase-selection queries directly against SQLite**

Verify candidate counts for:
- store phase
- HLTB phase
- ProtonDB phase
- SteamSpy phase
- OpenCritic phase
- Metacritic phase

- [ ] **Step 2: Compare candidate counts with logged `0 games enriched` outputs**

Identify whether each phase is empty because:
- the query truly returns no rows
- upstream data is missing
- rows are marked `FAILED`
- the phase is Steam-only and Steam never synced

- [ ] **Step 3: Record which zero-count phases are expected and which indicate a bug**

Do not fix yet; first separate expected skips from broken selection logic.


### Task 7: Reproduce OpenCritic Search Failure in Isolation

**Files:**
- Modify: `gamelib_mcp/data/opencritic.py`
- Test: new focused tests in `tests/`

- [ ] **Step 1: Add a focused failing test for the current OpenCritic client**

Cover one search title that currently returns `400`, such as `Loop Hero` or `Remnant: From the Ashes`.

- [ ] **Step 2: Capture the full request and response body for one failing search**

Do not rely on status code alone. Record the actual error payload if available.

- [ ] **Step 3: Verify the current API contract**

Confirm whether:
- endpoint path changed
- parameter name changed
- method changed
- request headers are required

- [ ] **Step 4: Identify whether `400` responses are permanent API drift or malformed local requests**

This task is complete only when the failure mechanism is proven.


### Task 8: Turn Confirmed Root Causes Into Tests

**Files:**
- Modify: `tests/test_steam_xml.py`, `tests/test_nintendo.py`, `tests/test_igdb.py`, add OpenCritic tests as needed

- [ ] **Step 1: Add a focused test for the Steam root cause**

Target the exact credential-loading behavior that fails locally.

- [ ] **Step 2: Add a focused test for the Nintendo root cause**

Target startup credential detection and cookie fallback behavior.

- [ ] **Step 3: Add a focused test for the IGDB persistence root cause**

Target the exact failure boundary found in Task 4.

- [ ] **Step 4: Add a focused test for the OpenCritic root cause**

Target the broken request construction or outdated endpoint behavior.


### Task 9: Fix One Root Cause at a Time

**Files:**
- Modify only the files required by the specific confirmed bug under repair

- [ ] **Step 1: Fix the highest-priority confirmed root cause**

Start with the issue blocking the most downstream behavior.

- [ ] **Step 2: Run only the focused tests for that root cause**

Expected:
- the new regression test passes
- nearby tests stay green

- [ ] **Step 3: Re-run one small integration check**

Do not combine all fixes into one pass.

- [ ] **Step 4: Repeat for the next confirmed root cause**

Preserve isolation between fixes.


### Task 10: Verify on a Fresh Local Database

**Files:**
- Test only

- [ ] **Step 1: Start from a clean local DB**

Use a fresh SQLite file for the final validation run.

- [ ] **Step 2: Run startup once**

Run:

```bash
uv run python -m gamelib_mcp.main
```

- [ ] **Step 3: Verify expected post-run state in SQLite**

Check that:
- `games.igdb_id` is populated for a meaningful subset
- `games.igdb_cached_at` is populated
- OpenCritic rows are not all marked `FAILED`
- Steam either syncs or logs a precise env absence
- Nintendo either syncs or logs a precise credential/path reason

- [ ] **Step 4: Record final evidence**

Keep the verification commands and the resulting counts in the task notes.

