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

# Lint and type check (both gate CI on pull requests)
.venv/bin/ruff check gamelib_mcp tests scripts
.venv/bin/mypy gamelib_mcp

# Docker (production setup with Caddy reverse proxy)
docker compose --profile prod build
docker compose --profile prod up -d
docker compose --profile prod logs -f app
```

`pytest`, `ruff`, and `mypy` are configured in the `dev` dependency group in `pyproject.toml` (ruff/mypy settings live under `[tool.ruff]`/`[tool.mypy]` there). In this workspace, the reliable test runner is the local virtualenv at `.venv/bin/python`. CI (`ci.yml`) runs ruff, mypy, and pytest on every pull request; mypy covers `gamelib_mcp` only, not `tests/`.

### Test Environment Note

DB-backed tests use temporary SQLite files; they do not require a checked-in `data/gamelib.db`.
In Codex sandboxing, `aiosqlite` tests can hang because the worker thread completes the SQLite
operation but the thread-safe event-loop callback does not resume the awaiting coroutine. If a
test hangs at `aiosqlite.connect()` or early DB migration setup, run the same pytest command
outside the sandbox before changing test fixtures or database paths.

## Required Environment Variables

Copy `.env.example` to `.env` for a production deploy (OAuth required), or `.env.local.example` to `.env` for localhost-only development (`MCP_AUTH_MODE=disabled`):

- `STEAM_API_KEY` — from steamcommunity.com/dev/apikey
- `STEAM_ID` — 64-bit Steam ID
- `DATABASE_URL` — SQLite path (optional). Defaults to `data/gamelib.db` when unset. Set explicitly (e.g. `file:./data/gamelib.db`) only when overriding the location.
- `MCP_AUTH_MODE` — must be explicit: `oauth` in production or `disabled` for localhost-only development.
- `MCP_PUBLIC_BASE_URL`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `MCP_OAUTH_JWT_SIGNING_KEY`, `MCP_OAUTH_GITHUB_USER_IDS` (comma-separated), and `FASTMCP_HOME` configure GitHub OAuth in production.
- `MCP_ADMIN_AUTH_TOKEN` — independent header-only bearer token for `/admin/*`.
- `MCP_ALLOWED_ORIGINS` — comma-separated browser origins allowed to call the HTTP surface, e.g. `https://chatgpt.com`. The OAuth server's own origin is automatically included; requests without an `Origin` header are still allowed for native/CLI MCP clients.
- `PORT` — server port (default: 8000)
- `DEKUDEALS_WISHLIST_URL` — optional. Your DekuDeals shared wishlist URL (e.g. `https://www.dekudeals.com/wishlist/<share-id>`), used by `sync_wishlist` to populate the switch2 wishlist since Nintendo has no wishlist API.
- `ITAD_API_KEY` — optional. Free key from [isthereanydeal.com/apps/my/](https://isthereanydeal.com/apps/my/), enables Steam/GOG/Epic price lookups for `get_wishlist_deals`; without it those items land in `unpriced`.
- `ITAD_COUNTRY` — optional. ISO country code for pricing (default `US`).
- `SCRAPE_HEAL_REQUIRE_APPROVAL` — optional. When set to `1`, a `propose_scrape_config` override that passes validation lands as `pending` (requiring `approve_scrape_config`) instead of activating immediately.

## Database Path

The project database lives at `./data/gamelib.db`. `_db_path()` defaults to `data/gamelib.db` when `DATABASE_URL` is unset — no legacy root-level fallback exists.

- Do not set `DATABASE_URL` for normal dev; the default is correct.
- If `./gamelib.db` exists in the repo root, it is stale/spurious — delete it.

## Architecture

### Entry Point & Transport

App composition is split across three thin top-level modules:
- `gamelib_mcp/main.py`: creates the FastMCP app, registers all MCP tools (declarative `@mcp.tool()` passthroughs whose signatures/docstrings are the wire schema), builds the security config and GitHub OAuth provider (`auth.py`), wires the lifespan + HTTP routes, and is the Streamable HTTP entry point (`python -m gamelib_mcp.main`).
- `gamelib_mcp/auth.py`: process-lifetime `SecurityConfig` (env validation, fail-closed unless `MCP_AUTH_MODE` is explicit), GitHub OAuth provider construction, and the single/multi-owner `AuthMiddleware` authorization check restricting MCP tool access to the configured GitHub user ID(s).
- `gamelib_mcp/lifecycle.py`: the `lifespan` context manager and all background-task orchestration — startup library refresh, background enrichment scheduling, periodic refresh loop, per-event-loop locks, and the per-platform sync-metadata helpers. On startup: DB is initialized, library refresh is scheduled if stale, and background enrichment starts without waiting for a single provider to finish first.
- `gamelib_mcp/http_admin.py`: `HttpSecurityMiddleware` (origin allowlisting for all routes, plus an independent header-only bearer token gating `/admin/*`) and the `/health` and `/admin/integrations*` routes, registered via `register_http_routes(mcp)`. `/mcp` itself is authenticated by FastMCP's OAuth provider, not this middleware.

Dependency direction is a clean DAG: `main → lifecycle`, `main → http_admin`, `tools.admin → lifecycle`. `lifecycle` reaches `tools.admin.refresh_library` lazily (no top-level import) to avoid a cycle.

### Layer Separation

**`gamelib_mcp/tools/`** — MCP tool handlers (business logic, formatting responses for AI consumption):
- `library.py`: `search_games`, `search_games_batch`, `get_library_stats`
- `detail.py`: `get_game_detail` (triggers lazy enrichment)
- `discover.py`: `discover_games` (vibe filters + taste/critic/value ranking with matched-tag explanations)
- `ratings.py`: `sync_ratings`, `rate_game`, `get_ratings`, `get_taste_profile`
- `stats.py`: `get_backlog_stats`
- `deals.py`: `get_wishlist_deals` (current prices/deals for wishlist games; cached via IsThereAnyDeal for Steam/GOG/Epic and DekuDeals for switch2; 12h TTL, `refresh=True` forces a live fetch)
- `admin.py`: `refresh_library` (full or per-platform sync), `sync_wishlist` (Steam + DekuDeals-backed switch2 wishlist sync; PSN has no wishlist API), `detect_farmed_games`, `detect_collapsed_games` (read-only; surfaces *within-platform* over-merges — one platform row holding multiple distinct same-type store IDs), `detect_cross_platform_collapses` (queries IGDB external_games to flag *cross-platform* over-merges — a row whose Steam appid resolves to a different IGDB game than the row's `igdb_id`, e.g. Steam 2008 + PS5 2023 Dead Space), `split_game` (inverse of `merge_games`: peels store identifier(s) off an over-merged row onto a new game — re-points the whole platform row when all its identifiers move, else creates a fresh platform row and moves the subset), `set_nintendo_session` (VGCS ownership cookies), `set_nintendo_pctl_session` (Parental Controls playtime token)
- `platforms.py`: `get_platform_breakdown`, `get_wishlist` (lists `game_wishlist` rows, optionally filtered by platform), `set_hardware_preference`, `add_game_to_platform` (`owned=False` records a manual wishlist entry instead of an owned copy — the only path for PSN), `update_game` (manual per-game property edits incl. `is_farmed`; edited columns are recorded in `games.manual_overrides` so sync/enrichment won't clobber them)
- `integrations.py`: `get_integration_status` (read-only filter over the inspector payload; the payload includes a `scrapers` entry reporting scrape-config drift)
- `scrape_admin.py`: the scrape-config heal tools — `get_scrape_config` (defaults + active override + version history), `diagnose_scrape` (live-fetches a sample page with the active config; returns per-selector match counts and a sanitized `untrusted_page_excerpt`), `propose_scrape_config` (validates a partial override via `data/scrape_validate.py` and persists it only on pass; auto-activates unless `SCRAPE_HEAL_REQUIRE_APPROVAL=1` lands it as pending), `approve_scrape_config`, `rollback_scrape_config` (walks back one version per call, ultimately to code defaults)
- `common.py`: shared helpers — the steam-appid correlated subquery, the series-names correlated subquery (`SERIES_NAMES_SQL`), and the platform-alias resolver (imported by the modules above). The three `_GAME_ROLLUP_CTE` variants deliberately stay in their own modules; they differ.
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
- `steam_wishlist.py`: Steam wishlist via the official `IWishlistService/GetWishlist` Web API (same `STEAM_API_KEY`/`STEAM_ID` as `steam_xml.py`); the endpoint returns appid only, so an unowned new item's title comes from `steam_store.fetch_app_name`.
- `dekudeals.py`: Nintendo/switch2 wishlist via a DekuDeals shared wishlist's public `.json` export (`DEKUDEALS_WISHLIST_URL`) — fuzzy-matched the same way as `backloggd.py`, since Nintendo's own eShop wishlist has no API. Confirmed export shape: `{"items": [{"name", "link", "added_at"}, ...]}` — no numeric/NSUID identifier anywhere (`link` is DekuDeals' own slug, unrelated to the `nintendo_title_id` used for VGCS ownership), so name matching is the only available bridge; each item's own `added_at` is used as `wishlisted_at`. Also scrapes current wishlist item prices via the shared wishlist HTML page (`fetch_wishlist_prices`).
- `itad.py`: IsThereAnyDeal price lookups for Steam/GOG/Epic-wishlisted games (Steam appid → best current deal across shops; requires `ITAD_API_KEY`).
- Other providers/syncs: `igdb.py`, `opencritic.py`, `metacritic.py`, `steamspy.py`, and the platform syncs `epic.py`, `gog.py`, `nintendo.py`, `psn.py` (plus `title_normalization.py`).
- Nintendo switch2 uses two complementary sources: `nintendo.py` provides **ownership** (VGCS digital library via session cookies; no playtime), and `nintendo_pctl.py` provides **playtime** via the Nintendo Switch Parental Controls API (`pynintendoparental`, plain OAuth — no Coral `f`-token). Parental Controls reports per-game minutes for any console registered to it, *ownership-agnostic*, so titles played on the console under another account are auto-added as owned switch2 games. `sync_nintendo` runs ownership then layers playtime on top (the playtime result is nested under `"playtime"`); per-game daily summaries — including the in-progress current day, whose counters refine through the day — accumulate idempotently in `nintendo_play_summary` (v12, natural PK), and the switch2 playtime total is their `SUM`. Each day's `players[].playedGames[]` entry nests game identity under `meta` (`applicationId`/`title`) with `playingTime` at the entry top level (see `_extract_rows`). Forward-only — Parental Controls has no retroactive history. Auth token comes from `set_nintendo_pctl_session` (`NINTENDO_PCTL_SESSION_FILE`).

- `scrape_config.py` / `scrape_validate.py` / `scrape_fixtures/`: the healable-scraper layer (see "Healable scrapers" below).

**`gamelib_mcp/integrations/`** — read-only integration status inspectors (`status.py` dataclasses, `inspectors.py` per-platform probes) surfaced by the `get_integration_status` tool and the `/admin/integrations*` routes. Which probes run, and under which name (switch2 is inspected as "nintendo"), derives from the platform registry.

**`gamelib_mcp/platforms_registry.py`** — the single registry of platforms (`PlatformSpec` + `PLATFORMS`). `PLATFORM_ALIASES`, `SYNCABLE_PLATFORMS`, `LIBRARY_PLATFORMS`, `WISHLIST_SYNCABLE_PLATFORMS`, `SYNC_METADATA_PLATFORMS`, and `INSPECTOR_PLATFORM_ALIASES` all derive from it (`tools/common.py`, `tools/admin.py`, and `lifecycle.py` re-export them under their long-standing names), and `run_library_sync`/`sync_wishlist` build their platform→sync-fn dicts from it via `resolve_platform_functions`. Sync/inspector functions are referenced as `(module, attr)` strings resolved lazily — the registry itself imports nothing, so no cycles — and resolution prefers names bound on a caller-supplied namespace (`tools/admin.py` passes itself), which keeps the `patch("gamelib_mcp.tools.admin.sync_epic", ...)` test pattern working. Adding a platform = write `data/<platform>.py` + add one `PlatformSpec` (plus an inspector probe in `integrations/inspectors.py` if it should appear in integration status).

### Database (SQLite via aiosqlite)

Core tables, auto-migrated on startup in `db.init_db()`:
- `games`: canonical game rows and shared enrichment fields
- `game_platforms`: ownership/playtime per platform — a row here always means a real platform relationship (owned, or a manual stub); never a wishlist-only entry
- `game_platform_identifiers`: provider-specific IDs such as `steam_appid` and `gog_product_id`
- `game_wishlist` (v16): "want to play" tracking, deliberately separate from `game_platforms` — a wishlist item may have no owned-platform row at all yet. `UNIQUE(game_id, platform)`; `source` records where it came from (`steam`, `dekudeals`, `manual`); `store_identifier` captures the store's own ID (e.g. a Steam appid) at wishlist-sync time — needed for unowned items with no `game_platforms` row
- `game_prices` (v18): current-price cache per game+platform+shop, refreshed by `get_wishlist_deals`; rows are overwritten in place (not history — ITAD is the historical source of record)
- `steam_platform_data`: Steam-only provider metadata
- `game_platform_enrichment`: cross-platform review/release enrichment
- `nintendo_play_summary`: per-(device, application, day) Switch playtime from the Parental Controls API (v12); the switch2 `playtime_minutes` total is the `SUM` of these rows (see `nintendo_pctl.py`)
- `scrape_config` (v17): versioned scrape-descriptor overrides per provider (see "Healable scrapers" below) — at most one `active` row per provider; an empty table means every provider runs on code defaults
- `game_series` / `game_series_membership`: normalized series tracking (IGDB collections + franchises) with a many-to-many membership junction; populated during IGDB backfill and surfaced/filterable via the `series` field on search/detail/stats tools
- `ratings`: normalized 1–10 scores from Backloggd (weight 1.0), manual `rate_game` ratings (weight 1.0), and Steam (weight 0.5)
- `tag_affinity`: precomputed per-tag preference scores (drives recommendations)
- `meta`: key-value store (last sync timestamp, etc.)

WAL mode enabled, foreign keys on.

### Key Design Patterns

- **Lazy enrichment**: `get_game_detail` fetches available provider-specific enrichment on demand and caches results. Bulk library calls skip unenriched fields.
- **Tag affinity**: After `sync_ratings` or `rate_game` (and after each background enrichment pass that processes rows), weighted tag scores are recomputed across all rated games (Steam feature flags from `data/tags.py` are excluded — they live in `games.features`, not `games.tags`). `discover_games` ranks unplayed games by these scores and explains each result via `matched_tags`.
- **Tag vocabulary**: `games.tags` is the rich, cross-platform tag cloud. For Steam games it comes from **SteamSpy community tags** (`steamspy.py`), not the Steam genre list — `steam_store.enrich_game` only *seeds* tags when null (`tags = COALESCE(tags, ?)`) so it never clobbers SteamSpy/IGDB. IGDB themes/keywords are *unioned* into existing tags (`igdb._merge_igdb_tags`, capped at `MERGED_TAG_CAP`), not gated on null. All tag writers and the affinity/discover/library query inputs run tags through `data/tag_synonyms.py::canonical_tag` (lowercase + a small synonym map, e.g. `soulslike`→`souls-like`); on a miss it returns plain lowercase to stay consistent with the SQL `lower(value)` joins. The v12→v13 migration canonicalizes existing tags in place and re-claims SteamSpy/IGDB enrichment for Steam games.
- **Rate limiting**: HLTB pre-warm uses an asyncio semaphore to avoid hammering the API.
- **Fuzzy matching**: Title matching uses rapidfuzz/stdlib helpers where provider identifiers are unavailable.
- **Game identity (anti-collapse)**: Name is only a *cross-platform* reconciliation key, never a within-platform one — two distinct same-platform store IDs (e.g. Steam appids for Dead Space 2008 vs the 2023 remake) must stay separate. Each sync resolves by its stable store identifier first (Steam appid in `bulk_upsert_steam_library`; a `get_game_by_identifier` short-circuit in `epic`, `psn`, `nintendo`, and `nintendo_pctl`), and the name/fuzzy fallback refuses to attach onto a row that already owns that platform (`find_game_by_name_fuzzy(exclude_platform=...)`, `resolve_and_link_game(platform=...)`, and the `NOT EXISTS … gp.platform='steam'` guard in the bulk resolver) plus rejects release-year conflicts (`reference_release_date`). `upsert_game(match_existing_by_name=False)` opts the create-new terminal out of the name fallback. GOG has no stable per-item store ID, so it relies on IGDB `igdb_id`/name and is the known exception. `detect_collapsed_games` (within-platform) and `detect_cross_platform_collapses` (cross-platform, via IGDB external_games) surface pre-existing over-merges; `split_game` peels them apart manually (the inverse of `merge_games`). There is no *automatic* split — playtime can't be reliably re-attributed without a re-sync, which Steam supports per-appid.
- **Manual overrides**: `update_game` writes user-edited `games` columns and records their names in `games.manual_overrides` (JSON array). Sync/enrichment writers (`steam_store`, `steamspy`, `hltb`, `igdb`, the bulk Steam name update, and `detect_farmed_games`) consult `get_manual_overrides` and skip those columns, so a hand edit survives later syncs. Protection is revocable: `update_game(clear_overrides=[...])` removes columns from `manual_overrides` (via `remove_manual_overrides`) so sync can manage them again.
- **Healable scrapers**: the four brittle scrape providers (`backloggd`, `steam_reviews`, `metacritic`, `dekudeals`) split their *declarative* surface — URL templates, CSS selectors, regexes, JSON keys, cache TTLs, pagination caps, fuzzy cutoffs — into frozen dataclasses in `data/scrape_config.py`, loaded per sync via `load_scrape_config(provider)` (active `scrape_config` row merged over code defaults; any load/parse error fails open to defaults so a bad override can never crash a sync). The vocabulary is data-only and validated per field kind: URL hosts are frozen to per-provider allowlists (`ALLOWED_HOSTS` — an override can restyle a path but never point at another site), selectors must compile under soupsieve, regexes must compile with required capture groups, everything is length/bounds-capped. The calling AI heals a broken scrape via the `tools/scrape_admin.py` tools; `propose_scrape_config` persists nothing unless `data/scrape_validate.py`'s gate passes: structural check → replay of the recorded pages in `data/scrape_fixtures/` (ships in the package; stale-fixture failures only warn when the live trial passes) → live trial + history sanity checks (scraped titles must fuzzy-overlap the library, appids must resolve to owned Steam games, a re-fetched Metascore must be within ±20 of the stored one) — the defense against wrong-but-plausible selectors silently corrupting data. Overrides are versioned with provenance (`source='ai_heal'|'manual'`, note, validation report) and `rollback_scrape_config` walks back one version per call; defaults are always recoverable. The *imperative* parts deliberately stay code and are not healable: Backloggd's title sibling-walk, steam_reviews' `_compute_score` fusion, Metacritic's JSON-LD `bestRating==100` guard, all of OpenCritic/IGDB (auth, backoff, multi-source fallback), and every reconciliation guard. Parser changes must keep `tests/test_scrape_parsers.py` and the fixture expectations in `scrape_validate.py` in sync with the fixture pages.
- **Single-user by design**: one deployment = one owner = one library; see docs/adr/0001-single-user.md before adding any per-user parameter or table.
- **Wishlist tracking**: `game_wishlist` records "wanted" in its own table rather than as a flag on `game_platforms` — a wishlist item may not be owned anywhere yet, and `game_platforms` rows are meant to mean a real platform relationship exists, so overloading `owned=0` there would blur that invariant (and, since `upsert_game_platform`'s `ON CONFLICT` unconditionally overwrites `owned`, risk a later sync silently un-owning an already-owned row). `db.clear_fulfilled_wishlist_entries` deletes a wishlist row once `game_platforms` shows it's actually owned on that platform — the same "purchase clears the wishlist" behavior storefronts like Steam implement — and runs after every `refresh_library`/`sync_wishlist` call, plus inline in `add_game_to_platform`. `db.delete_stale_wishlist_entries(platform, source, keep_game_ids)` handles the other direction — a title removed from the upstream wishlist without being bought — scoped to `(platform, source)` so it never touches manual entries or another source's rows; both `steam_wishlist.fetch_wishlist` and `dekudeals.sync_dekudeals_wishlist` call it, but **only** after confirming every fetched item resolved to a `game_id` this round (a failed HTTP fetch propagates instead of being swallowed to `[]`, and for Steam specifically, any single unresolved item — e.g. a rate-limited `fetch_app_name` lookup — skips the whole reconciliation pass), since an empty/partial fetch could otherwise be mistaken for "the wishlist is now empty" and wipe real entries. `sync_wishlist` covers Steam (official Web API) and switch2 (DekuDeals shared-wishlist scrape); PSN has no wishlist API, so `add_game_to_platform(owned=False)` is the only path for it. `get_wishlist` reads `game_wishlist` directly, optionally filtered by platform, and reports live ownership state as a diagnostic (normally False; True means a fulfillment cleanup just hasn't run yet).
