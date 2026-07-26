# ADR 0004: consolidated MCP tool surface

Status: accepted (2026-07-26)

## Context
- The tool surface had grown to 51 registrations, and ~54% of `main.py`'s 1925
  lines were docstring — which *is* the wire schema every client loads on
  connect. Three kinds of redundancy had accumulated:
  1. **Nine `*_batch` twins.** Six were literal loops over their single-item
     impl (`update_games_batch` → `update_game(**kwargs, dry_run=…,
     recompute_affinity=False)`), adding only a deferred
     `recompute_tag_affinity` and per-item error isolation. A caller had to
     learn two names, two docstrings, and two response shapes for one
     operation.
  2. **Verb-per-tool families.** Five scrape-config tools over one entity and
     one `provider` key (`get`/`diagnose`/`propose`/`approve`/`rollback`);
     three sync tools with near-identical signatures and a verbatim-duplicated
     `_resolve` closure (`tools/admin.py`); five zero-to-few-argument read
     reports.
  3. **Split pairs that are always called together.** `get_db_schema` existed
     solely to be called before `query_library` (its own docstring said so);
     `get_wishlist_deals` priced exactly the rows `get_wishlist` listed.
- ADR 0003 had already established the pattern and proved it: 8 `detect_*`
  tools collapsed into one `check_library` registry (58 → 51) by keeping every
  impl function byte-for-byte and consolidating only the *wire* surface.
- Nothing about the batch split was load-bearing. Every functional test calls
  the implementation in `gamelib_mcp/tools/*` **directly**; zero tests invoked
  `main.py`'s registered wrappers. The redundancy was purely in what clients
  had to read.

## Decision

1. **One tool per operation; `items=[...]` is the bulk mode.** The nine
   `*_batch` tools are removed as MCP tools. Each single-item tool gains an
   `items` parameter (`queries` on `search_games`), and its `@mcp.tool` wrapper
   dispatches to whichever impl applies. Both impls stay in `tools/*` under
   their existing names and signatures — this is a registration-layer change
   only. Names stay singular and unchanged, which keeps ~46 cross-references in
   impl docstrings, `checks.py` `suggested_action` payloads, and `apps.py`'s
   hardcoded `"get_game_detail"` widget call valid.

   The convention, uniform across all nine: scalar params act on one game and
   raise `ToolError` on failure; `items` returns `{results, total, ok, errors}`
   and isolates failures per item; `items` wins if both are given.

2. **A mode-dependent default is legitimate when the two modes genuinely
   differ, and must be stated in the docstring.** Three parameters take
   `None` meaning "one thing for a single call, another for `items`":
   - `get_game_detail(enrich=None)` — True single, False bulk. A single call
     fetches and caches missing provider enrichment; 50 items would mean 50
     rounds of provider HTTP, so bulk serves cache only and says
     `enrichment: "skipped"`. `enrich=True` with `items` is a `ToolError`
     rather than a silent fan-out.
   - `set_acquisition(overwrite=None)` — True single, False bulk. Naming a
     field in a single call is a deliberate correction; an `items` import must
     never clobber a value set by hand.
   - `set_acquisition(create_platform_row=None)` — True single, False bulk.
     Recording one purchase implies ownership; a bulk import reports
     `no_platform_row` instead of silently minting rows.

   These preserve the exact behavior of the tools they replaced. Where the two
   modes could not be reconciled honestly, the merge was not done (see
   "Rejected" below).

3. **Verb families collapse to an `action`/`report` parameter, but never
   across the read/write boundary.** The five scrape-config tools become two,
   split on that boundary rather than one tool with five actions: merging
   `get_scrape_config` into a tool that can also roll back would advertise the
   read path as a non-idempotent mutation. A merged tool takes the STRICTEST
   annotation of everything it absorbs — `manage_scrape_config` is
   `NON_IDEMPOTENT_MUTATION_TOOL` because rollback walks back one version per
   call, and `get_wishlist`/`get_scrape_config` became open-world because their
   opt-in modes fetch live.

