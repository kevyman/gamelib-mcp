# Implementation patterns

Rules and incidents moved out of the root `CLAUDE.md` on 2026-09-01 (repo audit
v3, §3 item 9), so they load when the relevant area is being worked on rather
than in every session. The root file keeps the rule; these files keep the why.
Decisions themselves live in `../adr/`.

| File | Covers |
| --- | --- |
| `testing.md` | The three `tests/conftest.py` conventions, the sandbox gotcha, xdist. |
| `mcp-surface.md` | HTTP/middleware modules, the two MCP Apps widgets, skills over MCP, bounded responses, tool consolidation (ADR 0004), spec currency (ADR 0005). |
| `database.md` | Table and column semantics: `delisted`, `unowned_at`, `last_seen_in_source`, `manual_overrides`, the `game_prices` negative cache, assessment provenance. |
| `tag-affinity.md` | The estimated shrinkage prior `k`, why `affinity_score` has no fixed scale, IDF, the tag vocabulary and its feature-flag quarantine. |
| `enrichment-and-igdb.md` | Lazy enrichment, IGDB linking order and the `_igdb_name_agrees` guard, series gap analysis. |
| `identity-and-nesting.md` | Anti-collapse identity rules and the DLC/nesting guards behind ADR 0002. |
| `ownership-and-wishlist.md` | The ownership lifecycle (ADR 0007) and wishlist tracking/reconciliation. |
| `sqlite-contention.md` | `SQLITE_BUSY_SNAPSHOT`, the retry decorator, and `BEGIN IMMEDIATE` on the Steam chunk path. |
| `playtime-history.md` | Cumulative snapshots and the stale-window gate, `last_played` coverage, completion status. |
| `assessments.md` | Assessment recording (ADR 0006 decision 5): declared provenance, exact-or-mint writes, read paths, the affinity firewall. |
| `scrapers.md` | The healable declarative surface and the propose/validate gate. |
| `sessions-and-sso.md` | Cookie/session ingest per provider and the Nintendo accounts-SSO handshake. |
- `review-and-merge.md` — shipping: one cross-model Codex review (rules in `AGENTS.md`), one refute-or-fix pass, squash merge; settings and sources.
