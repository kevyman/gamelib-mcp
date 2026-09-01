# Repo Audit v3 — 2026-09-01

Scope: full-repo audit against current published standards, measured rather
than eyeballed. Successor to `2026-07-01-repo-audit-and-roadmap.md` (process
gaps — all closed) and `2026-07-06-architecture-review-and-roadmap.md` (product
roadmap — items 1–4 still open, see §5). State at audit time on `main`
(`763d903`): ruff clean, mypy clean, **2,333 tests + 357 subtests green in 54 s**,
**86.5 % line / 81 % branch coverage** (measured with pytest-cov; not gated).

## 1. Method

How this audit was run, so the next one can repeat or improve it:

1. **Run the gates, don't trust the docs.** `uv sync --frozen`, ruff, mypy, the
   full suite, a coverage run, `pip-audit` over the exported lockfile, and an
   in-process `tools/list` measurement — all executed, numbers below are from
   those runs.
2. **Diff against the prior audits.** Every recommendation from 07-01 and 07-06
   was re-verified in code (§5), so this document reports drift, not just state.
3. **Fan out, then verify.** Six independent readers each took one dimension
   (security & supply chain, tests, architecture, MCP surface, ops/data,
   docs/DX) with a fixed output contract: `file:line` evidence, severity,
   confidence, concrete fix. The main session then re-verified every finding
   that ranks in the top ten before ranking it. No finding below is
   unevidenced; anything the readers disagreed on or could not verify is
   marked as such.
4. **Rank by value ÷ effort × confidence**, in the context this project
   actually has (single maintainer, single user, LLM-driven development,
   production server on the public internet behind OAuth). Box-ticking that
   would not change an outcome here is listed under "deliberately not
   recommended" rather than padded into the ranking.

Standards used as the bar: OpenSSF Scorecard checks (Pinned-Dependencies,
Token-Permissions, Dangerous-Workflow, Vulnerabilities, SAST, Security-Policy);
MCP spec security best practices (2026-07-28 revision: confused deputy, token
passthrough, state-handle hijacking, scope minimisation) and the 2025-11-25
tool-annotation semantics; Anthropic's "Writing effective tools for agents"
(consolidation, token efficiency, description quality, agentic evals); OWASP
ASVS / MCP cheat sheet; CIS Docker benchmark; the 2026 Python toolchain
consensus (ruff + mypy strict + coverage gate + pre-commit); SQLite production
practice (WAL, backup API, restore drills).

## 2. Verdict

This is a mature, unusually well-reasoned codebase for a single-maintainer
project. The application security layer (fail-closed auth, OAuth with
per-client consent, SQLite authorizer on the SQL escape hatch, SSRF-checked
scraper fetches, timing-safe comparisons), the test conventions
(migrate-once, `DEADLOCK_TIMEOUT`, `virtual_clock`, the thread-leak guard), the
migration safety net, and the ADR discipline are all genuinely above the bar.
Every process gap from the 07-01 audit is closed.

What has slipped is the **perimeter and the plumbing around the code**, not
the code: the lockfile carries six packages with published advisories (pyjwt
and starlette among them, both on the auth/HTTP path) and nothing in CI would
have noticed; the deploy job holds the production SSH key while running three
actions pinned by mutable tag with no `permissions:` block; long-lived store
credentials are written world-readable; a deploy that crashes at startup is
never detected. None of these are hard to fix and together they are the
highest-value work in the repo.

The second theme is **the MCP surface has outgrown its own measurement**:
the `tools/list` payload is 170 KB (~42 k tokens as an upper bound; ~24 k
tokens for the parts a host certainly forwards to the model), `record_assessment`
alone is 14 KB with 32 parameters, and one hard-delete mode is declared
idempotent. ADR 0004 bounded *responses* rigorously; the *schema* side never
got a budget or a tripwire.

Third, **the recommender still flies blind** — the 07-06 audit's first
roadmap item (an offline eval harness for `discover_games`) was never built,
while the affinity model has since gained an ANOVA-estimated shrinkage weight.

### Scorecard

