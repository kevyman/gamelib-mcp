# Spec: "The Binder" — one card design language for both MCP Apps widgets (2026-10-04)

Status: implemented 2026-10-04 (phases 1A–3); the mock-ups remain the visual reference.
Supersedes the *visual* rules of `2026-10-03-widget-ux-redesign.md` §1.1
(token table stays), §1.2 (type scale — amended below) and §1.5 (restyle
pass). Everything structural from that spec (tiering, hand-back paths,
bounded responses, skeletons, notices, ≤2 actions, bridge protocol) still
holds.

Reference material (read before building; the mock-ups are the truth when
this document is ambiguous):

- `docs/specs/assets/binder/DESIGN-LANGUAGE.md` — the language (idea, tier
  rules, tokens, 19 components, surface recipes).
- `docs/specs/assets/binder/gl.css` — the reference stylesheet the mock-ups
  inline. Port it; do not reinvent it.
- `docs/specs/assets/binder/MOTION.md` — the seven motion moments, with the
  final durations table (300ms card enter, everything at rest by 500ms).
- Mock-ups: artboards A–K on the canvas (the main session has the PNGs
  under its scratchpad `canvas/shots/`; executors receive paths in briefs).

Decision rule when a trade-off appears: **the mock-up wins over the old
spec; the old spec's invariants win over convenience; UX wins over visual
polish.** No tool's wire contract changes in any phase.

## 0. Invariants (unchanged from 2026-10-03 §0, restated)

- Both widgets stay self-contained: one HTML string each, no build step, no
  CDN, no `data:` URIs (the CSP forbids them — the grain is an inline `<svg>`
  element, never a background image), runtime fetches only from the
  allow-listed media hosts and the host's own font `@font-face`.
- Shared source lives in `apps_shared.py` and is spliced verbatim into both
  widgets (`SharedBlockTests`, `SplicedVerbatimTests`, `WidgetDriftTests` ≥20
  identical lines fails). Every NEW public constant must be spliced into both.
- All payload text goes through `textContent`/`el(...)`; never `innerHTML`.
- `ui://` URIs stay content-hashed; `prefers_border=False`; CSP lists
  unchanged; `reportSize`, `shouldReportSize`, model-context/fullscreen
  hand-backs, `label()`, number helpers unchanged in behaviour.
- Tests that pin the OLD look are **rewritten to pin the NEW rule** (same
  test name or a clearer one), never deleted without a replacement.
- Gates: `.venv/bin/ruff check gamelib_mcp tests scripts`,
  `.venv/bin/mypy gamelib_mcp`, `.venv/bin/python -m pytest tests/test_apps.py
  tests/test_apps_eval.py tests/test_apps_shared.py tests/test_tool_registration.py
  tests/test_docs_drift.py -q`. No new Python dependency.

## 1. The language, as it lands in code

### 1.1 Tokens (`TOKENS_CSS`)

Keep every existing `--gl-*` name. Add, each still `var(<host token>,
light-dark(L, D))` or a plain value when theme-invariant:

| New token | Value | Purpose |
|---|---|---|
| `--gl-keyline` | `light-dark(rgba(20,20,19,.12), rgba(250,249,245,.14))` | 1px inner line on art |
| `--gl-deep` | `#141413` | badge fill, ribbon ink, deep plate (theme-invariant) |
| `--gl-deep-ink` | `#FAF9F5` | text on deep |
| `--gl-ribbon-ink` | `#141413` | text on any ribbon |
| `--gl-ribbon-good/ok/bad/none` | `#7AB948 / #D1A041 / #EE8884 / #C2C0B6` | ribbon fills (theme-invariant) |
| `--gl-rarity-good/ok/bad` | the three conic gradients from gl.css (light variants under light) | card borders |
| `--gl-grain-opacity` | `light-dark(.035, .06)` | grain layer |
| `--gl-mono` | `var(--font-mono, ui-monospace, Menlo, Consolas, monospace)` | numerals |
| `--gl-serif` | `ui-serif, Georgia, "Times New Roman", serif` | flavor text only |
| `--gl-title` | `max(12px, var(--font-heading-lg-size, 20px))` | card title |
| `--gl-heavy` | `800` | titles, badge numbers, ribbon text |
| `--gl-tier`, `--gl-tier-text`, `--gl-tier-fill`, `--gl-rarity` | set by `.tier-good/.tier-ok/.tier-bad/.tier-none` | read by every component |

