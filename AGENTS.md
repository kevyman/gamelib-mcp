# AGENTS.md

Instructions for coding agents that are not Claude Code — today that is Codex,
which reviews every pull request here and can push fixes when asked. Claude
Code sessions read `CLAUDE.md` (the same rules, plus orchestration); keep the
two in agreement when you change an invariant.

## Orientation

gamelib-mcp is a single-user Model Context Protocol server (FastMCP, aiosqlite)
that syncs one person's game library across stores, enriches it, and serves
33 tools to claude.ai / chatgpt.com. Architecture, environment and the
one-line rules live in `CLAUDE.md`; rationale in `docs/adr/` and
`docs/patterns/` (`docs/README.md` is the index).

```bash
uv sync                                          # deps (uv; Python 3.12 pinned)
.venv/bin/ruff check gamelib_mcp tests scripts   # lint (gates CI)
.venv/bin/mypy gamelib_mcp                       # types (gates CI)
.venv/bin/python -m pytest -q                    # ~2,400 tests, ~45 s on 4 cores
.venv/bin/python -m pytest tests/test_x.py -n0   # one file, serial, live logs
```

Test conventions are non-negotiable (see `docs/patterns/testing.md`): copy the
migrated template per test — never a per-test `init_db()`; `DEADLOCK_TIMEOUT`
on every `wait_for`; `virtual_clock` for anything that backs off; no timed
`asyncio.sleep` as a liveness assertion; no real network. In a Codex sandbox
aiosqlite can hang at `connect()` — rerun outside the sandbox before changing
fixtures or DB paths.

## Code Review Rules

Review the diff for consequential, repository-specific behaviour. Lint, types,
docstring length, response caps and doc/tool-name drift are enforced by CI
(`ruff`, `mypy`, `SchemaBudgetTests`, `ResponseSizeGuardTests`,
`test_docs_drift.py`) — do not spend findings on them. One precise finding
with a file:line and a concrete failure beats five plausible ones: every
finding is reproduced or refuted with a test before it is fixed, so a finding
that cannot be made to fail costs more than it saves.

### Severity here

- **P0** — data loss or corruption in `data/gamelib.db`, an auth or origin
  bypass, a credential or cookie written world-readable or logged, a migration
  that can leave the schema between versions, SQL built from caller strings.
- **P1** — a rule in this section violated; a failure path that is swallowed
  so the caller sees success; a bug fix without the test that would have
  caught it; a background task that can die silently; a provider call with
  no timeout.
- **P2** — report only when the fix is one line and you are certain.

### Identity and writes

- Write paths resolve a game by id or by EXACT name, or mint a new row
  (`add_game_to_platform`, `record_assessment`); fuzzy or partial matching is
  read-path only. A loose write files data onto a near-miss sibling silently
  (e.g. "Alan Wake 2" onto "Alan Wake"); a typo that mints is visible and
  repairable with `merge_games`.
- Name is only a cross-platform reconciliation key, never within one
  platform; syncs resolve by store identifier first, and the name fallback
  must refuse a row that already owns that platform and reject release-year
  conflicts. Nothing splits games automatically — `split_game` is manual by
  design.
- `games.manual_overrides` and `game_platforms.manual_overrides` are honoured
  by every sync and enrichment writer: an edited column is never overwritten.
  `completion_status` is set only through `update_game`.
- `is_primary_library_item` is always derived from `content_type`; a parent
  must stay primary, both ways; primary rows keep no parent.

### Ownership, wishlist, prices

- Ownership can end (`unowned_at`): that is `owned = 0` plus a stamp on an
  existing row, never a delete and never a mint; every aggregate filters
  `owned = 1`. `last_seen_in_source` is recorded, never acted on.
- The wishlist is its own table because an item may be owned nowhere; never
  overload `game_platforms.owned = 0`. `delete_stale_wishlist_entries` runs
  only after every fetched item resolved this round, so a partial fetch is
  never read as "wishlist is now empty".
- `game_prices` is a current-price cache with negative entries (NULL price):
  readers filter `price IS NOT NULL`; prices are never currency-converted;
  `history_low` is ITAD's number in ITAD's currency.

### Assessments and taste

- Recorded verdicts never feed `tag_affinity` or `discover_games`, and
  recording never writes the wishlist. `skill`/`skill_version`/`model` are
  declared-only claims — the server never fills them in.
- `affinity_score` has no fixed scale (`k` is estimated every recompute):
  flag any constant threshold compared against it, and any damping by
  `game_count`. Every tag writer and reader goes through `canonical_tag`.

### Database and concurrency

- `retry_on_write_contention` wraps idempotent sync writers only — never a
  function that mints rows from partially-committed state; read-then-write
  hot paths open `BEGIN IMMEDIATE`. A "database is locked" retry loop added
  anywhere else is a P1.
- Schema changes need all three: `_V{N}_SCHEMA_DDL` + `SCHEMA_VERSION` in
  `data/db/schema.py`, and a step in `data/db/migrations.py` — the fresh-DB
  path and the migration chain must agree (parity test).
- `play_history` rows are cumulative totals written only on change; readers
  of `last_played` treat NULL as unknown, never as "never played".

### Tools and the wire surface

- One tool per operation: bulk is `items=[...]` on the single-item tool,
  verb families take an `action`/`report` selector, a merged tool carries the
  STRICTEST annotation of anything it absorbs (a mode that hard-deletes or
  errors on repeat is not idempotent), and a multi-mode tool validates every
  selected mode's inputs before running the first one.
- Every list whose length scales with the library carries a cap, the true
  total and a truncation flag. Errors are `ToolError` with an actionable
  message; per-item errors inside `items=` results never fail the batch.

### Providers, security, operations

- Provider fetchers degrade to a logged failure that is COUNTED (see
  `enrich_bg.py`'s per-provider stats), never a silent success; every
  `httpx.AsyncClient` has an explicit timeout; scraper URLs stay inside the
  host allowlist with redirects re-checked per hop.
- `MCP_AUTH_MODE` fails closed; `/admin/*` is header-bearer only with a
  timing-safe compare; session files are written through
  `_write_private_json` (0600); the SQL escape hatch stays authorizer-enforced
  read-only. Secrets never appear in logs, tracebacks or tool responses.
- Deploy runs migrations at container start: a migration that cannot be
  snapshotted (`VACUUM INTO`) must abort rather than proceed.
