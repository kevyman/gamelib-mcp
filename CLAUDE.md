# CLAUDE.md

gamelib-mcp is a [Model Context Protocol](https://modelcontextprotocol.io/) server giving AI assistants tools to manage a cross-platform game library, enriched from external sources (HowLongToBeat, ProtonDB, IGDB, Backloggd, Steam reviews) with personalized discovery via tag-based affinity scoring.

Detail not needed every session loads on demand from `docs/adr/` (decisions) and `docs/patterns/` (rules moved out of this file) — see `docs/README.md`; `→` pointers below are relative to `docs/`.

## Commands

```bash
uv sync                                           # install deps (uv package manager)
uv run python -m gamelib_mcp.main                 # run locally (Streamable HTTP :8000)
.venv/bin/python -m pytest                        # tests — the local venv is the reliable runner here
.venv/bin/python -m pytest tests/test_igdb.py -q  # focused test file
.venv/bin/python -m pytest -n0                    # serial (readable output, live logs) when debugging
.venv/bin/ruff check gamelib_mcp tests scripts    # lint (gates CI)
.venv/bin/mypy gamelib_mcp                        # types (gates CI; covers gamelib_mcp only, not tests/)
docker compose --profile prod up -d --build       # production (Caddy reverse proxy)
```

Three `tests/conftest.py` conventions keep the suite fast and honest about time (→ patterns/testing.md):

- **Migrate once, copy per test.** A session fixture migrates a template DB that `ToolDBTestCase` copies — never re-add a per-test `init_db()`.
- **`DEADLOCK_TIMEOUT` for every `wait_for`.** Tight budgets measure machine load, not correctness; never assert liveness with a timed `asyncio.sleep` either — use an event that is never set.
- **`virtual_clock(module)` for anything that backs off.** The Steam/IGDB gates sleep for real; assert `clock.sleeps` instead.

`faulthandler_timeout = 300` (pyproject) backstops all three, dumping every thread's stack past 5 minutes; `pytest-xdist` runs all cores, so tests must share no mutable state. Under Codex sandboxing aiosqlite can hang at `connect()` — re-run outside it before changing fixtures or DB paths.

## Model orchestration (always-on)

Multi-model sessions follow `rules/router.md`. Per-model postures live in `rules/model-postures.md` and are injected into every main-session prompt by the `UserPromptSubmit` hook in `.claude/settings.json` (`.claude/hooks/inject_model_posture.py` picks the section matching the live model; `SessionStart` caches the model since prompt payloads don't carry it).

- The main session (Fable 5 at high effort) owns requirements, judgment, integration, and final verification. It does not inline-execute large builds: for a bounded, difficult implementation it writes the spec, dispatches the `opus-executor` agent, and verifies the result against that spec. An executor converging fast on an approved spec is desired behavior, not a defect.
- Every dispatch brief states the exact delta, scope, output, stopping condition, and exclusions.
- `sonnet-worker` handles fan-out needing per-item judgment (reader panels, audits, sweeps); `haiku-worker` handles bounded mechanical reads/transforms. Workers return extracted key numbers and paths, never raw dumps, and never delegate further.
- Never turn a partial search failure into a global conclusion: "not found in the path I checked" ≠ "does not exist". Look at the full context before deciding something is broken.
- Postures are duplicated into `.claude/agents/*.md` because subagents never see `UserPromptSubmit` — edit `rules/model-postures.md` and the matching agent file together; they must not drift.

## Environment

Copy `.env.example` → `.env` for production (OAuth required) or `.env.local.example` for localhost-only dev. `MCP_AUTH_MODE` must be explicit (`oauth` or `disabled`) — the server fails closed otherwise.

- `STEAM_API_KEY`, `STEAM_ID` — required.
- `DATABASE_URL` — leave unset for normal dev (defaults to `data/gamelib.db`). If `./gamelib.db` exists in the repo root, it is stale — delete it.
- Production OAuth: `MCP_PUBLIC_BASE_URL`, `GITHUB_OAUTH_CLIENT_ID`/`_SECRET`, `MCP_OAUTH_JWT_SIGNING_KEY`, `MCP_OAUTH_GITHUB_USER_IDS` (comma-separated), `FASTMCP_HOME`.
- `MCP_ADMIN_AUTH_TOKEN` — independent header-only bearer token gating `/admin/*`.
- `MCP_DUPLICATE_TEXT_CONTENT` — `1` restores the MCP spec's duplicate serialized-JSON text block on every tool result; off by default because both registered clients read `structuredContent` (halves response bytes). See `response_encoding.py`.
- `MCP_ALLOWED_ORIGINS` — browser origins allowed on the HTTP surface; requests with no `Origin` (native/CLI clients) still pass. oauth mode auto-allowlists `MCP_PUBLIC_BASE_URL`'s origin; local `disabled` mode must list `http://localhost:8000`.
- Optional: `PORT` (default 8000); `LOG_LEVEL` (DEBUG/INFO/WARNING/ERROR, default INFO — DEBUG surfaces per-item enrichment failures); `DEKUDEALS_WISHLIST_URL` (switch2 wishlist source; Nintendo has no wishlist API); `ITAD_API_KEY`/`ITAD_COUNTRY` (Steam/GOG/Epic prices for `get_wishlist(with_prices=True)`; without a key those land in `unpriced`); `SCRAPE_HEAL_REQUIRE_APPROVAL=1` (validated overrides land `pending`, not active).
- Optional session files (`import_purchases` + Nintendo syncs), populated **only** via `create_session_ingest_link`, whose single-use paste form keeps cookies out of the chat (`tools/admin.py::set_*_session` is its save path); defaults under `data/`. → patterns/sessions-and-sso.md
  - `NINTENDO_COOKIES_FILE` (`"nintendo"`) — the one accounts.nintendo.com session, driving Switch ownership *and* eShop purchases; `NINTENDO_PCTL_SESSION_FILE` (`"nintendo_pctl"`) — Switch playtime, an interactive sign-in rather than a cookie paste.
  - `STEAM_REFRESH_TOKEN_FILE` (`"steam_refresh"`, **preferred**) — long-lived, mints store cookies on demand; `STEAM_STORE_COOKIES_FILE` (`"steam_store"`) — legacy short-lived fallback, only when no refresh token is stored.
  - `EPIC_COOKIES_FILE` (`"epic"`) — website orders, not the Legendary launcher session that syncs ownership; `HUMBLE_COOKIES_FILE` (`"humble"`).

## Architecture

Dependency direction is a clean DAG: `main → lifecycle`, `main → http_admin`, `tools.admin → lifecycle`; `lifecycle` reaches `tools.admin.refresh_library` lazily (no top-level import) to avoid a cycle.

Top-level modules (rationale and measurements: → patterns/mcp-surface.md):

- `main.py` — FastMCP app, tool registration (the `@mcp.tool()` signatures/docstrings ARE the wire schema), security/OAuth wiring; entry point.
- `auth.py` — fail-closed `SecurityConfig`, GitHub OAuth provider, `AuthMiddleware` restricting tools to the configured GitHub user ID(s).
- `lifecycle.py` — lifespan + background orchestration: startup refresh, enrichment, periodic refresh loop, per-event-loop locks, sync metadata.
- `http_admin.py` — origin-allowlist middleware + `/health`, `/admin/integrations*`, `/ingest/{nonce}` (outside `/admin/`, so a browser navigation needs no bearer header); `/mcp` is authenticated by FastMCP's OAuth provider.
- `response_encoding.py` — `StructuredOnlyMiddleware` drops FastMCP's duplicate text block beside `structuredContent` (`MCP_DUPLICATE_TEXT_CONTENT=1` restores it).
- `session_ingest.py` — single-use cookie-paste links: in-memory nonce store (TTL 15 min, pop-on-success) + the `/ingest/{nonce}` form; a restart voids open links.
- `apps.py` / `apps_eval.py` — the two MCP Apps widgets (game cards; the evaluation card from `record_assessment`'s `package`). Content-hashed `ui://` URIs, no CDN, per-widget CSP, shared blocks in `apps_shared.py`; served HTML stays self-contained. Preview: `scripts/preview_{game_cards,eval_card}.py`.
- `skill_resources.py` — serves `skills/` as `skill://<skill-name>/<path>` + `skill://index.json` (ADR 0006) AND backs `get_skill`, from one disk scan so the two can't drift.

`skills/` (repo root) is the canonical home of the client-side gaming skills (`game-quality`, `backlog-triage`, `bundle-evaluation`) per ADR 0006; `~/.claude/skills` and claude.ai installs are copies.

Nested memory files load in their own directories: `tools/CLAUDE.md` (tool handlers), `data/CLAUDE.md` (fetching and caching).

### Other packages

- `integrations/`: read-only per-platform status probes behind `get_integration_status` and `/admin/integrations*` (switch2 is inspected as "nintendo").
- `platforms_registry.py`: the single registry of platforms — every list/alias derives from it; sync/inspector functions are lazily-resolved `(module, attr)` strings. A new platform = `data/<platform>.py` + one `PlatformSpec`.

### Database (SQLite via aiosqlite; WAL, foreign keys on)

Auto-migrated on startup in `db.init_db()`. Column semantics: → patterns/database.md.

- `games`: canonical rows + shared enrichment. `completion_status` (`playing`/`completed`/`abandoned`/`evergreen`) is user-set only; `cover_image_id` (IGDB slug) beats the Steam capsule.
- `game_platforms`: ownership/playtime per platform — always a real platform relationship, never a wishlist-only entry. Holds the 5 acquisition columns (user/importer-supplied only), `delisted`, `unowned_at`, `last_seen_in_source`, `manual_overrides`.
- `game_platform_identifiers`: provider IDs (`steam_appid`, `gog_product_id`, `xbox_title_id`, …).
- `game_wishlist`: want-to-play tracking, deliberately separate from `game_platforms`. `UNIQUE(game_id, platform)`; `source` ∈ steam/dekudeals/manual/assessment.
- `game_prices`: current-price cache, overwritten in place — not history. A NULL price is a *negative* cache entry: readers must filter `price IS NOT NULL`.
- `play_history`: cumulative per-(game, platform) playtime snapshots, ≤1 row per UTC day, written post-sync only on change. Totals, never deltas; forward-only.
- `game_assessments`: verdict components + `presentation` (model-authored card content) + declared-only provenance (`skill`/`skill_version`/`model`; NULL = unknown). Append-only, ≤1 row per (game, UTC day); never joined into affinity or discovery.
- `nintendo_play_summary` (per-device/application/day Switch playtime), `steam_platform_data` + `game_platform_enrichment` (provider metadata), `game_series` + `game_series_membership` (IGDB collections), `scrape_config` (versioned overrides, ≤1 `active` row per provider; empty = code defaults), `ratings`, `tag_affinity`, `meta`.

## Key Design Patterns

Each bullet is the rule; the reasoning and incidents live at the pointer.

- **Bounded responses**: every response field whose length scales with library size must carry a cap, the true total, and a truncation flag (`get_wishlist`: `limit`/`total_matches`/`has_more`). `tests/test_tool_dispatch.py::ResponseSizeGuardTests` fails on any list over its documented cap — add new read paths there. The schema side has the same guard: `tests/test_tool_registration.py::SchemaBudgetTests` caps the serialized `tools/list` payload and every tool's description — trim before adding, and keep field-level methodology in `skills/` (ADR 0006), not in docstrings. → patterns/mcp-surface.md
- **One tool per operation, not per arity** (ADR 0004): 33 MCP tools over ~50 impls in `tools/`. Bulk is `items=[...]` on the single-item tool, never a second `*_batch` tool; verb families take an `action`/`report` selector; a merged tool inherits the STRICTEST annotation it absorbs; a multi-mode tool validates EVERY selected mode's inputs before running the first one. Read docs/adr/0004 — especially its "Rejected" list — before adding or merging a tool.
- **Lazy enrichment**: `get_game_detail` fetches provider enrichment on demand and caches; bulk calls skip unenriched fields.
- **Tag affinity**: **`k` (the shrinkage prior) is estimated from the data every recompute, never hand-picked** (`estimate_shrinkage_weight`), so **`affinity_score` has no fixed scale** — compare tags to each other or to `strong_affinity_cut()`, never to a constant, and never damp it by `game_count`. `discover_games` uses IDF-weighted mean affinity over **all** a game's tags, `df` floored at `_IDF_DF_FLOOR`; vibe filters only match tags within the first `VIBE_TAG_PROMINENCE_CUTOFF` entries of the vote-ranked list. → patterns/tag-affinity.md
- **Tag vocabulary**: `games.tags` is SteamSpy community tags, not Steam genres — `enrich_game` only *seeds* them when null; IGDB themes/keywords union in (capped). Every tag writer and reader goes through `canonical_tag`; `STEAM_FEATURE_FLAGS`/`FEATURE_FLAG_PREFIXES` keep capability metadata out of the taste vocabulary. → patterns/tag-affinity.md
- **IGDB linking order**: `backfill_missing_games` resolves via `external_games` (Steam appid → game) first, then the stored igdb_id, then name — and overrides a STORED link only when the mapping's record and the library row agree on the name (`_igdb_name_agrees`), since the mapping is not infallible. A manual `igdb_id` outranks everything. → patterns/enrichment-and-igdb.md
- **Game identity (anti-collapse)**: name is only a *cross-platform* reconciliation key, never within-platform. Syncs resolve by store identifier first; the name/fuzzy fallback refuses a row already owning that platform and rejects release-year conflicts (GOG has no per-item store ID and is the known exception). `split_game` repairs over-merges by hand — there is deliberately no automatic split. → patterns/identity-and-nesting.md
- **Manual overrides**: `update_game` records edited columns in `games.manual_overrides` and every sync/enrichment writer skips them (revoke with `clear_overrides`). Same on `game_platforms.manual_overrides` for playtime/`last_played` (`set_playtime`) and `delisted`/`owned` (`add_game_to_platform`). → patterns/database.md
- **Ownership lifecycle** (ADR 0007): ownership can END — refund, revoked key, lapsed subscription — which is neither a wishlist entry nor a delete. `add_game_to_platform(unowned_at="YYYY-MM-DD")` retires an EXISTING row (`owned=0` + stamp, history kept; never mints, pins `owned`); every aggregate filters `owned = 1`. `last_seen_in_source` records what the source RETURNED (sync paths only) and nothing acts on it automatically. → adr/0007, patterns/ownership-and-wishlist.md
- **SQLite write contention**: WAL + `busy_timeout` cover a writer waiting on a writer, not `SQLITE_BUSY_SNAPSHOT` — a read-then-write transaction losing to a concurrent commit, reported as "database is locked". `retry_on_write_contention` wraps the idempotent sync writers and only they — never anything minting rows from partially-committed state — and `bulk_upsert_steam_library` opens each chunk `BEGIN IMMEDIATE`. → patterns/sqlite-contention.md
- **Completion status**: set only through `update_game` — no sync/enrichment writer touches it. `evergreen` = endless games; backlog/discovery exclude `completed`/`abandoned` but not `evergreen`. → patterns/playtime-history.md
- **DLC & nested content**: `is_primary_library_item` is always DERIVED from `content_type ∈ PRIMARY_CONTENT_TYPES`, never set independently. Both writers (`apply_content_classification`, `_apply_igdb_metadata`, kept in sync) enforce the guards: **a parent must stay primary, both ways**, the substance guard, the edition-ownership guard, primary rows keep no parent. → adr/0002, patterns/identity-and-nesting.md
- **Playtime history**: snapshots are cumulative TOTALS written post-sync only on change, so a *correction* to a total looks exactly like a play session. `get_play_history` suppresses a row whose `last_played` predates the window (`excluded_stale_games`), reading **the value frozen onto the END SNAPSHOT (`play_history.last_played`), never the live `game_platforms` column**. → patterns/playtime-history.md
- **`last_played` is a cross-platform signal, not a derived value** (`data/last_played.py`): "the last day this platform's own source says you played". Nothing computes it from playtime, and NULL means *unknown*, never "never played" — readers must branch on that. Sources degrade to NULL (`coerce_last_played_date`) rather than break a sync. → patterns/playtime-history.md
- **Series gap analysis**: `discover_series_gaps` matches on `igdb_id` only — no fuzzy-name fallback, so run IGDB backfill first; members cached in `meta` KV (7-day TTL). → patterns/enrichment-and-igdb.md
- **Healable scrapers**: only the *declarative* surface of the four brittle providers (URLs, selectors, regexes, TTLs, caps) is healable — code defaults overridden by the versioned `scrape_config` table, host-allowlisted and bounds-capped. `manage_scrape_config(action="propose")` persists nothing unless `scrape_validate.py` passes. → patterns/scrapers.md
- **Assessment recording** (ADR 0006 decision 5): `record_assessment` stores the COMPONENTS of a verdict, not a score; `skill`/`skill_version`/`model` are **declared-only claims, NULL = unknown**, never server-stamped. **Write paths never resolve a name loosely** — exact-or-mint like `add_game_to_platform`; fuzzy matching lives only on the read path. **Hard constraint: verdicts never feed `tag_affinity` or `discover_games`**, and recording never writes the wishlist. → adr/0006, patterns/assessments.md
- **Single-user by design**: one deployment = one owner = one library. Read docs/adr/0001-single-user.md before adding any per-user parameter or table.
- **MCP spec currency** (ADR 0005): track the latest STABLE FastMCP/mcp SDKs (protocol 2025-11-25 today) — never pre-release SDKs in prod. The app-layer rule keeping a bump cheap: no tool depends on per-connection state, `tools/list` never varies per-connection, and cross-call state is explicit handles. → adr/0005, patterns/mcp-surface.md
- **eShop session via accounts SSO**: the 1h eShop token is never stored — the importer replays a silent OAuth handshake off the **one** `accounts.nintendo.com` session (`NINTENDO_COOKIES_FILE`) per import; a `/login` redirect means that session expired → re-run `create_session_ingest_link(provider="nintendo")`. → patterns/sessions-and-sso.md
- **Wishlist tracking**: a separate table because a wishlist item may be owned nowhere — never overload `game_platforms.owned=0`. `delete_stale_wishlist_entries(platform, source, keep_game_ids)` never touches other-source rows and runs **only** after every fetched item resolved this round, so a partial fetch is never read as "wishlist is now empty". A manual write's `source` is `manual` or `assessment` only. → patterns/ownership-and-wishlist.md