Amendment to the 2026-10-03 type rule: **four sizes** (`cap` 12, `body` 14,
`h` 16, `title` 20) and **three weights** (`regular` 400, `strong` 600,
`heavy` 800); mono may use 500/700 for stat values and badge numbers via the
same tokens (`--gl-strong`/`--gl-heavy`). `DesignSystemTests` are updated to
these counts. Raw colours are allowed only in `TOKENS_CSS`, `.cover-fallback`
rules and `COVER_NODE_JS` (the stamp exception goes away with the stamp).
Rotations allowed: `-1deg` (deal-in keyframe), `45deg` (pips), `180deg`
(chevrons). The `-3deg` stamp is gone.

### 1.2 Components → repo class names

The mock-ups use `gl-` prefixed classes; the repo stays unprefixed, matching
its existing `.card/.chip/.btn`. Mapping (new CSS in `apps_shared.py`,
constants named in brackets; each a public constant spliced into BOTH
widgets):

| Mock-up | Repo class | Constant | Notes |
|---|---|---|---|
| `.gl-card` (+`--s`) | `.frame` (+`.frame-s`) | `FRAME_CSS` | padding-box/border-box rarity trick; 6px / 4px; radius 14 / 12; `.tier-*` modifier; a tappable frame is a `<button class="frame …">` |
| `.gl-grain` | `.grain` | `GRAIN_JS` (`grainNode()`) | inline `<svg>` built with `document.createElementNS`, first child of every `.frame` |
| `.gl-art` (+`--plate`) | `.art` (+ existing `.cover-fallback` plate) | `ART_CSS` | 2:3 or 16:9 via `.art-hero`; keyline inset shadow; `::after` specular line |
| `.gl-badge` | `.badge` | `BADGE_CSS` + `BADGE_JS` (`badgeNode({value, suffix, tag, tier})`) | 52px disc, `--gl-deep`-on-inverse per theme as gl.css, 2px tier ring, tag beneath |
| `.gl-plate`, `.gl-no`, `.gl-sub` | `.plate`, `.card-no`, `.sub` | `PLATE_CSS` | sub spans gap-separated, never middots |
| `.gl-loz` | `.loz` | in `PLATE_CSS` | platform lozenge via `label("platform", …)` |
| `.gl-stats/.gl-stat` | `.stats/.stat` | `STATS_CSS` + `STATS_JS` (`statRow({label, note, value, key})`) | dotted leaders; `.is-key` |
| `.gl-pips` | `.pips` | `PIPS_CSS` + `PIPS_JS` (`pipsNode(lit, of, tier)`, half pip) | diamonds |
| `.gl-ribbon` (+`--s`, `--straddle`, `--art`) | `.ribbon` (+`.ribbon-s`, `.ribbon-straddle`, `.ribbon-art`) | `RIBBON_CSS` + `RIBBON_JS` (`ribbonNode(text, tier, note)`) | replaces `.stamp` entirely |
| `.gl-trait` | `.trait` (+`.trait-plus/.trait-minus`) | `TRAIT_CSS` + `TRAIT_JS` | spark / shield SVG icons |
| `.gl-ability` | `.ability` | `ABILITY_CSS` | run-in label |
| `.gl-flavor` (+`--quote`) | `.flavor` (+`.flavor-quote`) | `FLAVOR_CSS` | serif italic |
| `.gl-chip` | existing `.chip` restyled | `CHIP_CSS` (edit) | 1px tier border, radius 4, mono value; keep `.lbl`, `b`, `.meter`, `.aux`, `.tier-*` structure (Node probes read them) |
| `.gl-mini`, `.gl-strip` | `.mini`, `.ministrip` | `MINI_CSS` + `MINI_JS` (`miniCard({...})`) | 48×64 art, tier border, name + two cap spans; strips scroll-snap (reuse `STRIP_CSS` padding/snap rules) |
| `.gl-reel`, `.gl-play`, `.gl-thumbs` | existing `.hero`/`.play-badge`/`.thumb` restyled | `HERO_CSS`, `MEDIA_STRIP_CSS` (edit) | play chip 44px bottom-right; thumbs keyline, active 2px inverse |
| `.gl-btn` (+`--primary/--secondary`) | existing `.btn` / `.btn.primary` restyled | `CONTROLS_CSS` (edit) | pills, 44/40px, optional leading SVG glyph |
| `.gl-set` | existing `.grid-head` restyled | apps.py local | facets as `.loz` |
| `.gl-skel` | existing `.skel*` restyled | `SKELETON_CSS` (edit) | frame outline, inset blocks, pulse under motion |
| `.gl-notice` | existing `.notice` | `CONTROLS_CSS` | add the 16px outlined-"!" SVG |
| `.gl-eyebrow` | existing `.section-title` | `PANEL_CSS` (edit) | label style, tertiary |

