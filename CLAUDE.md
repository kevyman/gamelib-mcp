# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Steam MCP is a [Model Context Protocol](https://modelcontextprotocol.io/) server that gives AI assistants tools to manage a Steam game library. It enriches Steam data with external sources (HowLongToBeat, ProtonDB, Backloggd, Steam reviews) and provides personalized game discovery via tag-based affinity scoring.

## Commands

```bash
# Install dependencies (uses uv package manager)
uv sync

# Run locally (SSE transport on port 8000)
python -m gamelib_mcp.main

# Docker (production setup with Caddy reverse proxy)
docker compose --profile prod build
docker compose --profile prod up -d
docker compose --profile prod logs -f app
```

There is no test or lint framework configured.

## Required Environment Variables

Copy `.env.example` to `.env`:

- `STEAM_API_KEY` — from steamcommunity.com/dev/apikey
- `STEAM_ID` — 64-bit Steam ID
- `DATABASE_URL` — SQLite path (default: `file:steam.db`)
- `MCP_AUTH_TOKEN` — bearer token for MCP auth (empty = open)
- `PORT` — server port (default: 8000)

## Architecture

### Entry Point & Transport

`gamelib_mcp/main.py` creates the FastMCP app, registers all 10 tools, and starts an SSE server. On startup: DB is initialized, library is refreshed if >6h stale, and a background task pre-warms HLTB data for top unplayed games. A custom `/health` Starlette route returns sync status.

### Layer Separation

**`gamelib_mcp/tools/`** — MCP tool handlers (business logic, formatting responses for AI consumption):
- `library.py`: `search_games`, `get_library_stats`
- `detail.py`: `get_game_detail` (triggers lazy enrichment)
- `discover.py`: `find_games_by_vibe`, `get_recommendations`
- `ratings.py`: `sync_ratings`, `get_ratings`, `get_taste_profile`
- `stats.py`: `get_backlog_stats`
- `admin.py`: `refresh_library`, `detect_farmed_games`

**`gamelib_mcp/data/`** — Data fetching and caching layer (all async):
- `db.py`: SQLite schema, connection pool, tag affinity computation
- `steam_xml.py`: Steam Web API (owned games, playtimes)
- `steam_store.py`: Steam Store API (genres, tags, Metacritic) — 7-day cache
- `steam_reviews.py`: Scrapes Steam Community review pages
- `hltb.py`: HowLongToBeat async fetching — 30-day cache
- `protondb.py`: ProtonDB Linux compatibility tiers — 30-day cache
- `backloggd.py`: Scrapes Backloggd user reviews (fuzzy name matching via rapidfuzz)

### Database (SQLite via aiosqlite)

Four tables, auto-migrated on startup in `db.init_db()`:
- `games`: appid, name, playtimes, JSON genres/tags, cached enrichment fields
- `ratings`: normalized 1–10 scores from Backloggd (weight 1.0) and Steam (weight 0.5)
- `tag_affinity`: precomputed per-tag preference scores (drives recommendations)
- `meta`: key-value store (last sync timestamp, etc.)

WAL mode enabled, foreign keys on.

### Key Design Patterns

- **Lazy enrichment**: `get_game_detail` fetches from Steam Store, HLTB, ProtonDB on demand and caches results. Bulk library calls skip unenriched fields.
- **Tag affinity**: After `sync_ratings`, weighted tag scores are recomputed across all rated games. `get_recommendations` ranks unplayed games by these scores.
- **Rate limiting**: HLTB pre-warm uses an asyncio semaphore to avoid hammering the API.
- **Fuzzy matching**: Backloggd game titles are reconciled to Steam appids via rapidfuzz (handles naming mismatches between sources).