4. **Aggregate reports are one tool with a `report` selector.** `get_stats`
   replaces `get_backlog_stats`, `get_platform_breakdown`, `get_taste_profile`,
   `get_spending_stats`, and `get_series_breakdown`. The five impls stay
   exactly where they live (`tools/stats.py`, `platforms.py`, `ratings.py`,
   `acquisition.py`, `series.py`) — in particular the three deliberately
   divergent `_GAME_ROLLUP_CTE` variants are untouched, per their in-code
   "Kept separate on purpose; do not merge" comments. `get_library_stats` stays
   standalone: it is the paginated game *list*, not a rollup.

5. **The three syncs become `sync(targets=[...])`.** Default `None` →
   `["library"]`, preserving the old `refresh_library()` ergonomics. `library`
   keeps its fire-and-forget ack polled via `get_sync_status` (which stays
   standalone as the read side); `wishlist` and `ratings` stay blocking and
   return inline. Results are keyed by target.

6. **A report-only heuristic whose remedy is another tool call is a check, not
   a tool.** `suggest_completion_status` is removed and re-registered as
   `completion.unclassified` in the `CHECKS` registry — offline, permanently
   report-only, `suggested_action` = `update_game(game_id, completion_status)`.
   It always fit ADR 0003's finding contract; it just predated the registry.
   The heuristic itself stays unchanged in `tools/completion.py` with its own
   unit tests, adapted the same way the migrated detectors are.

7. **Merged tools declare one all-optional response model.** A union return
   annotation (`A | B`) renders as `{"properties": {"result": {"anyOf": …}}}` —
   it wraps the payload and loses the flat top-level schema, which also breaks
   the paginated-output assertions. So each merged tool has a single
   `FlexibleModel` whose fields span both modes, all optional, with comments
   marking which mode fills what. Two keys carry `bool | int` because the
   single and bulk shapes genuinely disagree (`add_game_to_platform.created`,
   `delete_game.deleted`: a flag in single mode, a count in bulk).

8. **The bulk convention is advertised in `mcp.instructions`**, drift-guarded
   by `tests/test_tool_registration.py`. A merged tool is only a win if clients
   reach for `items=[...]` instead of looping; the tool list alone does not
   teach that.

## Rejected (and why)
- **`import_purchases` into `set_acquisition`** — 92 docstring lines, five own
  parameters, and a distinct failure model (per-source isolation,
  `bundles_needing_split`, `create_missing` defaulting True).
- **`split_bundle_acquisition` into `set_acquisition`** — price splitting
  across constituents is its own operation, not a bulk write.
- **`split_game`/`merge_games`/`delete_game` behind one `action`** — that puts
  irreversible deletion behind the same schema as a merge.
- **`create_session_ingest_link` + `set_nintendo_pctl_session`** — the pctl
  flow is a two-step login round-trip, not a cookie paste; one parameter would
  mean two unrelated things.
- **`get_sync_status` into `get_integration_status`** — last-sync state from
  `meta` vs cached credential readiness.
- **`set_switch2_playtime_baseline` into `set_playtime`** — it deliberately is
  *not* a playtime pin (it writes `nintendo_play_summary` so future syncs keep
  accumulating). Merging would invite the exact confusion its docstring exists
  to prevent.

## Consequences

### Positive
- Tool surface shrinks 51 → 30 with no capability removed and no behavior
  change. Every operation that existed still exists.
- **The win is choice, not bytes.** Measured wire payload barely moved —
  137,847 → 137,030 chars (descriptions 65,514 → 62,370; input schemas
  18,243 → 17,218; output schemas 54,090 → 57,442). Merged docstrings have to
  document both modes, and an all-optional merged response model declares the
  union of both modes' fields, so output schemas actually grew. Anyone
  proposing a future merge should expect the same: consolidation buys a
  smaller decision space for the caller (one obvious tool per operation
  instead of picking between near-identical twins), not a cheaper connect.
  If context size is the goal, trimming the five docstrings that hold ~37% of
  all description text (`check_library`, `import_purchases`,
  `split_bundle_acquisition`, `set_acquisition`, `update_game`) is the lever.
- One calling convention to learn (`items` for bulk, `report`/`action` for verb
  families, `dry_run`/`confirm` for previews) instead of per-tool conventions
  discoverable only by reading each docstring.