Motion lands as `MOTION_CSS` (keyframes `deal`, `stamp`, `pop`, `pip`,
`leader`, `settle`, `fill`, `skel-pulse`; the `.deal` class and `--i`
stagger; `:active` press; `@media (hover:hover) and (pointer:fine)` tilt +
sheen; everything inside `@media (prefers-reduced-motion: no-preference)`
with the opacity-only fallbacks outside it) and `MOTION_JS`
(`dealIn(node, index)` sets `--i` (capped 5) and adds `.deal`;
`resolveSkeleton(skel, build)` adds `.leaving` for 120ms then swaps and
deals the result in from +60ms). Durations exactly as MOTION.md's table.

Straddle safety: a straddling ribbon overhangs 20px each side, so the
300px card is `width: min(300px, 100% - 40px)` centered; nothing may be
clipped at 360px with the 12px gutters.

### 1.3 Tier rules (who sets `.tier-*` on the frame)

| Surface | Frame tier | Badge |
|---|---|---|
| Eval card | verdict: `buy_now`/`play_what_you_own` good, `wishlist_for_sale`/`try_demo` ok, `skip` bad | lead critic (`leadCritic`: OpenCritic, else Metacritic, tier by score), else none |
| Grid, sort match | `match_percent` ≥70 good, 50–69 ok, else none | `{match}%` + MATCH |
| Grid, sort critic/value | lead critic tier (`leadCritic`) | lead critic + OPENCRITIC/METACRITIC |
| Detail | `my_rating` ≥7 good, 5–6.9 ok, <5 bad; unrated → lead critic tier; else none | rating `8` + `/10` + YOUR RATING, else lead critic |
| Note card | verdict tier; void → none | none |
| Skeleton | none | none |

Slim ribbons on grid cards: `TOP MATCH` on the first card of a match-sorted
page when its match ≥70; `CRITICS' PICK` when Metacritic ≥85 (good tier);
otherwise none. Minis take the tier of the game's rating (none when
unrated).

### 1.4 THE STORY (eval card, 2026-10-04 addendum)

`presentation.story` — 1–4 model-authored sentences of creator lore, each
citing ≥1 of ≤6 `{url, kind}` sources (record_assessment validates structure,
never truth). `storyNode` in `apps_eval.py` (local: the detail card has no
story):

- **Placement**: on the ground after the abilities and before the craft-note
  flavor; fullscreen `order: 6` (abilities 5, `.flavor` 7) with
  `max-width: 560px`, the `.ev-notes` measure.
- **Eyebrow** "The story" — the `.section-title` label, like "From the studio".
- **Paragraph** `p.story-text`: ONE upright `--gl-serif` paragraph (the
  flavor face without its italic), `--gl-text`, body size, line-height 1.55.
- **Refs** `span.story-ref` "[1]" / "[1,2]" after each sentence, mono,
  `--gl-muted`, 12px (`--gl-cap`) — never a `<sup>`, never smaller — joined to
  the sentence by a narrow no-break space so a ref never wraps away from it;
  a ref to a missing source is dropped, not thrown on.
- **Chips** `div.chips.story-sources` of `a.chip.story-src`, one per CITED
  source in index order, three spans: mono number, domain (hostname without
  `www.`), kind label (Press / Studio / Store page / Wiki / Social) — no
  middot. Each opens through `openLink` like the store pill.
