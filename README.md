# gamelib-mcp

A [Model Context Protocol](https://modelcontextprotocol.io/) server that gives AI assistants tools to manage a cross-platform game library. It pulls your owned games and playtimes from Steam, Epic, GOG, Nintendo, PSN, and Xbox, enriches them with external data sources (HowLongToBeat, ProtonDB, IGDB, OpenCritic, Metacritic, Backloggd, Steam reviews), and provides personalized game discovery through tag-based affinity scoring.

Ask your assistant things like *"what should I play next?"*, *"find me a cozy game under 10 hours"*, or *"how big is my backlog, honestly?"* — and it can answer from your actual library.

gamelib-mcp is single-user by design: one deployment serves one person's library (see [docs/adr/0001-single-user.md](docs/adr/0001-single-user.md)).

## Features

- **Cross-platform library** — Steam, Epic (via [Legendary](https://github.com/derrod/legendary)), GOG (via [lgogdownloader](https://github.com/Sude-/lgogdownloader)), Nintendo Switch (digital ownership via your Nintendo Account, playtime via the Switch Parental Controls API), PSN, and Xbox (via [OpenXBL](https://xbl.io)), unified into one canonical game list with per-platform ownership and playtime.
- **Rich enrichment** — completion times (HowLongToBeat), Linux/Steam Deck compatibility (ProtonDB), critic scores (OpenCritic, Metacritic), metadata and identity resolution (IGDB), community sentiment (Steam reviews, SteamSpy).
- **Personalized discovery** — syncs your ratings from Backloggd and Steam (or rate games directly in chat with `rate_game`), computes weighted per-tag affinity scores, and ranks unplayed games by predicted fit. One `discover_games` tool covers vibe-based moods ("cozy", "souls"), taste-profile matches (with "why this rec" tag explanations), critic-score ranking, and backlog value picks.
- **Backlog intelligence** — backlog stats, platform breakdowns, hardware-preference-aware recommendations (e.g. prefer Switch 2 over Steam Deck over PS5), and farmed-achievement detection.
- **Game-quality assessments** — the `game-quality`, `backlog-triage`, and `bundle-evaluation` skills are served from this repo (`get_skill`, `skill://` resources); verdicts are recorded with their components and rendered as an evaluation card, then compared against what was actually bought, played, and rated (`get_stats(report="calibration")`).
- **Operator control plane** — `get_integration_status` plus `/admin/integrations` (JSON) and `/admin/integrations/ui` (HTML) show per-platform readiness, missing credentials/mounts, and remediation steps.

## MCP Tools

33 tools. Any tool that acts on one game also takes `items=[...]` to do the same
thing in bulk in a single call — see [ADR 0004](docs/adr/0004-consolidated-tool-surface.md).

| Tool | Description |
|---|---|
| `search_games` | Punctuation-insensitive, relevance-ranked title search with fuzzy fallback; `queries=[...]` resolves several names at once |
| `get_game_detail` | Full detail for a game; triggers lazy enrichment on demand (`items` skips it) |
| `get_library_stats` | Library-wide aggregates with tag/genre/score/playtime filters |
| `discover_games` | Unified discovery: vibe filters, taste-match ranking (with matched-tag explanations), critic-score ranking, and backlog value picks |
| `get_stats` | One rollup per call: `backlog`, `platforms`, `taste`, `spending`, or `series` |
| `get_ratings` / `rate_game` | Read synced ratings; rate a game 0–10 directly, feeding the taste profile immediately |
| `get_wishlist` | Wishlist contents, or with `with_prices=True` current deals (Steam/GOG/Epic via IsThereAnyDeal, Switch via DekuDeals) |
| `get_play_history` | What you actually played in a time window, per game |
| `discover_series_gaps` | Unowned entries in series you own and rate highly (IGDB collections/franchises) |
| `sync` | Re-sync `library`, `wishlist`, and/or `ratings`; `platforms` scopes the first two |
| `get_sync_status` | Poll a running library sync |
| `check_library` | One registry of data-integrity checks (identity, nesting, ownership, spend, completion…) with machine-readable repair pointers |
| `query_library` | Read-only SQL escape hatch; call with no arguments for the live schema |
| `set_hardware_preference` | Priority order for suggested platforms |
| `add_game_to_platform` / `update_game` / `set_playtime` / `set_acquisition` | Manual ownership, metadata, playtime pins, and purchase records |
| `merge_games` / `split_game` / `delete_game` | Identity repair |
| `import_purchases` / `split_bundle_acquisition` | Storefront purchase history and bundle price splits |
| `create_session_ingest_link` | Mint a single-use browser link for connecting a store/account session outside the chat — cookie pastes (Nintendo/Epic/Humble/Steam) and the Nintendo Parental Controls sign-in that enables Switch playtime |
| `get_integration_status` | Per-platform integration health |
| `get_scrape_config` / `manage_scrape_config` | Inspect or heal the declarative scrape config |
| `get_assessment_context` / `record_assessment` / `void_assessment` | Game-quality evaluation: the library-grounded context for a verdict, recording the verdict's components (rendered as an evaluation card), and hard-deleting a misfiled one |
| `get_skill` | Load the gaming-skills methodology this server is the canonical home of (`game-quality`, `backlog-triage`, `bundle-evaluation`) |
| `set_switch2_playtime_baseline` | Pin the Switch playtime total the Parental Controls history starts counting from |

## Quick Start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/kevyman/gamelib-mcp.git
cd gamelib-mcp
uv sync

cp .env.local.example .env   # localhost-only auth mode; then fill in Steam values

# Run the server (Streamable HTTP on port 8000)
uv run python -m gamelib_mcp.main
```

Verify it's up:

```bash
curl http://localhost:8000/health
```

On startup the server initializes the SQLite database (default: `./data/gamelib.db`), refreshes the library if stale, and kicks off background enrichment.

### Connecting an MCP client

Point any Streamable-HTTP-capable MCP client at `http://localhost:8000/mcp`. The production configuration uses FastMCP's GitHub OAuth provider; local-only development can explicitly set `MCP_AUTH_MODE=disabled`. `/admin/*` always requires `Authorization: Bearer <MCP_ADMIN_AUTH_TOKEN>`. Browser-based clients must be allowlisted via `MCP_ALLOWED_ORIGINS`; native/CLI clients that send no `Origin` header are unaffected.

## Configuration

All configuration is via environment variables. Production starts from [.env.example](.env.example); localhost Docker development starts from [.env.local.example](.env.local.example).

| Variable | Required | Purpose |
|---|---|---|
| `STEAM_API_KEY` | yes | From [steamcommunity.com/dev/apikey](https://steamcommunity.com/dev/apikey) |
| `STEAM_ID` | yes | Your 64-bit Steam ID |
| `MCP_AUTH_MODE` | yes | `oauth` in production; `disabled` only for localhost development |
| `MCP_PUBLIC_BASE_URL` | OAuth | Public HTTPS origin used for OAuth discovery, callbacks, and token audience |
| `GITHUB_OAUTH_CLIENT_ID` / `GITHUB_OAUTH_CLIENT_SECRET` | OAuth | Credentials for the GitHub OAuth App |
| `MCP_OAUTH_JWT_SIGNING_KEY` | OAuth | Independent secret used to sign FastMCP access tokens |
| `MCP_OAUTH_GITHUB_USER_IDS` | OAuth | Comma-separated GitHub numeric user ID(s) allowed to use tools |
| `MCP_ADMIN_AUTH_TOKEN` | yes | Independent header-only token for `/admin/*` |
| `FASTMCP_HOME` | OAuth | Persistent encrypted OAuth state directory; `/data/fastmcp` in Docker |
| `MCP_ALLOWED_ORIGINS` | recommended | Comma-separated browser origins allowed to call MCP |
| `DATABASE_URL` | no | SQLite path; defaults to `data/gamelib.db` |
| `PORT` | no | Server port (default `8000`) |
| `LOG_LEVEL` | no | Root log level: `DEBUG`, `INFO` (default), `WARNING`, `ERROR`; `DEBUG` surfaces per-item enrichment failures |
| `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` | optional | IGDB enrichment ([dev.twitch.tv/console](https://dev.twitch.tv/console)) |
| `BACKLOGGD_USER` | optional | Backloggd username for rating sync |
| `PSN_NPSSO` | optional | PSN NPSSO cookie for PlayStation sync |
| `OPENXBL_API_KEY` | optional | Personal key from [xbl.io/console](https://xbl.io/console) for Xbox sync (ownership via title history, playtime best-effort) |
| `OPENXBL_XUID` | optional | Xbox account to inspect; defaults to the API key owner's own account |
| `NINTENDO_COOKIES_FILE` | optional | Switch digital ownership (populate via `create_session_ingest_link(provider="nintendo")`) |
| `NINTENDO_PCTL_SESSION_FILE` | optional | Switch playtime via Parental Controls (populate via `create_session_ingest_link(provider="nintendo_pctl")`) |
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
- **Tag affinity** — after `sync(targets=["ratings"])` or `rate_game`, weighted tag scores are recomputed across all rated games (Backloggd/manual weight 1.0, Steam 0.5) and drive `discover_games`; Steam storefront feature flags are quarantined into a separate `features` column so they never skew taste.
- **Caching & rate limiting** — provider responses are cached (Steam Store 7 days, HLTB/ProtonDB 30 days) and HLTB pre-warming is throttled with an asyncio semaphore.
- **Fuzzy matching** — rapidfuzz-based title matching where providers lack stable identifiers.

The database is SQLite (WAL mode, foreign keys on), auto-migrated on startup. Core tables: `games`, `game_platforms`, `game_platform_identifiers`, `steam_platform_data`, `game_platform_enrichment`, `nintendo_play_summary` (Parental Controls per-game playtime history), `ratings`, `tag_affinity`, and a `meta` key-value store.

## Development

```bash
# Run the test suite (parallel across all cores by default)
.venv/bin/python -m pytest

# Run a focused test file
.venv/bin/python -m pytest tests/test_igdb.py -q

# Run serially, for readable output while debugging
.venv/bin/python -m pytest -n0
```

DB-backed tests use temporary SQLite files and do not require a checked-in database. See [CLAUDE.md](CLAUDE.md) for additional contributor notes, including sandbox caveats for `aiosqlite` tests.

## License

MIT — see [LICENSE](LICENSE).
