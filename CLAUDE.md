# CLAUDE.md

gamelib-mcp is a [Model Context Protocol](https://modelcontextprotocol.io/) server giving AI assistants tools to manage a cross-platform game library, enriched from external sources (HowLongToBeat, ProtonDB, IGDB, Backloggd, Steam reviews) with personalized discovery via tag-based affinity scoring.

## Commands

```bash
uv sync                                           # install deps (uv package manager)
uv run python -m gamelib_mcp.main                 # run locally (Streamable HTTP :8000)
.venv/bin/python -m pytest                        # tests — the local venv is the reliable runner here
.venv/bin/python -m pytest tests/test_igdb.py -q  # focused test file
.venv/bin/ruff check gamelib_mcp tests scripts    # lint (gates CI)
.venv/bin/mypy gamelib_mcp                        # types (gates CI; covers gamelib_mcp only, not tests/)
docker compose --profile prod up -d --build       # production (Caddy reverse proxy)
```

Tool config lives in `pyproject.toml` (`dev` dependency group, `[tool.ruff]`/`[tool.mypy]`). CI (`ci.yml`) runs ruff, mypy, and pytest on every PR.

Test gotcha: in Codex sandboxing, aiosqlite tests can hang at `aiosqlite.connect()` or early migration setup (the thread-safe event-loop callback never resumes the awaiting coroutine). Re-run outside the sandbox before changing test fixtures or DB paths. DB tests use temp SQLite files; no checked-in `data/gamelib.db` needed.

## Environment

Copy `.env.example` → `.env` for production (OAuth required) or `.env.local.example` → `.env` for localhost-only dev. `MCP_AUTH_MODE` must be explicit (`oauth` or `disabled`) — the server fails closed otherwise.

- `STEAM_API_KEY`, `STEAM_ID` — required.
- `DATABASE_URL` — leave unset for normal dev (defaults to `data/gamelib.db`). If `./gamelib.db` exists in the repo root, it is stale — delete it.
- Production OAuth: `MCP_PUBLIC_BASE_URL`, `GITHUB_OAUTH_CLIENT_ID`/`_SECRET`, `MCP_OAUTH_JWT_SIGNING_KEY`, `MCP_OAUTH_GITHUB_USER_IDS` (comma-separated), `FASTMCP_HOME`.
- `MCP_ADMIN_AUTH_TOKEN` — independent header-only bearer token gating `/admin/*`.
- `MCP_ALLOWED_ORIGINS` — browser origins allowed on the HTTP surface; requests without an `Origin` header (native/CLI clients) still pass.
- Optional: `PORT` (default 8000); `DEKUDEALS_WISHLIST_URL` (switch2 wishlist source — Nintendo has no wishlist API); `ITAD_API_KEY`/`ITAD_COUNTRY` (Steam/GOG/Epic prices for `get_wishlist_deals`; without a key those land in `unpriced`); `SCRAPE_HEAL_REQUIRE_APPROVAL=1` (validated scrape overrides land `pending` instead of auto-activating).

## Architecture

Dependency direction is a clean DAG: `main → lifecycle`, `main → http_admin`, `tools.admin → lifecycle`; `lifecycle` reaches `tools.admin.refresh_library` lazily (no top-level import) to avoid a cycle.

Top-level modules:
- `main.py` — FastMCP app, tool registration (the `@mcp.tool()` signatures/docstrings ARE the wire schema), security/OAuth wiring; the entry point.
- `auth.py` — fail-closed `SecurityConfig`, GitHub OAuth provider, `AuthMiddleware` restricting tool access to the configured GitHub user ID(s).
- `lifecycle.py` — lifespan + all background orchestration: startup refresh, background enrichment, periodic refresh loop, per-event-loop locks, sync metadata.
- `http_admin.py` — origin-allowlist middleware + `/health` and `/admin/integrations*`. `/mcp` is authenticated by FastMCP's OAuth provider, not this middleware.
- `apps.py` — MCP Apps game-cards widget for `discover_games`/`get_game_detail`. The `ui://` URI is content-hashed because hosts cache ui:// resources by URI (a stable URI left claude.ai stale across deploys). The HTML is dependency-free (hand-rolled postMessage bridge, no CDN); CSP allowlists only the two cover-art hosts. Rank badges render only when the payload carries `offset`. Preview with `scripts/preview_game_cards.py` (no MCP host needed).

### `tools/` — MCP tool handlers

- `library.py`: search_games, search_games_batch, get_library_stats
- `detail.py`: get_game_detail (triggers lazy enrichment)
- `discover.py`: discover_games (vibe filters + taste/critic/value ranking)
- `ratings.py`: sync_ratings, rate_game, get_ratings, get_taste_profile
- `stats.py`: get_backlog_stats
- `series.py`: get_series_breakdown, discover_series_gaps
- `completion.py`: suggest_completion_status — read-only heuristic, never writes; a human confirms via update_game
- `deals.py`: get_wishlist_deals — honors `hardware_preference` with a `preference_override_ratio` escape hatch; 12h TTL, `refresh=True` forces live fetch
- `history.py`: get_play_history (see playtime-history pattern below)
- `admin.py`: refresh_library, sync_wishlist, detect_farmed_games, detect_collapsed_games (read-only, within-platform over-merges), detect_cross_platform_collapses (via IGDB external_games), split_game (inverse of merge_games), set_nintendo_session, set_nintendo_pctl_session
- `platforms.py`: get_platform_breakdown, get_wishlist, set_hardware_preference, add_game_to_platform (`owned=False` = manual wishlist entry — the only path for PSN), update_game (manual edits recorded in `games.manual_overrides`)
- `integrations.py`: get_integration_status
- `scrape_admin.py`: scrape-heal tools — get/diagnose/propose/approve/rollback_scrape_config
- `common.py`: shared subqueries + platform-alias resolver. The three `_GAME_ROLLUP_CTE` variants deliberately stay in their own modules; they differ.
- `search.py`: tiered name-match SQL (exact > prefix > substring > token-AND over `name_normalized`) + fuzzy fallback; used by search, detail, and rate_game.

### `data/` — fetching and caching (all async)

- `db/`: SQLite package. `__init__.py` holds connection/migration/init and re-exports everything — `gamelib_mcp.data.db.<name>` is the stable public API. Submodules: `schema.py`, `claims.py` (enrichment claiming), `queries.py`, `upserts.py`, `affinity.py`, `fuzzy.py`.
- `enrich_bg.py`: background enrichment orchestration.
- Providers: `steam_xml.py`, `steam_store.py` (7-day cache), `steam_reviews.py`, `hltb.py` (30-day), `protondb.py` (30-day), `backloggd.py`, `igdb.py`, `opencritic.py`, `metacritic.py`, `steamspy.py`, `itad.py`.
- Platform syncs: `epic.py`, `gog.py`, `nintendo.py`, `psn.py`, `xbox.py` (+ `title_normalization.py`). Xbox uses OpenXBL (`OPENXBL_API_KEY`, optional `OPENXBL_XUID`); ownership = title history (documented approximation), playtime best-effort (`None` when unavailable, like GOG).
- Wishlists: `steam_wishlist.py` (official `IWishlistService/GetWishlist`; returns appid only, so unowned titles come from `steam_store.fetch_app_name`) and `dekudeals.py` (shared-wishlist `.json` export: `{"items": [{"name", "link", "added_at"}]}` — no NSUID anywhere, so fuzzy name matching is the only bridge; `added_at` becomes `wishlisted_at`). `dekudeals.py` also scrapes wishlist and search-page prices (shared selectors).
- Nintendo switch2 uses two complementary sources: `nintendo.py` = **ownership** (VGCS cookies, no playtime); `nintendo_pctl.py` = **playtime** (Parental Controls API, ownership-agnostic — titles played under another account get auto-added as owned). Daily summaries accumulate idempotently in `nintendo_play_summary`; switch2 total playtime is their `SUM`. Forward-only — no retroactive history exists.
- `scrape_config.py` / `scrape_validate.py` / `scrape_fixtures/`: the healable-scraper layer (pattern below).

### Other packages

- `integrations/`: read-only per-platform status probes behind `get_integration_status` and `/admin/integrations*` (switch2 is inspected as "nintendo").
- `platforms_registry.py`: the single registry of platforms. All platform lists/aliases derive from it. Sync/inspector functions are `(module, attr)` strings resolved lazily (no import cycles), preferring names bound on a caller-supplied namespace — which keeps the `patch("gamelib_mcp.tools.admin.sync_epic", ...)` test pattern working. Adding a platform = `data/<platform>.py` + one `PlatformSpec` (+ an inspector probe if it should appear in integration status).

### Database (SQLite via aiosqlite; WAL, foreign keys on)

Auto-migrated on startup in `db.init_db()`:
- `games`: canonical rows + shared enrichment. `igdb_platforms` = ownership-independent JSON of IGDB platform ids (Switch 2 = 508, Switch = 130, both → internal switch2). `completion_status` (nullable; `playing`/`completed`/`abandoned`/`evergreen`) is user-set only. `cover_image_id` = IGDB cover slug; `tools/common.py::cover_url` prefers it, falls back to the Steam capsule by appid.
- `game_platforms`: ownership/playtime per platform. A row here always means a real platform relationship — never a wishlist-only entry.
- `game_platform_identifiers`: provider IDs (`steam_appid`, `gog_product_id`, `xbox_title_id`, …).
- `game_wishlist`: want-to-play tracking, deliberately separate from `game_platforms` (see wishlist pattern). `UNIQUE(game_id, platform)`; `source` ∈ steam/dekudeals/manual; `store_identifier` captured at sync time for unowned items.
- `game_prices`: current-price cache, overwritten in place — not history (ITAD is the historical source of record).
- `steam_platform_data`, `game_platform_enrichment`: provider metadata / cross-platform enrichment.
- `nintendo_play_summary`: per-(device, application, day) Switch playtime.
- `play_history`: cumulative per-(game, platform) playtime snapshots, ≤1 row per UTC day, written post-sync only on change. Snapshots are totals, never deltas. Forward-only.
- `scrape_config`: versioned scrape overrides, ≤1 `active` row per provider; empty table = code defaults everywhere.
- `game_series` / `game_series_membership`: IGDB collections/franchises, many-to-many.
- `ratings`: 1–10 scores — Backloggd 1.0, manual 1.0, Steam 0.5 weight.
- `tag_affinity`, `meta`: precomputed tag scores; KV store.

## Key Design Patterns

- **Lazy enrichment**: `get_game_detail` fetches provider enrichment on demand and caches; bulk calls skip unenriched fields.
- **Tag affinity**: `recompute_tag_affinity` (after ratings changes and enrichment passes) builds per-tag scores from explicit ratings **plus** a 0.3-weight playtime pseudo-rating (owned, non-farmed, unrated, ≥2h; log-scaled, capped 9.5). Scores are mean-centered and shrunk: `affinity = Σw·(score − μ) / (Σw + 2.0)` — signed, so ubiquitous at-the-mean tags land near zero. `discover_games` scores a game as IDF-weighted mean affinity over **all** its tags (unrated = neutral dilution; IDF via the `gl_ln` SQL function), damped by `_MATCH_PRIOR`. Vibe filters only match tags within the first `VIBE_TAG_PROMINENCE_CUTOFF` entries of the vote-ranked tag list — GTA V's low-vote "racing" tag doesn't make it a racing game.
- **Tag vocabulary**: `games.tags` comes from SteamSpy community tags, not Steam genres — `steam_store.enrich_game` only *seeds* tags when null (`COALESCE`), never clobbers. IGDB themes/keywords are *unioned* in (capped at `MERGED_TAG_CAP`). All tag writers and readers run through `data/tag_synonyms.py::canonical_tag` (lowercase + synonym map; miss → plain lowercase to match SQL `lower()` joins). `STEAM_FEATURE_FLAGS` (`data/tags.py`) quarantines capability metadata ("save anytime", "achievements") out of the taste vocabulary.
- **Game identity (anti-collapse)**: name is only a *cross-platform* reconciliation key, never within-platform — two same-platform store IDs (Dead Space 2008 vs 2023) must stay separate. Every sync resolves by stable store identifier first; the name/fuzzy fallback refuses to attach onto a row already owning that platform (`exclude_platform`/`NOT EXISTS` guards) and rejects release-year conflicts. `upsert_game(match_existing_by_name=False)` opts a create-new terminal out entirely. GOG has no per-item store ID and is the known exception. Detection tools surface over-merges; `split_game` fixes them manually — there is deliberately no automatic split (playtime can't be re-attributed without a re-sync).
- **Manual overrides**: `update_game` records edited column names in `games.manual_overrides`; all sync/enrichment writers consult `get_manual_overrides` and skip those columns. Revocable via `update_game(clear_overrides=[...])`.
- **Completion status**: set only through `update_game` — no sync/enrichment writer ever touches it. `evergreen` = endless games (MMOs, sandboxes). `PLAY_STATE_SQL` treats explicit `completed` as `played` even with unknown playtime, but does *not* force `abandoned`/`evergreen` play state. Backlog/discovery exclude `completed`/`abandoned` but not `evergreen` (still recommendable); `get_backlog_stats`'s backlog-hours/best-unplayed queries *do* also exclude `evergreen`.
- **Playtime history**: `record_play_history_snapshots` runs after each platform sync (failure logs a warning, never fails the sync); writes only on change, never NULL playtime. `get_play_history` derives window deltas: `latest_in_window − baseline_before_window`, falling back to the first in-window snapshot (whose prior growth is excluded, not misreported). switch2 skips snapshot math entirely — served from `nintendo_play_summary` daily rows via `nintendo_title_id`; unmatched playtime is reported as `switch2_unmatched_minutes`, not dropped.
- **Series gap analysis**: `discover_series_gaps` = owned series/taste + live IGDB member lookup, cached in `meta` KV (7-day TTL; stale cache served on fetch failure). Matching is `igdb_id`-only — deliberately no fuzzy-name fallback, so run IGDB backfill first to avoid false-positive gaps. Per-series failures land in `errors`, not the whole call.
- **Healable scrapers**: the four brittle scrape providers (backloggd, steam_reviews, metacritic, dekudeals) keep their *declarative* surface (URLs, selectors, regexes, TTLs, caps) in frozen dataclasses in `data/scrape_config.py`, overridable via the versioned `scrape_config` table (load errors fail open to code defaults). URL hosts are frozen to per-provider allowlists; selectors/regexes must compile; everything is bounds-capped. `propose_scrape_config` persists nothing unless `scrape_validate.py` passes: structural check → fixture replay (`data/scrape_fixtures/`) → live trial + history sanity (titles fuzzy-overlap the library, appids resolve to owned games, Metascore within ±20 of stored). The *imperative* parts stay code and are not healable (title sibling-walks, score fusion, all of OpenCritic/IGDB, every reconciliation guard). Parser changes must keep `tests/test_scrape_parsers.py` and the fixture expectations in sync with the fixture pages.
- **Single-user by design**: one deployment = one owner = one library. Read docs/adr/0001-single-user.md before adding any per-user parameter or table.
- **Wishlist tracking**: separate table because a wishlist item may not be owned anywhere, and overloading `game_platforms.owned=0` would blur that invariant (and `upsert_game_platform`'s `ON CONFLICT` unconditionally overwrites `owned` — a later sync could silently un-own a row). Fulfillment: `clear_fulfilled_wishlist_entries` deletes a wishlist row once actually owned on that platform (runs after every refresh/sync + inline in add_game_to_platform). Removal: `delete_stale_wishlist_entries(platform, source, keep_game_ids)` is scoped so it never touches manual/other-source rows, and callers invoke it **only** after every fetched item resolved to a game_id this round (failed fetches propagate; for Steam, any single unresolved item skips the whole reconciliation) — otherwise a partial fetch could be mistaken for "wishlist is now empty" and wipe real entries. `sync_wishlist` covers Steam + switch2; PSN has no wishlist API (`add_game_to_platform(owned=False)` only).
