# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

gamelib-mcp is a [Model Context Protocol](https://modelcontextprotocol.io/) server that gives AI assistants tools to manage a cross-platform game library. It enriches platform data with external sources such as HowLongToBeat, ProtonDB, IGDB, Backloggd, and Steam reviews, and provides personalized game discovery via tag-based affinity scoring.

## Commands

```bash
# Install dependencies (uses uv package manager)
uv sync

# Run locally (Streamable HTTP on port 8000)
uv run python -m gamelib_mcp.main

# Run tests
.venv/bin/python -m pytest

# Run a focused test file
.venv/bin/python -m pytest tests/test_igdb.py -q

# Fallback if pytest/plugin discovery is awkward in the environment
.venv/bin/python -m unittest tests.test_igdb tests.test_enrich_bg

# Docker (production setup with Caddy reverse proxy)
docker compose --profile prod build
docker compose --profile prod up -d
docker compose --profile prod logs -f app
```

`pytest` is configured in the `dev` dependency group in `pyproject.toml`. In this workspace, the reliable test runner is the local virtualenv at `.venv/bin/python`. There is no lint framework configured.

### Test Environment Note

DB-backed tests use temporary SQLite files; they do not require a checked-in `data/gamelib.db`.
In Codex sandboxing, `aiosqlite` tests can hang because the worker thread completes the SQLite
operation but the thread-safe event-loop callback does not resume the awaiting coroutine. If a
test hangs at `aiosqlite.connect()` or early DB migration setup, run the same pytest command
outside the sandbox before changing test fixtures or database paths.

## Required Environment Variables

Copy `.env.example` to `.env`:

- `STEAM_API_KEY` — from steamcommunity.com/dev/apikey
- `STEAM_ID` — 64-bit Steam ID
- `DATABASE_URL` — SQLite path (optional). Defaults to `data/gamelib.db` when unset. Set explicitly (e.g. `file:./data/gamelib.db`) only when overriding the location.
- `MCP_AUTH_TOKEN` — bearer token for MCP auth (empty = open)
- `MCP_ALLOWED_ORIGINS` — comma-separated browser origins allowed to call the MCP endpoint, e.g. `https://claude.ai,https://chatgpt.com`. Requests without an `Origin` header are still allowed for native/CLI MCP clients.
- `PORT` — server port (default: 8000)

## Database Path

The project database lives at `./data/gamelib.db`. `_db_path()` defaults to `data/gamelib.db` when `DATABASE_URL` is unset — no legacy root-level fallback exists.

- Do not set `DATABASE_URL` for normal dev; the default is correct.
- If `./gamelib.db` exists in the repo root, it is stale/spurious — delete it.

## Architecture

### Entry Point & Transport

App composition is split across three thin top-level modules:
- `gamelib_mcp/main.py`: creates the FastMCP app, registers all MCP tools (declarative `@mcp.tool()` passthroughs whose signatures/docstrings are the wire schema), wires the lifespan + HTTP routes, and is the Streamable HTTP entry point (`python -m gamelib_mcp.main`).
- `gamelib_mcp/lifecycle.py`: the `lifespan` context manager and all background-task orchestration — startup library refresh, background enrichment scheduling, periodic refresh loop, per-event-loop locks, and the per-platform sync-metadata helpers. On startup: DB is initialized, library refresh is scheduled if stale, and background enrichment starts without waiting for a single provider to finish first.
- `gamelib_mcp/http_admin.py`: bearer-auth ASGI middleware plus the `/health` and `/admin/integrations*` routes, registered via `register_http_routes(mcp)`.

Dependency direction is a clean DAG: `main → lifecycle`, `main → http_admin`, `tools.admin → lifecycle`. `lifecycle` reaches `tools.admin.refresh_library` lazily (no top-level import) to avoid a cycle.

### Layer Separation

**`gamelib_mcp/tools/`** — MCP tool handlers (business logic, formatting responses for AI consumption):
- `library.py`: `search_games`, `search_games_batch`, `get_library_stats`
- `detail.py`: `get_game_detail` (triggers lazy enrichment)
- `discover.py`: `discover_games` (vibe filters + taste/critic/value ranking with matched-tag explanations)
- `ratings.py`: `sync_ratings`, `rate_game`, `get_ratings`, `get_taste_profile`
- `stats.py`: `get_backlog_stats`
- `admin.py`: `refresh_library` (full or per-platform sync), `detect_farmed_games`, `set_nintendo_session`
- `platforms.py`: `get_platform_breakdown`, `set_hardware_preference`, `add_game_to_platform`, `update_game` (manual per-game property edits incl. `is_farmed`; edited columns are recorded in `games.manual_overrides` so sync/enrichment won't clobber them)
- `integrations.py`: `get_integration_status` (read-only filter over the inspector payload)
- `common.py`: shared helpers — the steam-appid correlated subquery and the platform-alias resolver (imported by the modules above). The three `_GAME_ROLLUP_CTE` variants deliberately stay in their own modules; they differ.
- `search.py`: tiered name-match SQL builder (exact > prefix > substring > token-AND over `games.name_normalized`) plus the fuzzy fallback, used by search, detail, and rate_game name resolution.

**`gamelib_mcp/data/`** — Data fetching and caching layer (all async):
- `db/`: SQLite package. `__init__.py` holds the connection/migration/init bottom layer and re-exports everything (so `gamelib_mcp.data.db.<name>` is the stable public API). Submodules: `schema.py` (versioned DDL), `claims.py` (enrichment row-claiming + batch loaders), `queries.py` (meta KV, lookups, platform assembly), `upserts.py` (game/platform/enrichment upserts + bulk Steam sync), `affinity.py` (tag-affinity recompute), `fuzzy.py` (fuzzy name matching).
- `enrich_bg.py`: background enrichment orchestration (the worker families driven by the claim functions).
- `steam_xml.py`: Steam Web API (owned games, playtimes)
- `steam_store.py`: Steam Store API (genres, tags, Metacritic) — 7-day cache
- `steam_reviews.py`: Scrapes Steam Community review pages
- `hltb.py`: HowLongToBeat async fetching — 30-day cache
- `protondb.py`: ProtonDB Linux compatibility tiers — 30-day cache
- `backloggd.py`: Scrapes Backloggd user reviews (fuzzy name matching via rapidfuzz)
- Other providers/syncs: `igdb.py`, `opencritic.py`, `metacritic.py`, `steamspy.py`, and the platform syncs `epic.py`, `gog.py`, `nintendo.py`, `psn.py` (plus `title_normalization.py`).

**`gamelib_mcp/integrations/`** — read-only integration status inspectors (`status.py` dataclasses, `inspectors.py` per-platform probes) surfaced by the `get_integration_status` tool and the `/admin/integrations*` routes.

### Database (SQLite via aiosqlite)

Core tables, auto-migrated on startup in `db.init_db()`:
- `games`: canonical game rows and shared enrichment fields
- `game_platforms`: ownership/playtime per platform
- `game_platform_identifiers`: provider-specific IDs such as `steam_appid` and `gog_product_id`
- `steam_platform_data`: Steam-only provider metadata
- `game_platform_enrichment`: cross-platform review/release enrichment
- `ratings`: normalized 1–10 scores from Backloggd (weight 1.0), manual `rate_game` ratings (weight 1.0), and Steam (weight 0.5)
- `tag_affinity`: precomputed per-tag preference scores (drives recommendations)
- `meta`: key-value store (last sync timestamp, etc.)

WAL mode enabled, foreign keys on.

### Key Design Patterns

- **Lazy enrichment**: `get_game_detail` fetches available provider-specific enrichment on demand and caches results. Bulk library calls skip unenriched fields.
- **Tag affinity**: After `sync_ratings` or `rate_game`, weighted tag scores are recomputed across all rated games (Steam feature flags from `data/tags.py` are excluded — they live in `games.features`, not `games.tags`). `discover_games` ranks unplayed games by these scores and explains each result via `matched_tags`.
- **Rate limiting**: HLTB pre-warm uses an asyncio semaphore to avoid hammering the API.
- **Fuzzy matching**: Title matching uses rapidfuzz/stdlib helpers where provider identifiers are unavailable.
- **Manual overrides**: `update_game` writes user-edited `games` columns and records their names in `games.manual_overrides` (JSON array). Sync/enrichment writers (`steam_store`, `steamspy`, `hltb`, `igdb`, the bulk Steam name update, and `detect_farmed_games`) consult `get_manual_overrides` and skip those columns, so a hand edit survives later syncs.
