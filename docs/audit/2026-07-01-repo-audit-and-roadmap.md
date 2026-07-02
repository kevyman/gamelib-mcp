# Repo Audit & Roadmap — 2026-07-01

Scope: full-repo review — architecture, code quality, tests, security, ops, and
forward direction. Test suite at time of audit: **558 tests + 89 subtests, all
passing (~54s)**.

## Status (updated 2026-07-01)

**Fixed in PR #41 (merged):** PR CI workflow (gap 2); timing-safe auth compare;
tracked/cancellable
startup ratings task (gap 6); fresh-DB init message now uses `SCHEMA_VERSION`;
deploy.yml concurrency comment; root `test.py` moved to
`scripts/seed_v1_sample_db.py`; Dockerfile `HEALTHCHECK`. The httpx-timeout nit
was dropped as moot — every steam_store request already passes an explicit
per-request timeout through a shared helper.

**Fixed in the follow-up gap-resolution PR:**

- **Static analysis (gap 1)** — ruff + mypy in the dev group, configured in
  `pyproject.toml`, both gating CI alongside pytest. All pre-existing findings
  fixed (unused imports, misplaced module-level statements, bs4 attribute
  narrowing, missing annotations); the intentional patterns are configured,
  not suppressed inline: the `data/db` façade's re-exports/late imports and
  `main.py`'s post-dotenv imports get per-file ignores, and main.py's
  dict-vs-response-model returns (validated by FastMCP at runtime) get a
  documented `return-value` override.
- **Pre-migration DB snapshot (gap 3, app half)** — `_run_migrations` now
  writes `gamelib.db.pre-v{N}.bak` via `VACUUM INTO` (atomic, WAL-safe) before
  any schema-changing migration; skipped for fresh or already-current DBs. A
  snapshot failure aborts the migration rather than proceeding without the
  safety net. The server half (nightly `sqlite3 .backup` cron + off-machine
  copy) is documented in deploy.md → "Database backups" — it still needs to be
  set up **on the Hetzner box by hand**.
- **LICENSE (gap 4)** — MIT added. Owner should confirm the copyright line
  (currently "kevyman") or swap the license entirely if all-rights-reserved
  was the intent.
- **`/health` platform coverage** — expected platforms are now derived from
  sync history (`integration_sync_{platform}_last_success_at` in meta) instead
  of a hardcoded five-platform list: a platform only counts as "missing" if it
  has synced successfully before and now reports zero owned games.

**Fixed on 2026-07-02:**

