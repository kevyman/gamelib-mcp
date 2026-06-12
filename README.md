# gamelib-mcp

A [Model Context Protocol](https://modelcontextprotocol.io/) server that gives AI assistants tools to manage a cross-platform game library. It pulls your owned games and playtimes from Steam, Epic, GOG, Nintendo, and PSN, enriches them with external data sources (HowLongToBeat, ProtonDB, IGDB, OpenCritic, Metacritic, Backloggd, Steam reviews), and provides personalized game discovery through tag-based affinity scoring.

Ask your assistant things like *"what should I play next?"*, *"find me a cozy game under 10 hours"*, or *"how big is my backlog, honestly?"* — and it can answer from your actual library.

## Features

- **Cross-platform library** — Steam, Epic (via [Legendary](https://github.com/derrod/legendary)), GOG (via [lgogdownloader](https://github.com/Sude-/lgogdownloader)), Nintendo (via [nxapi](https://github.com/samuelthomas2774/nxapi)), and PSN, unified into one canonical game list with per-platform ownership and playtime.
- **Rich enrichment** — completion times (HowLongToBeat), Linux/Steam Deck compatibility (ProtonDB), critic scores (OpenCritic, Metacritic), metadata and identity resolution (IGDB), community sentiment (Steam reviews, SteamSpy).
- **Personalized discovery** — syncs your ratings from Backloggd and Steam (or rate games directly in chat with `rate_game`), computes weighted per-tag affinity scores, and ranks unplayed games by predicted fit. One `discover_games` tool covers vibe-based moods ("cozy", "souls"), taste-profile matches (with "why this rec" tag explanations), critic-score ranking, and backlog value picks.
- **Backlog intelligence** — backlog stats, platform breakdowns, hardware-preference-aware recommendations (e.g. prefer Switch 2 over Steam Deck over PS5), and farmed-achievement detection.
- **Operator control plane** — `get_integration_status` plus `/admin/integrations` (JSON) and `/admin/integrations/ui` (HTML) show per-platform readiness, missing credentials/mounts, and remediation steps.

## MCP Tools

| Tool | Description |
|---|---|
| `search_games` / `search_games_batch` | Punctuation-insensitive, relevance-ranked title search with fuzzy fallback |
| `get_game_detail` | Full detail for one game; triggers lazy enrichment on demand |
| `get_library_stats` | Library-wide aggregates with tag/genre/score/playtime filters |
| `discover_games` | Unified discovery: vibe filters, taste-match ranking (with matched-tag explanations), critic-score ranking, and backlog value picks |
| `get_taste_profile` | Your computed tag preferences |
| `sync_ratings` / `get_ratings` | Pull ratings from Backloggd and Steam reviews |
| `rate_game` | Rate a game 0–10 directly; feeds the taste profile immediately |
| `get_backlog_stats` | Backlog size, completion estimates |
| `get_platform_breakdown` | Ownership counts per platform |
| `refresh_library` | Re-sync all platforms, or a subset (e.g. just `["gog"]`) |
| `set_hardware_preference` | Priority order for suggested platforms |
| `add_game_to_platform` | Manually record ownership |
| `detect_farmed_games` | Flag games with suspicious achievement patterns |
| `set_nintendo_session` | Provide Nintendo session cookies at runtime |
| `get_integration_status` | Per-platform integration health |

## Quick Start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kevyman/gamelib-mcp.git
cd gamelib-mcp
uv sync

cp .env.example .env   # then fill in at least STEAM_API_KEY and STEAM_ID

# Run the server (Streamable HTTP on port 8000)
uv run python -m gamelib_mcp.main
```

Verify it's up:

```bash
curl http://localhost:8000/health
```

On startup the server initializes the SQLite database (default: `./data/gamelib.db`), refreshes the library if stale, and kicks off background enrichment.

### Connecting an MCP client

Point any Streamable-HTTP-capable MCP client at `http://localhost:8000/mcp`. If `MCP_AUTH_TOKEN` is set, clients must send it as a bearer token. Browser-based clients (claude.ai, chatgpt.com) must be allowlisted via `MCP_ALLOWED_ORIGINS`; native/CLI clients that send no `Origin` header are unaffected.

## Configuration

All configuration is via environment variables (see [.env.example](.env.example) for the full annotated list).

| Variable | Required | Purpose |
|---|---|---|
| `STEAM_API_KEY` | yes | From [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) |
| `STEAM_ID` | yes | Your 64-bit Steam ID |
| `MCP_AUTH_TOKEN` | recommended | Bearer token for the MCP endpoint (empty = open) |
| `MCP_ALLOWED_ORIGINS` | recommended | Comma-separated browser origins allowed to call MCP |
| `DATABASE_URL` | no | SQLite path; defaults to `data/gamelib.db` |
| `PORT` | no | Server port (default `8000`) |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | optional | IGDB enrichment ([dev.twitch.tv/console](https://dev.twitch.tv/console)) |
| `BACKLOGGD_USER` | optional | Backloggd username for rating sync |
| `PSN_NPSSO` | optional | PSN NPSSO cookie for PlayStation sync |
| `NINTENDO_SESSION_TOKEN` | optional | From `nxapi nso auth`, for Nintendo sync |
| `EPIC_LEGENDARY_HOST_PATH` | optional | Legendary config dir for Epic sync |
| `LGOGDOWNLOADER_HOST_PATH` | optional | lgogdownloader config dir for GOG sync |
| `HARDWARE_PREFERENCE` | optional | Platform priority for recommendations, e.g. `switch2,steam_deck,ps5` |

## Docker

Local testing (publishes the app port on localhost, production-only services disabled):

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build app
```

See [LOCAL_DOCKER.md](LOCAL_DOCKER.md) for the full local walkthrough.

Production (with Caddy reverse proxy):

```bash
docker compose --profile prod build
docker compose --profile prod up -d
docker compose --profile prod logs -f app
```

See [deploy.md](deploy.md) for the deployment runbook, including the integration control plane you should check first after any deploy or auth change.

## Architecture

```
gamelib_mcp/
├── main.py            # FastMCP app + tool registration (Streamable HTTP entry point)
├── lifecycle.py       # Lifespan, startup refresh, background enrichment orchestration
├── http_admin.py      # Bearer-auth middleware, /health, /admin/integrations*
├── tools/             # MCP tool handlers (business logic, AI-shaped responses)
├── data/              # Async data layer: platform syncs, enrichment providers
│   └── db/            # SQLite (aiosqlite): schema, migrations, queries, upserts,
│                      #   enrichment claims, tag-affinity recompute, fuzzy matching
└── integrations/      # Read-only per-platform status inspectors
```

Key design points:

- **Layer separation** — `tools/` handles MCP-facing logic and formatting; `data/` handles fetching and caching; `data/db/` owns all SQLite access.
- **Lazy enrichment** — bulk library calls stay fast by skipping unenriched fields; `get_game_detail` fetches and caches provider data on demand, while background workers enrich the rest over time.
- **Tag affinity** — after `sync_ratings` or `rate_game`, weighted tag scores are recomputed across all rated games (Backloggd/manual weight 1.0, Steam 0.5) and drive `discover_games`; Steam storefront feature flags are quarantined into a separate `features` column so they never skew taste.
- **Caching & rate limiting** — provider responses are cached (Steam Store 7 days, HLTB/ProtonDB 30 days) and HLTB pre-warming is throttled with an asyncio semaphore.
- **Fuzzy matching** — rapidfuzz-based title matching where providers lack stable identifiers.

The database is SQLite (WAL mode, foreign keys on), auto-migrated on startup. Core tables: `games`, `game_platforms`, `game_platform_identifiers`, `steam_platform_data`, `game_platform_enrichment`, `ratings`, `tag_affinity`, and a `meta` key-value store.

## Development

```bash
# Run the test suite
.venv/bin/python -m pytest

# Run a focused test file
.venv/bin/python -m pytest tests/test_igdb.py -q
```

DB-backed tests use temporary SQLite files and do not require a checked-in database. See [CLAUDE.md](CLAUDE.md) for additional contributor notes, including sandbox caveats for `aiosqlite` tests.

## License

No license file is currently included; all rights reserved by default.