- The single-item ergonomics survive: `rate_game(name="Hades", score=9)` is
  still a typed, wire-validated call. Dropping the singles in favor of
  plural-only tools would have pushed every one-game call into an untyped dict
  inside a list, since the wire layer only validates top-level params
  (`tools/batch.py` documents this).
- Impl churn was near zero: no function in `tools/*` changed name or signature,
  so all 1700+ functional tests kept passing untouched. Only
  `test_tool_registration.py` and three string assertions needed updating.

### Negative / revisit triggers
- **Merged output schemas are looser.** A tool with two modes cannot declare
  either mode's fields as required, so the schema anchors what keys *may*
  appear, not what will. Mitigated by per-field comments naming the mode. If a
  client ever needs strictness, the fix is a discriminated union, not a split.
- **Mode-dependent defaults are a real footgun** and only survive because they
  preserve existing behavior exactly. A fourth one should be treated as a
  signal that the merge is wrong, not as precedent.
- **`get_stats` unions ten parameters** for five reports, most applying to only
  one. A sixth report with its own parameters would make the case for splitting
  the paginated `series` report back out.
- **`bool | int` on `created`/`deleted`** is honest but ugly. The alternative
  was renaming the bulk counters, which would have changed impl return shapes
  and broken their unit tests. Worth revisiting if those keys ever confuse a
  caller in practice.

## Amendment (2026-07-26): measured, not assumed

The original decision was argued from tool count and reasoned about payload
size without measuring it. Everything below is measured — schema against the
live server, responses against a copy of the 2,726-game dev database.

### 1. The `$defs` hypothesis was wrong

Consequence-section reasoning suggested FastMCP inlining `$defs` was wasting a
lot of output-schema space. It is not. `FastMCP(dereference_schemas=False)`:

| | inlined (default) | `$defs` kept |
|---|---|---|
| `search_games` output schema | 6,604 | 3,764 (−43%) |
| **all 30 tools** | **57,442** | **55,756 (−1.2%)** |

Only a model referenced more than once wins (`GameSummary` appears twice in
`search_games`). For single-use models the `$ref` indirection costs about what
it saves. Not worth the client-compatibility risk of shipping `$ref`s, even
though 2026-07-28 permits them. Left at the default.

### 2. The real waste was an unbounded response, not the schema

`get_platform_breakdown`'s `overlap_games` had no `LIMIT`. On a ~3k-owned-row
library it returned 428 entries — **33,972 of the report's 34,414 chars (98%)**,
growing linearly with the library forever. `get_wishlist` had the same shape
(no `LIMIT` at all), hidden only because the dev wishlist is empty.

Both are now capped (`overlap_limit`, default 25 / max 200; `limit`+`offset`,
default 100 / max 500) with the true total still reported (`overlap_count`,
`total_matches`) and an explicit truncation flag. `get_stats(report="platforms")`
went 65,713 → 4,995 chars on the wire (−92%).

**Rule this establishes:** every response field whose length scales with
library size needs a cap, a true total, and a truncation flag. An audit script
that walks responses and flags any list over ~1,500 chars is the cheap way to
catch the next one — the two found here were the only offenders across the 13
read paths, but neither was obvious from reading the code.

### 3. Descriptions: progressive disclosure beat prose

`check_library`'s description was 11,290 chars — 8.2% of the entire tool
surface — enumerating all 21 check ids in paragraphs. `list_checks=True`
already returned exactly that as structured data. The description now keeps the
bare id list (they are the selection vocabulary, and the drift-guard test still
enforces every id appears) plus the three facts a caller needs *before* calling
that are unsafe to discover late: which checks write, which need network, which
take options. Prose moved to the catalog. −7,595 chars.

Total tool surface: 137,030 → 130,753 chars (−4.6%).

### 4. Responses cost more than schema, and half of every response is duplicated

Schema is paid once per connect; responses are paid per call. Across 21
representative calls the response total was **325,668 chars after the caps**
(386,312 before) — 2.5× the entire schema, in one short session.

**156,683 of those 325,668 chars (48%) are the same payload sent twice.**
FastMCP populates both `content[0].text` (serialized JSON) and
`structuredContent`, which the spec endorses: "For backwards compatibility, a
tool that returns structured content SHOULD also return the serialized JSON in
a TextContent block." `ToolResult(content=[], structured_content=…)` suppresses
it and is a one-line change per tool.