- ✅ **Non-root container** (PR #45) — the app container runs as UID 10001;
  server data dirs were chowned first, then the change deployed. Verified in
  production: container uid 10001, healthy, DB writable.
- ✅ **Nightly backup cron** — installed on the server
  (`/etc/cron.d/gamelib-backup`, 04:15 UTC `sqlite3 .backup` + a copy owned by
  a dedicated key-only `gamelib-backup` user). Off-machine leg: the home
  Windows machine pulls via scp on a daily scheduled task (installed and
  verified 2026-07-02; details in deploy.md → "Database backups"). **The
  audit's backup gap is fully closed.**
- ✅ **OAuth** (from "Non-obvious improvements" #4) — PR #44 replaced the
  static bearer token with FastMCP's GitHub OAuth 2.1 proxy.
- ✅ **Decide on single-user (roadmap item 7)** — recorded as
  [docs/adr/0001-single-user.md](../adr/0001-single-user.md): single-user is
  an explicit non-goal, cross-referenced from CLAUDE.md and README.md.

**Fixed in PR #46 (2026-07-02):** all remaining "Non-obvious improvements" —
table-driven migrations (registry replaces the if-ladders), connection reuse
(opt-in checkout-exclusive per-loop pool, enabled by the server lifespan),
FTS5 (self-healing `games_fts` trigram derived index with LIKE-parity), and
Dependabot (uv + github-actions, weekly grouped).

**Still open:**

- ❌ The feature roadmap below.

## Overall verdict

This is an unusually healthy codebase for a personal project: clean layer
separation (`tools/` → `data/` → `data/db/`), a versioned 16-step migration
chain with state detection, claim-based background enrichment that survives
crashes, careful anti-collapse identity rules, and design docs for every major
feature. The gaps are almost all **operational/process**, not architectural.

## Glaring gaps

### 1. No static analysis at all
27k lines of async Python with zero lint or type checking (CLAUDE.md admits
"no lint framework configured"). Async code is exactly where a type checker
pays for itself (forgotten `await`s, `Row | None` access). CI runs only pytest.

**Fix:** add `ruff` (lint + format) and a type checker (`mypy` or `pyright`)
to the dev group, gate CI on them. An afternoon of setup; most churn will be
one-time.

### 2. No CI on pull requests
`deploy.yml` triggers only on push to `main` and manual dispatch. Every PR in
the git history (#26–#40…) merged with **zero automated test run**. The test
job only runs after the merge, immediately before a production deploy — the
worst possible time to learn about a failure.

**Fix:** a separate `ci.yml` on `pull_request` (tests + lint), keep deploy.yml
for main.

### 3. No database backup story — and some data is unrecoverable
`data/gamelib.db` holds data that cannot be re-synced:

- `nintendo_play_summary` — Parental Controls is **forward-only**; lose the
  file, lose all Switch playtime history permanently.
- manual ratings (`rate_game`), `manual_overrides`, hardware preference,
  manual platform entries, merge/split repairs.

Meanwhile the migration chain does destructive table rebuilds
(`ALTER TABLE … RENAME` → recreate → copy → drop) with no pre-migration file
copy, and the deploy flow auto-runs migrations on every push to main.

**Fix (cheap):** copy the DB file to `gamelib.db.pre-v{N}` inside `migrate_db`
before applying steps, plus a nightly `sqlite3 .backup` cron (or Litestream to
object storage) on the Hetzner box.

### 4. No LICENSE file
Public GitHub repo, no license — technically all-rights-reserved, so nobody
can legally use or contribute to it. Add MIT/Apache-2.0 (or a deliberate
proprietary note).

### 5. Auth hardening (small but real)
`http_admin.py`:

- Token accepted via `?token=` **query string** (line 107-109). Query strings
  land in Caddy access logs, browser history, and Referer headers. If a client
  actually needs it, document the tradeoff; otherwise drop it.
- Token comparison is `==`, not `hmac.compare_digest` — timing-safe compare is
  a one-line change.
- `MCP_AUTH_TOKEN` is read at import time, so a rotate requires knowing that a
  restart is needed (worth a comment at minimum).

**Resolved later:** the MCP endpoint now uses FastMCP's GitHub OAuth 2.1 proxy,
the query-token fallback was removed, and `/admin/*` uses a separate
header-only bearer token. OAuth and admin secret changes require recreating the
container so the process receives the new environment.

### 6. Fire-and-forget startup task can be garbage-collected
`lifecycle.py:502`:

```python
asyncio.create_task(_run_startup_ratings_sync())
```

The result is discarded. The event loop holds only a **weak** reference to
tasks; CPython may GC this task mid-run (a documented asyncio pitfall), and
shutdown doesn't cancel it (every other background task is tracked in a
module-level var and cancelled in the lifespan teardown). Track it like
`_RATINGS_SYNC_TASK` and cancel it on shutdown.

## Smaller defects / nits

| Where | Issue |
|---|---|
| `data/db/__init__.py:1138` | Fresh-DB message says "schema v12" but writes v16. |
| `.github/workflows/deploy.yml` | Concurrency comment claims in-flight test runs get cancelled, but `cancel-in-progress: false` cancels nothing. Comment and config disagree. |
| `test.py` (repo root) | Scratch script that hits the live Steam API and writes to `/tmp`; its name collides with pytest conventions. Move to `scripts/` or delete. |
| `http_admin.py` `/health` | Reports `degraded` whenever any of 5 hardcoded platforms has 0 owned games — wrong for anyone not using all five. Derive expected platforms from configured integrations instead. |
| `Dockerfile` / compose | Container runs as root; no `HEALTHCHECK`/compose healthcheck despite a perfectly good `/health` endpoint; `restart: always` can loop a crashing app invisibly. |
| `steam_store.py:231,342`, `enrich_bg.py:211` | `httpx.AsyncClient()` with implicit default timeout while every other module sets one explicitly. Works (httpx defaults to 5s) but inconsistent. |

## Non-obvious improvements

1. **Table-driven migrations.** `_run_migrations` is a ~170-line if-ladder
   (`if version == 1: … if version == 2: …` plus a parallel elif-ladder for
   version-0 state recording). A `MIGRATIONS: list[tuple[int, Callable]]`
   registry collapses it and makes adding v17 a one-liner. Purely mechanical.

2. **Connection-per-call SQLite.** Every `get_db()` opens a fresh aiosqlite
   connection (which spawns a thread) — hot tools do this several times per
   request, and `enrich_bg.py` already carries "database is locked"
   defer-machinery, which is a symptom of many concurrent short-lived writers.
   A per-event-loop cached connection (mirroring the existing per-loop lock
   pattern) or a small pool would cut latency and lock contention.

3. **FTS5 for search (later).** The tiered LIKE/token-AND search is O(n) per
   query. Fine at current library scale; if the library grows or search feels
   slow, SQLite FTS5 (trigram tokenizer) subsumes the prefix/substring tiers
   and improves misspelling tolerance beyond the single-candidate rapidfuzz
   fallback in `fuzzy_fallback_game_ids` (which today returns at most one id).

4. **MCP spec auth (OAuth 2.1).** Static bearer works today, but remote MCP
   clients are converging on the spec's OAuth resource-server flow.
   FastMCP has support for this; worth adopting before more clients connect.

5. **Renovate/Dependabot.** `uv.lock` is frozen (good) but nothing bumps it.
   Pinned scraper-adjacent deps (psnawp, pynintendoparental, howlongtobeatpy)
   rot fast as upstream sites change.

## Roadmap — where to go next

Ordered by value-for-effort:

1. **Process week (do first):** PR CI + ruff/type-check + LICENSE + DB
   backups + the small fixes above. Everything else builds on a repo that
   can't silently break.

2. **Wishlist price tracking / deal alerts.** The v16 wishlist is the obvious
   foundation: IsThereAnyDeal's API covers Steam/GOG/Epic prices in one place,
   and DekuDeals (already scraped) has Switch prices. A
   `get_wishlist_deals` tool ("which wishlist games are under $20 right now?")
   turns the wishlist from a list into a purchase advisor — the same move
   `discover_games` made for the backlog.

3. **Completion status.** `play_state` is inferred purely from playtime;
   there's no "completed", "abandoned", or "currently playing". A user-set
   status (with an HLTB-vs-playtime heuristic suggesting candidates:
   playtime ≥ hltb_main ⇒ probably finished) makes `get_backlog_stats` honest
   and lets discovery stop recommending abandoned games.

4. **Series gap analysis.** `game_series` + IGDB already exist — but only for
   owned games. An IGDB-backed `discover_series_gaps` ("unowned entries in
   series I own and rate highly") combines taste affinity with franchise data
   and naturally feeds the wishlist. Also enables "a new game just released in
   a series you love" on the periodic refresh.

5. **Generalized playtime history.** `nintendo_play_summary` proves the
   per-day model; Steam's 2-week deltas and PSN last-played can be snapshotted
   per sync into a generic `play_history` table → "what did I play this
   month" / year-in-review, which the current schema can't answer.

6. **Xbox sync.** The platform enum already accepts `xbox` manually; OpenXBL
   makes automated ownership+playtime feasible, following the existing
   `data/<platform>.py` + inspector pattern.

7. **Decide on single-user.** `STEAM_ID` etc. are process-level env vars and
   every table assumes one owner. That's fine — but each new feature hardens
   the assumption, so write it down as an explicit non-goal (or plan the
   `user_id` column now, cheaply, while tables are small).