| Dimension | Grade | One line |
|---|---|---|
| App-layer security | A- | Fail-closed config, OAuth consent, authorizer-enforced RO SQL, SSRF hops re-checked, `compare_digest` everywhere |
| Supply chain & CI security | C | 6 vulnerable packages in lockfile, no vuln/SAST gate, actions tag-pinned, no `permissions:` |
| Tests | A- | 2,333 tests, 86.5 % / 81 % coverage, strong conventions; no wire-level e2e, no coverage gate |
| Architecture & code quality | B+ | Zero import cycles, 0 TODOs, disciplined error handling; four 2–3 kLOC grab-bag modules, 899 duplicated widget lines |
| MCP surface | B | Exemplary response bounding and drift tests; 42 k-token schema with no budget, 3 annotation mismatches |
| Ops & reliability | B | Backups, deep `/health`, write-contention work, hang tripwire; no deploy gate/rollback, silent enrichment failures |
| Docs & DX | B- | ADRs and nested CLAUDE.md are excellent; README/.env stale by a month, root CLAUDE.md ~10.8 k tokens/session |
| Product measurement | C+ | Assessment calibration exists; `discover_games` ranking quality still unmeasured |

## 3. Ranked suggestions

Effort: **S** < 2 h · **M** half-day to a day · **L** multi-day. Confidence is
in the evidence and the fix, not in the priority.

### 1. Patch the vulnerable lockfile packages and add a vulnerability gate to CI — S, high

`pip-audit` over `uv export --no-dev` (2026-09-01):

| Package | Locked | Advisory | Severity | Fixed in | Reachable? |
|---|---|---|---|---|---|
| pyjwt | 2.12.1 | PYSEC-2026-175…179 | High 7.4 (JWK/HMAC key-confusion → token forgery) | 2.13.0 | FastMCP signs/verifies the MCP access tokens with it |
| cryptography | 46.0.5 | GHSA-537c-gmf6-5ccf + 5 PYSEC | High 7.5 (bundled OpenSSL DoS) | 48.0.1 | OAuth/TLS path |
| starlette | 1.2.1 | PYSEC-2026-248/249 | Medium 5.3 (`request.url.hostname` spoofable via path) | 1.3.1 | The HTTP framework under FastMCP |
| pydantic-settings | 2.13.1 | GHSA-4xgf-cpjx-pc3j | — | 2.14.2 | transitive |
| pygments | 2.19.2 | PYSEC-2026-2987 | — | 2.20.0 | transitive |
| soupsieve | 2.8.3 | PYSEC-2026-3071/3072 | — | 2.8.4 | bs4 selector path (scrape overrides) |

The open Dependabot PR #161 bumps five *direct* dependencies and none of these;
Dependabot's uv support does not raise transitive advisories reliably. Nothing
in `ci.yml` or `deploy.yml` scans for vulnerabilities (Scorecard
**Vulnerabilities**, **SAST**).

Fix: `uv lock --upgrade-package pyjwt --upgrade-package cryptography
--upgrade-package starlette …` today; add a CI step (`uvx pip-audit -r
<(uv export --frozen --no-dev)` or `osv-scanner --lockfile uv.lock`) that
fails the PR; optionally a weekly scheduled run on `main` so a new advisory
against an unchanged lockfile is still seen.

### 2. Harden the two workflows: SHA-pin actions, least-privilege token, post-deploy gate with rollback — S/M, high

- `.github/workflows/ci.yml:23,26` and `deploy.yml:20,23,41`: `actions/checkout@v7`,
  `astral-sh/setup-uv@v7`, `appleboy/ssh-action@v1` — mutable tags. The deploy
  job hands `appleboy/ssh-action` the production host, user and SSH key
  (Scorecard **Pinned-Dependencies**). Dependabot's `github-actions` entry
  already exists, so SHA pins stay current automatically.
- Neither workflow declares `permissions:` (Scorecard **Token-Permissions**).
  Add `permissions: contents: read` at the top of both.
- `deploy.yml` ends at `docker compose up -d --build && docker image prune -f`.
  A build that succeeds and a process that dies before `lifespan` yields is a
  silent `restart: always` crash-loop; recovery is manual SSH. Add a
  `curl --fail --retry` loop against `/health` (the endpoint already reports
  schema version and sync state) that fails the job, and on failure
  `git reset --hard <previous-sha> && docker compose up -d --build` so main
  stays deployable without a laptop. Keeping the previous image tag instead of
  pruning makes that rollback a few seconds.
