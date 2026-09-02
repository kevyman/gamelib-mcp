# MCP surface: modules, widgets, tool consolidation

Why this exists: `main.py`'s tool signatures ARE the wire schema, and the
middleware/widget modules around them each encode a decision that is expensive
to rediscover (why a `ui://` URI is content-hashed, why the duplicate text
block is dropped, which CSP hosts a card needs). The root `CLAUDE.md` keeps a
one-line map plus the rules; the rationale and the measurements moved here on
2026-09-01. Decisions themselves live in ADR 0004 (tool surface) and ADR 0005
(spec currency) — this file holds the implementation notes around them.

## HTTP and response encoding

`http_admin.py` — origin-allowlist middleware + `/health`, `/admin/integrations*`, and `/ingest/{nonce}` (the cookie paste form; deliberately outside `/admin/` so a browser navigation needs no bearer header). `/mcp` is authenticated by FastMCP's OAuth provider, not this middleware.

`response_encoding.py` — `StructuredOnlyMiddleware` drops the duplicate text block FastMCP ships alongside `structuredContent` (the spec's backwards-compat SHOULD). Measured at 48% of all response bytes; both clients registered against prod (claude.ai AND chatgpt.com) were probed on 2026-07-27 and read `structuredContent`. `MCP_DUPLICATE_TEXT_CONTENT=1` restores spec-default behavior for a client that needs the text block — re-run ADR 0004's differential probe before assuming a new client is safe.

`session_ingest.py` — single-use cookie-paste links: in-memory nonce store (TTL 15 min, pop-on-success) + `/ingest/{nonce}` GET form / POST handler that dispatches to `tools.session_admin.set_*_session`. In-memory by design (single-user, single-process); a server restart invalidates outstanding links.

`MCP_ALLOWED_ORIGINS` — browser origins allowed on the HTTP surface; requests without an `Origin` header (native/CLI clients) still pass. The `create_session_ingest_link` paste form POSTs a same-origin `Origin`, so oauth mode auto-allowlists `MCP_PUBLIC_BASE_URL`'s origin, but local `disabled` mode must include `http://localhost:8000` here.

## The two MCP Apps widgets

`apps.py` — MCP Apps game-cards widget for `discover_games`/`get_game_detail`. The `ui://` URI is content-hashed because hosts cache ui:// resources by URI (a stable URI left claude.ai stale across deploys). The HTML is dependency-free (hand-rolled postMessage bridge, no CDN); CSP allowlists only the two cover-art hosts. Rank badges render only when the payload carries `offset`. Since `get_game_detail(media=True)`, the detail card also renders the neutral media representation — trailer hero, screenshot strip with a stacking lightbox, similar-games row — and the grid overlay's live upgrade call passes `media: true`, so discover click-throughs get trailers; its CSP carries the same media hosts + youtube-nocookie frame domain as apps_eval.py. The blocks the two widgets genuinely agree on — palette and reset CSS, the postMessage bridge, the trailer stage, the carousel, the pedigree strip — live in `apps_shared.py` and are spliced into both, so one edit reaches both instead of being hand-ported (899 lines were duplicated before the split). What each widget serves stays self-contained ON THE WIRE: every `ui://` resource is still one standalone HTML string, no build step, no CDN, no cross-resource fetch — `SharedBlockTests` in both test files asserts every shared constant appears verbatim in the served HTML, and `tests/test_apps_eval.py::WidgetDriftTests` fails if the two modules again share an identical run of ≥ 20 non-blank lines outside `apps_shared.py` (longest today: 15). Preview with `scripts/preview_game_cards.py` (no MCP host needed; `--sample-media` for the media sections offline).

`apps_eval.py` — the second MCP Apps widget: the evaluation card rendered from `record_assessment` results (`package` block; a package-less response renders a compact "Recorded/voided" note). Same content-hashed-URI/bridge/no-CDN discipline as `apps.py`; its CSP additionally lists the Steam `shared.*`/`cdn.akamai` image hosts, `i.ytimg.com` posters, and `frame_domains=["https://www.youtube-nocookie.com"]` (per the MCP Apps spec, resourceDomains feeds media-src too — the inline `<video>` trailer relies on that). Preview with `scripts/preview_eval_card.py`.

## Skills over MCP

`skill_resources.py` — serves the `skills/` gaming skills (see below) as `skill://<skill-name>/<path>` MCP resources plus a `skill://index.json` discovery index, in the SEP-2640 URI shape (ADR 0006), AND backs the `get_skill` tool (decision 4b) — the same bytes for hosts whose model can't call `resources/read` (claude.ai custom connectors surface only tools to the model). Both surfaces share one per-request disk scan, so an edited skill file needs no restart and the two can't drift; a missing/empty `skills/` dir logs a warning and registers no resources rather than failing startup (the tool then serves an empty index with a note). `scripts/package_skills.py` builds installable trigger stubs (canonical frontmatter + a fetch-via-`get_skill` body) and claude.ai Skills-upload zips from the canonical folders.

`skills/` (repo root, sibling of `gamelib_mcp/`) is the canonical home of the client-side gaming skills (`game-quality`, `backlog-triage`) per ADR 0006 — they version with this repo and are served read-only via `skill_resources.py`; `~/.claude/skills` and claude.ai Skills installs are downstream copies, never the source of truth.

## Platform registry

`platforms_registry.py`: the single registry of platforms. All platform lists/aliases derive from it. Sync/inspector functions are `(module, attr)` strings resolved lazily (no import cycles), preferring names bound on a caller-supplied namespace — which keeps the `patch("gamelib_mcp.tools.admin.sync_epic", ...)` test pattern working. Adding a platform = `data/<platform>.py` + one `PlatformSpec` (+ an inspector probe if it should appear in integration status).

## Bounded responses

**Bounded responses**: every response field whose length scales with library size must carry a cap, the true total, and a truncation flag (`get_stats(report="platforms")`'s `overlap_games`/`overlap_count`/`overlap_truncated`; `get_wishlist`'s `limit`/`total_matches`/`has_more`). Schema is paid once per connect; responses are paid per call and were measured at 2.5x the whole tool surface across one short session. `tests/test_tool_dispatch.py::ResponseSizeGuardTests` walks real responses and fails on any list over its documented cap — add new read paths there.

## One tool per operation, not per arity (ADR 0004)

**One tool per operation, not per arity** (ADR 0004): `main.py` registers 33 MCP tools over ~50 implementation functions in `tools/`. A `@mcp.tool` wrapper is a thin mode-dispatcher; impls keep their own names, signatures, and unit tests, which is why consolidating the wire surface touched almost no test. Conventions a new tool must follow: bulk is `items=[...]` on the single-item tool (never a second `*_batch` tool), verb families take an `action`/`report` selector, a merged tool inherits the STRICTEST annotation it absorbs, merged response models declare every field optional with comments naming which mode fills what, and a multi-mode tool validates the inputs of EVERY selected mode before running the first one (`admin.validate_sync_platforms` — `sync` starts the fire-and-forget library target first, so a later target's rejection would otherwise error on a sync already in flight). Read docs/adr/0004-consolidated-tool-surface.md — especially its "Rejected" list — before merging or adding a tool.

## MCP spec currency (ADR 0005)

**MCP spec currency** (ADR 0005): track the latest STABLE FastMCP/mcp SDKs (protocol 2025-11-25 today); adopt a new spec revision only once a stable SDK ships it AND a registered client speaks it — never pre-release SDKs in prod. The app-layer rule that keeps that bump cheap: no tool may depend on per-connection state, `tools/list` never varies per-connection, and cross-call state is explicit handles (`sync`'s ack + `get_sync_status`). The 2026-07-28 migration checklist (SDK bump, drop ctx-logging, tasks extension, apps bridge version, ttlMs values, re-probe clients) lives in docs/adr/0005-mcp-2026-07-28-readiness.md — read it before bumping fastmcp past 3.x or adding protocol-level features.
