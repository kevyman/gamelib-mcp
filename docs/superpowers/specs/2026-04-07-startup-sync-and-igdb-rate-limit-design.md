# Startup Sync And IGDB Rate Limit Design

## Goal

Reduce perceived load time by making the server ready immediately with whatever library rows already exist, while a full refresh runs in the background. Speed up Steam ingest for large libraries by batching writes. Keep non-Steam syncs parallel without violating IGDB's published limits.

## Current Problems

- Startup blocks on Steam library sync in `gamelib_mcp/main.py`, so a large Steam account delays readiness even when existing rows are already useful.
- `gamelib_mcp/data/steam_xml.py` processes each Steam game through a long sequence of awaited DB writes, which scales poorly for libraries around 2,000 games.
- Non-Steam sync modules (`epic.py`, `gog.py`, `psn.py`, `nintendo.py`) now resolve titles through IGDB, but `gamelib_mcp/data/igdb.py` has no shared limiter, no `429` retry policy, and no jittered backoff.
- `refresh_library` runs platforms sequentially, so Steam delays the start of other platform fetches.

## Product Decision

- Server readiness is prioritized over completeness.
- Existing rows remain queryable during refresh, even if the library is temporarily incomplete.
- Refreshes are additive and update-in-place. There is no clear-and-rebuild phase.
- Data freshness is secondary to responsiveness. Temporary staleness is acceptable.

## External Constraint

IGDB's official documentation states:

- limit: 4 requests per second
- maximum open requests: 8
- over-limit responses: `429 Too Many Requests`
- `multiquery` is supported for batching up to 10 queries in one request

Sources:

- https://api-docs.igdb.com/
- https://api-docs.igdb.com/#multi-query

## Recommended Approach

Implement three coordinated changes:

1. Make startup non-blocking by moving stale library refresh into a singleton background task.
2. Batch Steam ingest writes into transactions so large libraries no longer pay one awaited DB path per game.
3. Add a shared IGDB limiter and retry layer, then allow Steam and non-Steam platform syncs to run in parallel behind that limiter.

This keeps the API responsive immediately, shortens full refresh time, and prevents parallel platform work from turning into IGDB burst traffic.

## Architecture

### Startup Behavior

`gamelib_mcp/main.py` will change from:

- initialize DB
- if library is stale, synchronously fetch Steam before serving
- start background enrichment

To:

- initialize DB
- seed hardware preference if needed
- check `library_synced_at`
- if stale, mark refresh state as in progress and spawn a background full-library refresh task
- start background enrichment independently
- begin serving requests immediately

The health endpoint and normal tools continue to operate using whatever rows already exist.

### Refresh Orchestration

`gamelib_mcp/tools/admin.py::refresh_library` becomes the canonical full refresh orchestration path for both manual refreshes and startup background sync.

Behavior:

- accept optional platform subset as it does today
- launch requested platform syncs concurrently with `asyncio.gather(..., return_exceptions=True)`
- isolate failures per platform so one failure does not cancel the others
- return a result map keyed by platform

Target concurrency:

- Steam, Epic, GOG, PSN, and Nintendo fetches can all start in parallel
- non-Steam IGDB resolution still flows through one shared limiter in `igdb.py`

### Sync State Metadata

Add lightweight metadata keys:

- `library_sync_status`: `idle` or `in_progress`
- `library_sync_started_at`: ISO timestamp for the current run
- `library_sync_finished_at`: ISO timestamp for the last completed run
- `library_sync_error`: optional summary string for the last failed run

Existing key:

- `library_synced_at` remains the last successful completed library refresh timestamp

These keys are for status visibility and duplicate-run prevention. They do not affect query semantics.

### Singleton Background Refresh

Only one full-library background refresh may run at a time.

Requirements:

- startup should not spawn a duplicate task if a refresh is already active
- manual `refresh_library` should either reuse the active refresh or return a clear `"already running"` response
- task cleanup must reset `library_sync_status` even when the background job raises

A process-local task guard is acceptable for now because the app currently runs as one server process. If multi-process deployment is introduced later, the lock will need to move into shared storage.

## Steam Ingest Design

### Current Bottleneck

`gamelib_mcp/data/steam_xml.py` currently:

- fetches Steam owned games once
- for each game, awaits `upsert_game`
- awaits `upsert_game_platform`
- awaits `upsert_game_platform_identifier`
- awaits `upsert_steam_platform_data`

This creates a high number of SQL round trips and commits for large libraries.

### New Steam Write Strategy

Refactor the Steam sync path to:

1. Fetch the full Steam library payload once.
2. Normalize game rows in memory.
3. Open a DB transaction.
4. Upsert games in batches.
5. Upsert `game_platforms` rows in batches.
6. Upsert `game_platform_identifiers` rows in batches.
7. Upsert `steam_platform_data` rows in batches.
8. Commit once per batch chunk or once per full sync if memory remains reasonable.

Implementation guidance:

- introduce DB helpers for bulk upsert operations rather than looping through the existing single-row helpers
- use chunking if needed, for example 250-500 rows per chunk
- keep idempotent semantics identical to the current single-row upserts
- avoid clearing any Steam-owned rows before the new data is written

### Steam Success Semantics

On successful Steam completion:

- update `library_synced_at`
- include `games_upserted` and `synced_at` in the result

