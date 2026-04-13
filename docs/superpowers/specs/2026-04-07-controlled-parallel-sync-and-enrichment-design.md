# Controlled Parallel Sync And Enrichment Design

## Goal

Increase overlap between platform sync and metadata enrichment without turning SQLite or external APIs into an uncontrolled bottleneck. Steam sync, non-Steam sync, Steam-derived enrichment, and IGDB enrichment should all be able to make progress at the same time under explicit per-source limits.

## Verified Current Behavior

- `refresh_library()` already runs store syncs concurrently with `asyncio.gather(...)`.
- `background_enrich()` is still phase-serialized, so only one enrichment family runs at a time.
- Startup currently delays background enrichment until Steam sync signals readiness.
- IGDB work is not an independent background pipeline. It only happens when Epic, GOG, PSN, or Nintendo sync loops call `resolve_and_link_game(...)`.

This means Steam and non-Steam sync can overlap today, but enrichment parallelism is limited and IGDB cannot independently start while Steam enrichment is already running unless a non-Steam sync loop is actively invoking it.

## Product Decision

- Prefer controlled throughput over maximal burst throughput.
- Separate stores may sync concurrently.
- Separate enrichment families may run concurrently with each other and with store sync.
- IGDB must have its own background worker path and must not be gated on Steam completion.
- SQLite safety and duplicate-work avoidance are requirements, not optimizations.

## Recommended Approach

Use independent bounded worker families with DB-backed row claiming.

Worker families:

- Steam Store
- HLTB
- ProtonDB
- SteamSpy
- OpenCritic
- Metacritic
- IGDB

Each worker family repeatedly:

1. selects a small batch of eligible rows,
2. atomically claims those rows in SQLite,
3. performs network work outside the transaction,
4. writes final results,
5. clears the claim or leaves a normal cached/failure marker.

This prevents duplicate processing when multiple workers or startup triggers overlap, while still allowing different enrichment families to proceed in parallel.

## Claiming Model

Use explicit claim columns instead of overloading `*_cached_at` with sentinel strings.

Reasoning:

- `*_cached_at` is already used for freshness checks.
- sentinel values like `"IN_PROGRESS"` make freshness parsing and debugging worse.
- separate claim timestamps are easy to expire and reason about.

Suggested claim fields:

- `games.igdb_claimed_at`
- `games.hltb_claimed_at`
- `steam_platform_data.store_claimed_at`
- `steam_platform_data.protondb_claimed_at`
- `steam_platform_data.steamspy_claimed_at`
- `game_platform_enrichment.opencritic_claimed_at`
- `game_platform_enrichment.metacritic_claimed_at`

Claims should be considered stale after a short timeout, for example 10-15 minutes, so crashed workers do not permanently strand rows.

## Startup And Scheduling

Startup should no longer wait for Steam before starting enrichment.

New behavior:

- if the library is stale, schedule `refresh_library()` in the background as today
- independently schedule the background enrichment orchestrator immediately
- background workers poll for work until the system is quiescent instead of exiting after the first empty scan

This is the key change that allows IGDB to start while Steam sync or Steam enrichment is already in progress.

## IGDB Design

IGDB becomes a first-class background enrichment family.

Eligibility:

- canonical `games` rows with `igdb_cached_at IS NULL`
- optionally rows with stale `igdb_cached_at` in a later follow-up, but the first pass only needs missing metadata

Resolution strategy:

- resolve against the canonical game name
- if a Steam platform row exists, prefer Steam/PC as the platform hint
- otherwise choose the first known owned platform with an IGDB mapping
- reuse the existing request gate and per-IGDB-ID link locks

Writes:

- `igdb_id`
- `igdb_cached_at`
- missing `release_date`
- missing `genres`
- missing `tags`
- platform-specific release dates when a corresponding platform row exists

The existing sync-time calls to `resolve_and_link_game(...)` remain valid. They become one producer of IGDB completion, not the only producer.

## Background Enrichment Orchestrator

Replace the serialized phase pipeline with a supervisor that runs worker families concurrently, each with its own limit and pacing.

Principles:

- each family has a small, explicit concurrency limit
- each family claims rows before network work
- the supervisor tolerates one family failing without cancelling the others
- the orchestrator exits only after repeated idle polls across all families

Example initial limits:

- Steam Store: 4
- HLTB: 3
- ProtonDB: 1
- SteamSpy: 1
- OpenCritic: 1
- Metacritic: 1
- IGDB: rely on the existing shared IGDB request gate, with worker concurrency 2-4

## Safety Constraints

- SQLite writes remain short and transactional.
- Network work happens outside DB transactions.
- Claim acquisition uses optimistic single-writer SQL updates and verifies the claimed row count.
- Failed rows clear claims and record normal cached/failure markers so workers do not hot-loop.
- Existing upsert functions remain idempotent.

## Testing Requirements

Add coverage for:

- startup schedules refresh and enrichment independently
- IGDB worker starts even when Steam sync is still running
- different enrichment families can overlap
- a claimed row is not processed twice by concurrent workers
- stale claims are reclaimable
- worker-family failure does not cancel the supervisor
- Steam rows inserted during an active refresh are eventually picked up by the store worker

## Non-Goals

- generic job-queue infrastructure
- multi-process distributed locking
- stale IGDB refresh for already-cached rows
- changing platform sync semantics beyond concurrency and background scheduling