- Lower priority in the same area: digest-pin `python:3.12-slim` and
  `caddy:alpine`; add `read_only: true`, `cap_drop: [ALL]`,
  `security_opt: [no-new-privileges:true]` to the app service (CIS Docker).

### 3. Write credential files with owner-only permissions — S, high

`tools/admin.py:556-558` (`_save_session_cookies`) writes every provider's
long-lived session (Nintendo account, Epic, Humble, Steam refresh token —
~200-day validity — Steam store, Nintendo PCTL) with `open(path, "w")` and no
`chmod`; `grep -rn chmod gamelib_mcp/` → 0. Under the container's default
umask that is 0644. Add `os.chmod(path, 0o600)` after the write and `0o700`
on `default_data_dir()`; the same for `data/steam_session.py`'s minted cookie
file if it persists one.

### 4. Put the tool-schema payload on a budget, and make the budget a test — M, high on measurement / medium on model impact

Measured in-process (`main.mcp.list_tools()` → MCP wire form):

| | bytes | share |
|---|---|---|
| descriptions (32 docstrings) | 77,178 | 45 % |
| output schemas | 63,843 | 37 % |
| input schemas | 19,940 | 12 % |
| **total `tools/list`** | **170,409** (~42.6 k tokens at 4 chars/token) | |

Top offenders: `record_assessment` 13.9 KB (7.1 KB description, 32 params),
`get_stats` 13.0 KB, `add_game_to_platform` 10.0 KB, `get_assessment_context`
9.3 KB, `get_game_detail` 9.1 KB. Ten tools exceed the ≤ 8-parameter
guidance ADR 0004 itself cites; the ADR's "known deviations" list stops at
`update_game` (23) and predates `record_assessment` (32) and
`get_assessment_context` (9).

Why it matters: CLAUDE.md says schema is "paid once per connect". For hosted
clients (claude.ai, chatgpt.com) tool definitions ride in the model context of
**every turn** of every conversation with the connector enabled; prompt caching
lowers the price but not the context occupancy. Descriptions + input schemas
(~24 k tokens) are certainly forwarded; whether a host forwards `outputSchema`
is host-specific (the Anthropic Messages API tool definition has no output
schema field), so treat 42 k as the upper bound and re-run ADR 0004's
differential probe before optimising that slice.

Fix, in order of leverage: (a) a test in `test_tool_registration.py` that
serialises the real `tools/list` and asserts total bytes and per-tool bytes
under a cap — the schema-side twin of `ResponseSizeGuardTests`; (b) move
methodology prose out of `record_assessment`/`get_assessment_context`/
`get_stats` docstrings into the `skill://` resources / `get_skill` they already
point at, keeping the "when to use vs. sibling" sentence and one example;
(c) split `record_assessment`'s void mode into its own tool (see #5), which
also shrinks its union schema; (d) for the biggest response models, trim
`Optional`-everything unions or opt FastMCP out of emitting `output_schema`
where the host ignores it. Also: `get_library_stats`'s docstring never
signposts `get_stats` for aggregate questions (one sentence).

### 5. Make tool annotations honest — S, high

- `main.py:1226` registers `record_assessment` with `MUTATION_TOOL`
  (`idempotentHint=True`), but `void_assessment_id` hard-deletes
  (`tools/assessment.py:1534`) and raises "not found" on a repeat call —
  precisely the shape `main.py:118-123` uses to justify
  `NON_IDEMPOTENT_MUTATION_TOOL` on `merge_games`/`delete_game`/`split_game`.
  ADR 0004's own "strictest annotation absorbed" rule is violated. Split the
  void into `void_assessment` carrying `NON_IDEMPOTENT_MUTATION_TOOL`.