On partial or failed Steam completion:

- leave prior rows intact
- do not regress `library_synced_at`
- record a refresh error summary in sync metadata

## Parallel Platform Refresh

### What Runs In Parallel

Parallelize at the platform sync level:

- `fetch_library()` for Steam
- `sync_epic()`
- `sync_gog()`
- `sync_psn()`
- `sync_nintendo()`

This is safe because each platform reads its own source and writes additive updates to the same DB.

### What Must Not Burst

Do not let each platform issue unconstrained IGDB requests independently.

All calls from:

- `sync_epic`
- `sync_gog`
- `sync_psn`
- `sync_nintendo`

must use the same shared IGDB limiter and retry path in `gamelib_mcp/data/igdb.py`.

## IGDB Rate Limit Design

### Shared Limiter

Implement a module-level limiter in `gamelib_mcp/data/igdb.py` with conservative settings below the published maximum:

- steady-state target: 3 requests per second
- hard ceiling: never exceed 4 requests per second
- max in-flight requests: 4

Rationale:

- stays below the official 4 req/s limit
- leaves headroom for timing drift and retries
- prevents platform fan-out from creating 8-open-request pressure immediately

### Retry Policy

Retry on:

- `429 Too Many Requests`
- transient `5xx`
- transport timeouts and short-lived network failures

Rules:

- respect `Retry-After` when present
- otherwise use exponential backoff
- add random jitter to every retry delay
- cap total retries to a small fixed count, for example 4

Suggested backoff sequence:

- base delay: 0.5 seconds
- retry delays approximately `0.5s`, `1s`, `2s`, `4s`
- apply jitter multiplier, for example between `0.8` and `1.3`

Jitter is required because parallel platform syncs can otherwise re-fire at the same time after a limit event.

### Multiquery

Add an IGDB multiquery path for batch title resolution.

Usage:

- gather unresolved titles into small batches
- send up to 10 search subqueries in one IGDB request
- map responses back to input titles and run the same fuzzy-best-match logic per title

Expected benefit:

- lowers request count substantially for non-Steam syncs
- preserves throughput while staying inside the official per-request rate limits

This should be treated as an optimization on top of the shared limiter, not a replacement for it.

### Failure Fallback

If IGDB ultimately fails after retries for a given title:

- log the failure
- fall back to existing fuzzy name matching behavior
- continue syncing the rest of the platform

The sync should degrade gracefully rather than aborting the whole platform.

## Background Enrichment Interaction

Background enrichment in `gamelib_mcp/data/enrich_bg.py` already runs after startup and is intentionally slower due to its own polite delays.

This design does not move enrichment into the critical path.

Expected behavior after the redesign:

- startup becomes fast because neither Steam sync nor enrichment blocks readiness
- refresh duration shifts toward background Steam ingest plus post-sync enrichment
- user-visible wait is reduced because data appears incrementally

## Query Semantics During Refresh

During any refresh:

- all existing rows remain queryable
- newly synced rows become visible as they are committed
- partial platform completion is acceptable
- tools do not need special casing for partial-library mode

Optional future enhancement:

- expose sync status in a tool or `/health` payload so clients can show that a refresh is still running

## Error Handling

- per-platform failures must be logged and reported in refresh results without cancelling sibling tasks
- the background refresh wrapper must always clear in-progress state in a `finally` block
- sync metadata should preserve the last error summary for inspection
- stale existing rows must remain available after any failure
- IGDB retry behavior must not retry permanent client errors other than `429`

## Testing

### Startup And Orchestration

- startup returns without waiting for stale Steam sync
- stale startup schedules one background refresh task
- repeated stale checks do not create duplicate refresh tasks
- `refresh_library` runs requested platforms concurrently
- one platform failure does not cancel the others

### Steam Batching

- batched Steam sync preserves current ownership, playtime, identifier, and `rtime_last_played` semantics
- large Steam payloads use bulk helpers rather than one full single-row write chain per game
- `library_synced_at` updates only on successful Steam completion

### IGDB Limiter

- concurrent callers share one limiter
- request rate stays at or below configured ceiling
- `429` triggers retry with jittered backoff
- `Retry-After` is honored
- ultimate IGDB failure falls back to fuzzy matching rather than aborting sync

### Query Behavior

- existing rows remain queryable while refresh is in progress
- partial results are visible during background sync
- no clear-and-rebuild behavior occurs

## Out Of Scope

- cross-process distributed locking
- changing background enrichment rate limits
- deleting platform rows that disappear from remote libraries
- schema redesign beyond lightweight sync-status metadata and any bulk-helper support needed for Steam batching

## Implementation Order

1. Add sync-status metadata and background task orchestration in `main.py` and `tools/admin.py`.
2. Refactor Steam sync to batch writes through new DB bulk helpers.
3. Add shared IGDB limiter and retry logic.
4. Parallelize full-library refresh across platforms.
5. Add IGDB multiquery batching as a throughput optimization.
6. Add tests for startup behavior, Steam batching, and IGDB retry/limit handling.

## Expected Outcome

- server becomes ready almost immediately when stale data already exists
- Steam no longer dominates perceived startup time for large libraries
- full refresh completes faster because Steam and non-Steam work run together
- IGDB usage remains within published limits even under parallel platform refresh
- enrichment remains the long-running background activity, but not a readiness blocker