**Deliberately not done.** It is a SHOULD, not a MAY, and which of the two a
given host feeds to the model is not something this repo can verify — our own
game-cards widget reads `structuredContent` *with a fallback to content text*
(`apps.py`), which is evidence that hosts differ. Halving response bytes is the
single largest remaining win available, and it is gated on testing against the
actual client, not on code. Revisit if a host is confirmed to prefer
`structuredContent`.

### 5. Spec conformance for 2026-07-28

Audited against the revision going final 2026-07-28: no `-32002` literals to
migrate; all tool names within the charset/length rules; every tool carries
annotations and a non-empty output schema; the one no-parameter tool already
emits `additionalProperties: false` as recommended. All 30 tools previously
omitted the spec's optional `title`, which clients use for display — now set,
at no measured cost to the description or schema fields.

Not adopted, deliberately: `execution.taskSupport` (the Tasks extension) would
suit `sync(targets=["ratings"])` and `import_purchases`, both of which run for
minutes, but it changes client interaction semantics and cannot be validated
here. `oneOf`/conditional input schemas could now express the
scalar-vs-`items` exclusivity that decision 1 enforces at runtime — worth
doing once SDK and client support is real, not on the strength of the spec
alone.

### 6. Known deviations from published guidance

AWS prescriptive guidance recommends ≤8 parameters per tool. Eight tools exceed
it: `update_game` (23), `add_game_to_platform` (15), `set_acquisition` (14),
`get_library_stats` (14), `get_stats`/`split_bundle_acquisition` (10),
`discover_games`/`set_playtime` (9). These are "set any subset of these fields"
shapes; collapsing them into a `fields: dict` would trade away the top-level
wire validation that is the entire reason decision 1 kept scalar params. Left
as a conscious deviation.

## Amendment (2026-07-27): the dual-encoding question, answered empirically

Amendment §4 left the largest available win — 48% of all response bytes being
the same payload sent twice — undone, because whether a host feeds the model
`content[0].text` or `structuredContent` could not be verified from the repo.
It has now been tested against the real deployment.

**Method.** One read-only tool (`get_sync_status`) was changed on the
production server to `ToolResult(content=[], structured_content=await _status())`,
rebuilt, and called through the live claude.ai connector. The edit was verified
present *inside the running container* before the call, and the server was
reverted to a clean tree and rebuilt immediately after.

**Result.** The call returned the complete payload with **zero content
blocks** on the wire. A local wire-level check (`_call_tool_mcp`) confirms
FastMCP does not re-add the text block: `normal` → 1 block / 93 text chars +
102 structured; `suppressed` → 0 blocks / 0 text chars + 102 structured.

**Conclusion: claude.ai's MCP client reads `structuredContent`.** For that
client the duplicate text block is pure overhead, and suppressing it would cut
response bytes roughly in half.

**Still not adopted, for a new reason.** The server's OAuth proxy has two
registered clients: `claude_ai` **and `chatgpt_com`**. The experiment proves
nothing about the second one, and the spec's "SHOULD return the serialized
JSON in a TextContent block" exists precisely for clients that need it.
Suppressing the block server-wide would be a coin-flip on the ChatGPT
connector. The options, in order of preference:
1. Repeat this same experiment against the ChatGPT connector; if it also reads
   `structuredContent`, suppress unconditionally.
2. Make it opt-in per deployment (env flag, default off) if only one client is
   confirmed.
3. Leave as is.

**Correction to operational lore:** the deployment notes claimed container
restarts expire claude.ai connector OAuth sessions. They do not, and have not
since `FASTMCP_HOME` moved onto a host bind mount. Stored tokens dated
2026-07-20 survived restarts on 2026-07-26 and two more during this
experiment, with tool calls working throughout and no re-auth. Schema caching
was not exercised (the experiment deliberately changed only the response
encoding, not any tool signature).

## Amendment (2026-07-27, second): duplicate text block removed