- `get_game_detail` (`READ_ONLY_TOOL`) and `get_wishlist(with_prices=True)`
  (`DIAGNOSTIC_NETWORK_TOOL`, `readOnlyHint=True`) persist enrichment / price
  caches on a cold path. Either document the caveat at the annotation site as
  `VALIDATION_TOOL` already does, or move them to an annotation without
  `readOnlyHint`. (2025-11-25 spec: `readOnlyHint` = "does not modify its
  environment".)
- `ImportPurchasesResponse.sources` lists (`created_details`, `unmatched`,
  `skipped`, `bundles_needing_split`) carry no cap/truncation flag and the tool
  is absent from `ResponseSizeGuardTests.CONTRACTS`.

### 6. Add one real wire-protocol test and two tripwires — M, high

- No test drives `Client(main.mcp)` end-to-end with populated arguments; the
  ~40 `test_tools_*.py` files await the implementation coroutines directly,
  `test_tool_registration.py` introspects `list_tools()` in-process, and the
  single `_call_tool_mcp` round-trip (`test_response_encoding.py:59`) calls
  `get_sync_status` with no args. A pydantic serialisation break, a
  `Literal` coercion regression, or an auth-middleware interaction would pass
  all 2,333 tests. One file calling ~10 representative tools through the real
  client, asserting the deserialised shape, closes this.
- Fresh databases apply `_V39_SCHEMA_DDL` directly (`data/db/__init__.py:2295`)
  and never run the 38 chained steps; production came up the chain. Measured
  today the two produce the same 48 objects and identical column sets (five
  tables differ only in column order) — parity holds, but no test says so.
  A ~30-line test that builds both and diffs `sqlite_master` + `table_info`
  keeps it true.
- The `tools/list` byte budget from #4.
- Roughly 18 of 38 migration transitions are exercised only as "did not
  crash in the chain", not with a per-step data-preservation assertion; a
  table-driven per-step test would cover them cheaply.

### 7. Make background-enrichment failures visible — S, high

Every per-item worker in `data/enrich_bg.py` (six `except Exception` sites,
e.g. `:237-248`) logs the failure at `DEBUG` and still returns `1`, so
"HLTB worker complete: processed N rows" reads the same whether every fetch
succeeded or every fetch threw. `main.py:72` hard-codes `INFO` with no
`LOG_LEVEL` override, so those lines never appear in production. The only
human-visible signal for a provider silently breaking (a markup change at
HLTB, an expired IGDB token) is `check_library`'s `enrich.coverage`, which
nobody runs on a schedule. Count successes vs. failures per provider, log the
rate at `WARNING` above a threshold, add `LOG_LEVEL`, and consider exposing
last-run failure rates through `/health` or `get_integration_status`.

### 8. Bring README, `.env.example` and LOCAL_DOCKER back in sync, then keep them there — S, high

- `README.md` says "29 tools", lists 27, actual is 32; missing:
  `discover_series_gaps`, `get_assessment_context`, `record_assessment`,
  `get_skill`, `set_switch2_playtime_baseline`. The whole assessment feature
  (and the evaluation card) is invisible to a README reader. README last
  touched 2026-07-27; `main.py` 2026-08-31.
- `.env.example` refers to four tools that do not exist:
  `set_nintendo_pctl_session` (line 32, contradicted by line 33),
  `get_wishlist_deals` (48), `get_recommendations` (52),
  `propose_scrape_config`/`approve_scrape_config` (56-58).
- `LOCAL_DOCKER.md:3` links to `/home/john/code/gamelib-mcp/...`.
- Fix the text, then add a test asserting every backticked `*_*` name in
  README/.env.example that matches a tool pattern is a registered tool (the
  registration test already has the inventory), or generate the README table
  from the decorators.

### 9. Put the root CLAUDE.md on a diet — M, high

43,181 bytes (~10.8 k tokens) loaded into every session regardless of what is
being worked on; "Key Design Patterns" alone is 25.8 KB (60 %). Much of it is
incident narrative and rationale ("froze a production Steam sync for 3 days",
"Ghost of Tsushima read as 81 hours") that already has better homes: the
nested `tools/CLAUDE.md` (28.7 KB) and `data/CLAUDE.md` (11.9 KB), which load
only on demand, and ADRs 0002/0006/0007. Target ~15–18 KB: keep one-line
operational rules per pattern with a pointer, move the why into
`docs/patterns/*.md` or the relevant ADR. Move first: Assessment recording
(4.5 KB), DLC & nested content (3.0 KB), Tag affinity (2.2 KB), Wishlist
(2.1 KB), Playtime history (1.9 KB), SQLite write contention (1.6 KB), eShop
SSO (1.5 KB). Saves ~7 k tokens on every session for the rest of the
project's life; the model-orchestration hook injection (≤ 700 B) is already
cheap and needs no change.

### 10. Build the recommender eval harness (07-06 roadmap #1, still open) — M, medium

`discover_games` ranking has real tuning surface (`_MATCH_PRIOR`, the 0.3
playtime pseudo-rating weight, `_IDF_DF_FLOOR`, `VIBE_TAG_PROMINENCE_CUTOFF`)
and since 07-06 gained an ANOVA-estimated shrinkage `k` — with no
measurement. `scripts/eval_recsys.py` as designed there (leave-one-out over
the ratings table against a backup snapshot; Spearman, precision@10,
separation; `--baseline`) is still the right first step, and
`get_stats(report="calibration")` shows the project already knows how to do
this for assessments. Items 2–4 of that roadmap (purchase advisor, deal
alerts, `suggest_games_to_rate`) remain valid and unbuilt; the eval harness
is what makes the advisor's weights tunable rather than guessed.

### 11. Split `tools/admin.py` on its four seams, and do the long-deferred `db/engine.py` split — M, high

`tools/admin.py` (3,145 LOC) is four unrelated modules: 1,308 LOC of
ADR-0003-obsoleted detectors that `checks.py` already imports as adapters
(`detect_misclassified_dlc` is 524 LOC on its own), 975 LOC identity repair,
366 LOC session-file save paths that `session_ingest.py` reaches by
`getattr`, 351 LOC sync orchestration. Two pure moves (`tools/detectors.py`,
`tools/session_admin.py`) cut it to ~1,500 LOC with no logic change.
`data/db/__init__.py` grew from 1,936 to 2,637 LOC through ~22 migrations
since the 07-06 audit said "split it the next time migrations are touched";
`_MIGRATION_STEPS` plus the 38 step functions are 82 % of the file and belong
in `db/migrations.py` behind the existing façade. Also: `run_library_sync` and
`sync_wishlist` duplicate a ~30-line fan-out shape worth one helper; 24
functions exceed cyclomatic 25 (`add_game_to_platform` ≈ 69).

### 12. Stop `apps.py` / `apps_eval.py` drifting apart — M, medium

CLAUDE.md calls the two widgets "deliberately duplicated, not shared". Measured:
899 shared lines in 33 identical blocks ≥ 5 lines (~50 % of both files), and
the big blocks are functional JS — `trailerEntry` (110 lines), `playBadge`
(93), the carousel/lightbox stage (127), the popup-blocked-link fallback (42)
— not CSS resets. A fix to trailer selection in one file is now easy to
forget in the other and nothing tests they match. Either extract the shared
JS/CSS into one Python string both HTML builders include (keeping the
"self-contained widget" property at the served-HTML level), or add a test
that asserts the named shared blocks are byte-identical so drift fails CI.

### 13. Small reliability and hygiene fixes, one PR — S, high

- Four `httpx.AsyncClient()` constructions with no explicit timeout
  (`data/enrich_bg.py:210`, `data/steam_store.py:282,468,494`) — open since
  the 07-01 audit's nit; pass `timeout=`.
- `data/steam_licenses.py:304-306` opens a fresh client per probed appid
  (≤ 25 per audit run); thread the shared client through.
- Dead code: `prewarm_hltb`, `meets_min_tier`, `is_dekudeals_configured`,
  opencritic `is_configured` (zero references) plus five `is_*_configured`
  vestiges kept alive only by `patch(..., create=True)` in
  `test_startup_sync.py`; three unused lock globals in `lifecycle.py:40-44`.
- Seven test files (`test_hltb`, `test_protondb`, `test_enrich_bg`,
  `test_search_fts`, `test_steam_xml`, `test_integrations_http`; 12 call
  sites) still run `init_db()` per test against CLAUDE.md's rule; two tests in
  `test_tool_dispatch.py:389,393` assert nothing.
- Add `.coverage` to `.gitignore`; pin `.python-version` (CI ran on 3.11 here,
  prod is 3.12 — test both or pick one).

### 14. Run the restore drill and write it down — S, high

Open since 07-06. Nightly `sqlite3 .backup` + off-machine copy exist; no
`scripts/restore*`, and deploy.md's only restore text covers the pre-migration
snapshot. One scripted restore into a scratch dir + `init_db()` + row-count
sanity check, recorded in deploy.md, turns "backups exist" into "backups
work".

### 15. Turn on `disallow_untyped_defs` now; go strict per-module later — S, medium

Only 27 of 1,085 functions lack a return annotation and all 13 `type: ignore`
comments carry codes, so `disallow_untyped_defs` is nearly free.
`check_untyped_defs`/`--strict` need per-module overrides replacing the
blanket `ignore_missing_imports` for the stub-less providers first.

### Deliberately not recommended

- CHANGELOG, CONTRIBUTING, CODEOWNERS, CODE_OF_CONDUCT, release tags/semver:
  continuous deploy from `main`, one maintainer — no outcome changes.
- `SECURITY.md`: a five-line file is fine if you want the Scorecard point;
  nothing depends on it.
- Per-user anything (ADR 0001 stands), automatic game splits, a price-history
  table, more platforms — the 07-06 "what NOT to build" list still holds.
- Moving `stacks/` (12 MB vendored three.js visualiser) to its own repo: it is
  tested (`test_stacks_galaxy.py`), served by Caddy from this compose file,
  and licensed; note the three.js addon versions in a comment and move on.

## 4. Findings by dimension (compact)

### Security & supply chain
Verified strong: `data/db/readonly.py` (`mode=ro` + `PRAGMA query_only` +
authorizer allowlist + 5 s progress abort + length limits); scrape fetches
follow redirects manually with the host allowlist re-checked per hop
(`data/scrape_config.py:236-270`); `hmac.compare_digest` on the admin bearer
(`http_admin.py:112`) and constant-time nonce scan (`session_ingest.py:267-280`);
256-bit single-use ingest nonces with 15-min TTL and uniform 404s;
`MCP_AUTH_MODE` fail-closed, secrets ≥ 32 chars and `repr=False`
(`auth.py:150-154,180-184`); OAuth via FastMCP `GitHubProvider` with
`require_authorization_consent=True` and pinned client redirect URIs
(`auth.py:112-120`) — the MCP spec's confused-deputy mitigation; origin
allowlist as one ASGI choke point; widgets use `textContent`/`el()` only, zero
`innerHTML`; no secret literal in git history; `.env`, `/data/`, `*.db`
ignored. Open: #1, #2, #3 above; no rate limit on `/admin/*` or
`/ingest/{nonce}` (offset by entropy); regex overrides are length-capped but
not backtracking-checked (gated by fixture replay + live trial).

### Tests
2,322 test functions / 397 classes / 80 files; 75/80 unittest-style; 3
parametrize uses; 0 hypothesis; 33/33 `wait_for` use `DEADLOCK_TIMEOUT`; 0
numeric-literal timeouts; 11 `asyncio.sleep` (all yields, not liveness polls);
3 skips, 0 xfail; every provider test patches `httpx.AsyncClient` at the
module namespace but there is no autouse network guard. Modules under 60 %
line coverage: `scrape_validate` 54 %, `nintendo` 54 %, `backloggd` 54 %,
`epic` 59 %, `nintendo_pctl` 61 % — all provider modules whose remaining
lines are live-network paths. Largest test file 4,876 lines
(`test_purchase_importers.py`), longest test 182 lines.

### Architecture & code quality
0 import cycles; 3 places a promoted local import would create one, each
commented; 198 function-local imports (56 in `main.py` stylistic); 99
`except Exception` sites, sample of 10 all log/re-raise/record; 8 custom
exception types; `raise ToolError` 235 vs 22 `{"error":…}` returns (per-item
isolation + `query_library`'s self-correction shape, both by design); 58
response models / 507 fields / 68 % Optional / **0** `Field(description=…)`;
CLAUDE.md factual claims checked (32 tools, v39, 14 paths, 3 DAG edges) all
accurate.

### MCP surface
32 tools, 6 resources (2 `ui://` content-hashed widgets, 3 `skill://`, index),
1 resource template, 0 prompts (ADR 0006 rejection, deliberate), no
completion support, 923-char `instructions`. Widgets are resource-served
(62 KB / 67 KB HTML) not inlined per call. `ResponseSizeGuardTests` covers 7
tools. Findings #4, #5.

### Ops & reliability
v39 schema, 38 chained steps, `VACUUM INTO` pre-migration snapshot that
aborts the migration on failure; 9 writers under `retry_on_write_contention`,
2 `BEGIN IMMEDIATE` sites; 20 named indexes; 30 `httpx.AsyncClient` sites,
4 without timeout; Steam and IGDB request gates; `/health` checks schema
version, per-platform ownership vs. sync history, in-flight sync; nightly
backup + off-machine pull; no live `VACUUM` (fine at this scale);
`json_each(tags)` scans in `discover_games` unindexable but fine at single-user
scale. Findings #2 (deploy), #7, #13, #14.

### Docs & DX
7 ADRs all `Status: accepted (date)`; `rules/model-postures.md` and
`.claude/agents/*.md` byte-identical on the shared posture text; skills'
frontmatter valid and every tool they name exists; CI commands match
CLAUDE.md 1:1; 34 markdown files / 596 KB under `docs/` with no index
(plans/specs are historical and fine, a `docs/README.md` pointing at the ADRs
as source of truth would help); `pyproject` has no `license` field though MIT
`LICENSE` exists. Findings #8, #9.

## 5. Prior-audit status

**2026-07-01** — all six gaps and all seven roadmap items implemented and
re-verified in code (static analysis, PR CI, pre-migration snapshot + nightly
backup, LICENSE, auth hardening, tracked startup task; wishlist deals →
`get_wishlist(with_prices=True)`, completion status, series gaps, play
history, Xbox, single-user ADR). One nit still open: the four
implicit-timeout httpx clients (#13).

**2026-07-06** — roadmap items **1–4 not implemented** (recsys eval harness,
purchase advisor with `history_low`/`advice_score`, deal alerts,
`suggest_games_to_rate`); item 5: restore drill **open**, `db/__init__.py`
split **partial** (satellite modules exist; the façade grew 1,936 → 2,637
LOC), deals widget / sale-window urgency not started. The project instead
built the assessment-recording system, methodology provenance, the evaluation
card, and ownership lifecycle — all well-executed, none of which addresses
the "recommender flies blind" critique.

## 6. Key numbers

| Metric | Value |
|---|---|
| Source / test LOC | 50,942 / 46,943 |
| Tests (functions / subtests / wall) | 2,322 / 357 / 54 s on 4 cores |
| Coverage line / branch | 86.5 % / 81.0 % (15,767 stmts, 1,814 missed) |
| Locked packages / with advisories | 113 / 6 |
| `tools/list` bytes (desc / input / output) | 170,409 (77,178 / 19,940 / 63,843) |
| Tools / > 8 params / max params | 32 / 10 / 32 (`record_assessment`) |
| Root CLAUDE.md / nested / posture injection | 43.2 KB / 40.6 KB / ≤ 0.7 KB |
| Largest modules | admin 3,145 · assessment 2,651 · db/__init__ 2,637 · main 2,354 · checks 2,334 |
| Import cycles / dead functions / unused globals | 0 / 4 (+5 mock-only) / 3 |
| Widget duplication | 899 shared lines, ratio 0.51 |
| Actions tag-pinned / `permissions:` blocks | 5 / 0 |
| `chmod` calls in package | 0 |
| Open issues / open PRs | 5 (#155 tripwire, #151, 3× stacks) / 1 (Dependabot #161) |

## 7. Sources

- OpenSSF Scorecard checks — https://github.com/ossf/scorecard/blob/main/docs/checks.md
- MCP security best practices (2026-07-28) — https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices
- Anthropic, "Writing effective tools for agents" — https://www.anthropic.com/engineering/writing-tools-for-agents
- OWASP MCP Security Cheat Sheet — https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html
- Scientific Python Development Guide — https://learn.scientific-python.org/development/
- SQLite backup strategies in production — https://oldmoe.blog/2024/04/30/backup-strategies-for-sqlite-in-production/
- Advisories: PYSEC-2026-179 (pyjwt), PYSEC-2026-248 (starlette), GHSA-537c-gmf6-5ccf (cryptography) — https://osv.dev
- Method reference (evidence-first LLM audits): RepoAudit (ICML 2025); kevinpatrickrobbins/codebase-audit playbook