- **Store line** (both widgets, shared `PEDIGREE_JS`): FROM THE STUDIO's
  header gains a 12px muted `.ped-claim` after `.ped-with` — `The store says:
  "<text>"` from `pedigree.store_claim` (the Steam blurb's own "from the
  creators of…" sentence). Attributed marketing, never a people claim; a
  claim alone never opens the section (`hasStudio` ignores it).

## 2. Phases and ownership (disjoint files per parallel executor)

### Phase 1A — foundation (`apps_shared.py`, `tests/test_apps_shared.py`, the `DesignSystemTests`/`SharedComponentTests`/`HitAreaTests`/`SharedCssHygiene` classes in `tests/test_apps.py` and `tests/test_apps_eval.py`, plus the one-line splices in both widgets)

1. Port gl.css into the token layer and the new constants of §1.2 (keep
   `TOKENS_CSS`'s generated `@supports not (light-dark)` fallback working for
   every new colour token; `PlainColorFallback` counts them).
2. Restyle the existing shared components in place (chip, buttons, hero,
   thumbs, skeleton, notice, section-title) to the Binder look WITHOUT
   changing their DOM structure or the class names the Node probes read.
3. Add `MOTION_CSS`/`MOTION_JS`, `GRAIN_JS`, and the JS builders. Builders
   use only `el()`/`createElementNS`; every string via `textContent`.
4. Splice every new public constant into BOTH widgets (CSS after
   `COMPONENTS_CSS`; JS after `COMPONENTS_JS`) so `SharedBlockTests` pass,
   but do NOT restructure the widgets' render functions (that is Phase 2).
   Widgets must still render their current layout with the restyled
   primitives.
5. Remove `.stamp` CSS from `apps_eval.py`? **No** — Phase 1A leaves the
   widgets' local CSS alone except the splices; the stamp dies in Phase 2B.
6. Tests: update `DesignSystemTests` (four sizes, three weights, rotation
   list, raw-colour allow-list), `HitAreaTests` (add `.frame[role=button],
   button.frame, .mini a, .mini button` to INTERACTIVE), `SharedCssHygiene`,
   `StartupBehaviour` skeleton pins if class counts change, and add
   `BinderComponentTests` in `test_apps_shared.py`: a Node probe per builder
   (badge structure + tier ring class, statRow leader + key, pips lit/half
   counts, ribbon text + note + tier, trait icon kind, mini card lines,
   grainNode is an `svg` with a `filter`), `MOTION_CSS` inside the
   reduced-motion media query, `.deal` stagger cap, no `will-change`
   outside the hover media query, no `data:` anywhere.

Stopping: gates green; both widgets still render all preview samples; a
short return listing new constants, changed tests, and anything in gl.css
that could not be ported verbatim and why.

### Phase 1B — tooling (`scripts/preview_game_cards.py`, new `scripts/screenshot_widgets.mjs`, `scripts/preview_samples/*.json`, `tests/test_apps.py::PreviewScriptTests` only)

1. `preview_game_cards.py --from-json PATH`: render the widget from a saved
   tool payload instead of running the tool (grid or detail by shape);
   `scripts/preview_samples/` holds `discover_taste_match.json` and
   `detail_ghost_of_tsushima.json` (already written). `--tool-input` optional
   JSON for the header line.
2. `scripts/screenshot_widgets.mjs` (Node; uses the `playwright` module if
   resolvable, else prints how to install; never a project dependency):
   renders a list of preview HTML files at 360 and 760 px, light and dark
   (`--theme` previews) to PNGs under an output dir, mapping
   `images.igdb.com` / `i.ytimg.com` requests to a local cache dir when
   offline (`--img-cache DIR`), and prints each page's measured height.
   One `make`-free entry: `scripts/render_all_previews.sh` that writes every
   preview variant (grid/detail/eval 0–4, inline/fullscreen, light/dark,
   360/760) to `/tmp/gl-previews/`.
3. A `PreviewScriptTests` case for `--from-json` (both shapes) that does
   not need a database.

Stopping: `scripts/render_all_previews.sh` produces PNGs in this container
(the main session's image cache is at the path given in the brief), ruff
clean, tests green.

### Phase 2A — game cards (`apps.py`, `tests/test_apps.py` minus the classes Phase 1A owns, `scripts/preview_game_cards.py` samples if needed)

Rebuild `gridCard`, `renderGrid`, `headerLine`/`gridActions`,
`identityPanel`, `relatedBlock`, `detailCard` on the Phase 1A primitives to
match artboards E, F, D exactly (grid: set line, 2-up `.frame-s` cards with
badge/ribbon/plate/sub/chips/tags, footer actions; fullscreen: 4-up,
sticky set line with "Show next"; detail: top bar when drilled in, the big
frame with badge + art ribbon + plate + stats + chip row + 10 pips, then
description clamp, review `flavor-quote`, AWARD abilities from `features`
(only `the game awards - … - winner|nominee` entries, humanised, winners
first, ≤2 lines), tags line, reel, IN YOUR LIBRARY `.ministrip`, FROM THE
STUDIO strip or notice, actions). Deal-in on grid (M2) and detail (M1), press
(M3), skeleton resolve (M5). Keep: hand-back, drill-in, lightbox, rank from
`offset`, labels, number helpers, ≤3 chips, bounded strips (≤8 minis).

Tests: rewrite the `[V]` pins in `GameCardsResourceTests`,
`ContentTypeBadgeTests`, `GridModeTests`, `DetailModeTests` to the new
rules (frame tier from match/rating, badge content, ribbon conditions, sub
spans without middots, stats rows, pips count, award lines, set line
facets); keep every behavioural test.

### Phase 2B — evaluation card (`apps_eval.py`, `tests/test_apps_eval.py` minus Phase 1A's classes, `scripts/preview_eval_card.py` samples if needed)

Rebuild `headerNode`/`stampNode`/`scoreChips`/`callNode` into the frame
(artboard B): art + badge, plate, stats (PACE, SEEN, TARGET key, LENGTH,
FIT pips), straddling ribbon (verdict + mono note "wait for ~€40" when a
target exists). Ground content order: candidate line → summary `h` →
WEAKNESS traits → pitch → abilities (STUDIO/PEOPLE/MOMENT/ANTICIPATION) →
craft note `flavor` → reel → IN YOUR LIBRARY strip → actions → provenance.
At ≥560px container: frame left (300) and ground content right, reel full
width below. Breakdown (artboard C): traits two-column, GROUNDED strip of
minis with pips, LINEAGE two columns with notes, library/studio strips,
PAST VERDICTS ledger (date, ribbon-s, seen/target, skill version), reel
4-up, actions. Note cards (artboard G): horizontal `.frame-s` with
ribbon-s; void → `.tier-none` plain ribbon. Deal-in (M1 incl. ribbon stamp,
badge pop, pips, leaders), settle (M6), meter fills (M7), press, skeleton
resolve. Delete `.stamp` CSS/JS entirely.

Tests: rewrite `HtmlSanity` stamp pins to ribbon pins (verdict → tier map,
`.ribbon-straddle` present, notched clip-path, ink token, no rotate),
`LayoutTests` order pins to the new order, keep ActionRow/Error Node tests.

### Phase 3 — integration and judgment (main session + workers)

1. Gates. Then `scripts/render_all_previews.sh`; the main session compares
   every PNG against the canvas artboards.
2. Blind reader panel: three `sonnet-worker`s score the PNGs 0–10 per item:
   (1) same card family across all surfaces; (2) tier rules visibly honoured;
   (3) verdict + price call readable in the first 500px at 360; (4) nothing
   clipped, no overlap, ≥12px; (5) light theme parity; (6) motion present
   only where MOTION.md says, and off under reduced motion (checked by
   code read); (7) no generic-AI tells (middots, uniform soft-shadow cards,
   gradient washes); (8) state coverage (skeleton, notice, cancelled,
   empty, note, void). Any item <9 goes back to the owning executor.
3. Docs: this file's status, `CLAUDE.md` widget bullet (type scale and
   "frame/ribbon" wording), `docs/patterns/mcp-surface.md` widget section,
   `docs/README.md` index line. Commit on the designated branch; push.

## 3. Out of scope

Pointer-tracked holo tilt, gyroscope tilt, any animation of text or
numbers, new MCP tools or response fields, changing `response_encoding`,
re-styling the carousel/lightbox beyond tokens (it keeps working as is).