The previous amendment proved claude.ai reads `structuredContent` but withheld
the change because the OAuth proxy also has a `chatgpt_com` client that the
experiment said nothing about. That gap is now closed.

**Method.** A *differential* probe — strictly better than the first
suppress-and-see test, because it identifies the channel positively instead of
inferring it from absence-of-breakage. `get_sync_status` returned the real
payload in BOTH channels, differing only by a `_channel` marker
(`CONTENT_BLOCK` vs `STRUCTURED_CONTENT`), so whichever a client surfaced named
what it consumed, and neither client could see a broken-looking result.

**Result.** claude.ai and chatgpt.com both reported `STRUCTURED_CONTENT`.
Neither reads the duplicate text block.

**Decision: strip it, via middleware, on by default.**
`gamelib_mcp/response_encoding.py::StructuredOnlyMiddleware` drops text blocks
from any tool result that already carries structured content. Middleware rather
than 30 edited wrappers: one implementation, one place to reason about, and it
covers tools added later without anyone remembering to.

Measured across the same 21 representative calls: **325,668 → 168,985 chars
(−48.1%)**, zero duplication remaining. Combined with the response caps in the
previous amendment, the total is down from 386,312 — a 56% reduction in what
this server sends back.

**Why it stays reversible.** This is a per-deployment optimization, not a
general one — the spec's SHOULD exists for clients that need the text block.
`MCP_DUPLICATE_TEXT_CONTENT=1` restores spec-default behavior with no code
change, for a third client that ever needs it. Re-run the differential probe
before assuming a new client is safe.

**Three edge cases pinned by `tests/test_response_encoding.py`,** because the
failure mode (a client seeing empty results) is severe:
- A result with no structured content is left alone. FastMCP populates
  structured content even for a scalar return (wrapping it `{"result": ...}`),
  so this guard fires rarely — but it is what keeps a deliberately
  raw-content tool working.
- Non-text blocks (image/audio/resource links/embedded resources) always
  survive; they are not duplicates of the structured payload.
- Errors are safe *by construction*: a failing tool raises through
  `call_next` rather than returning a `ToolResult`, so an error message can
  never be stripped from a model that needs it to self-correct. The test
  asserts this rather than assuming it.

## Amendment (2026-07-27, third): re-audited against a real prod snapshot

The response audit that produced the caps above ran against the dev database,
which has zero ratings, zero wishlist rows, and **zero priced rows**. Re-running
it against a nightly prod backup (3,819 games / 4,211 owned rows / 190 wishlist
/ 2,859 priced rows) found one more unbounded field the dev data could not have
surfaced:

**`get_spending_stats`'s `by_bundle` had no `LIMIT`** — 139 entries and 47% of
that response, gaining a row for every distinct bundle ever bought. Its
siblings (`by_year`, `by_source`, `by_platform`) are bounded by a fixed
vocabulary; `bundle_name` is not. Now capped at `BUNDLE_BREAKDOWN_CAP` (25,
biggest spend first) with `by_bundle_count` / `by_bundle_truncated`, the same
shape as `overlap_games`. The spending report went 29,187 → 17,879 chars (−39%).

Everything else held: the caps behave correctly at scale
(`overlap_games` 25 of a true 474, `by_bundle` 25 of 139, wishlist 25 of 190),
caller-supplied limits are clamped to 200 by `clamp_limit` (which also guards
SQLite's negative-limit-means-unbounded behaviour), the full offline
`check_library` sweep runs 18 checks over 3,819 games with zero errors, and
every response came back with zero content blocks.

**A test was wrong, not the code.** Paging the real 190-row wishlist returned
185 distinct `game_id`s, which looked like a pagination defect. It is not:
`game_wishlist` is `UNIQUE(game_id, platform)` and five games are wishlisted on
both steam and switch2. `test_wishlist_pages_do_not_overlap_or_skip` asserted
uniqueness on `game_id` alone and would have failed spuriously the moment a
test seeded that shape; it now keys on `(game_id, platform)`, and a companion
test documents the two-rows-one-game invariant.

**Lesson for the next audit:** a response-size audit is only as good as the
data behind it. Three of the four unbounded fields found across this work were
invisible on the dev database — run the audit against a prod snapshot, not the
checked-in fixture.
