# Spec: MCP Apps widget UX redesign (2026-10-03)

Status: approved by the main session; implemented in phases by executors.
Decision rule when a trade-off appears: **UX first, visual quality second,
everything else third.** Nothing in this spec changes a tool's wire contract
(no response field is added, renamed or removed) except where Phase B3 trims
*description text*.

Sources this spec is built on (read them before deviating):

- Claude MCP Apps design guidelines —
  https://claude.com/docs/connectors/building/mcp-apps/design-guidelines
- Claude "Blend your MCP App with Claude's theme" —
  https://claude.com/docs/connectors/building/mcp-apps/transparent-theming
- MCP Apps spec 2026-01-26 (ext-apps) — method shapes quoted in §1.4
- Anthropic "Writing tools for agents"; WCAG 1.4.1 / 1.4.3 / 1.4.11

Baseline measurements (before): evaluation card 2,400px tall at 760px and
~3,300px at 360px (9 stacked panels; price/target/HLTB last); type sizes
9/10/11/12/13.5px with weights 650–800; opaque cream/brown body background;
`appCapabilities: {}`; no `hostContext` handling; no skeleton; raw ids
(`steam_deck`, `ps5`, `n=114k`, `play_what_you_own`) shown as labels.

## 0. Invariants that hold in every phase

- Both widgets stay **self-contained on the wire**: one HTML string each, no
  build step, no CDN, no runtime fetch besides the allow-listed media hosts
  and the host's own font `@font-face` rules. The shared source lives in
  `gamelib_mcp/apps_shared.py` and is **spliced verbatim** (tests:
  `SharedBlockTests`, `WidgetDriftTests` — ≥20 identical significant lines
  between `apps.py` and `apps_eval.py` fails; put anything that large in
  `apps_shared.py`).
- All payload text goes through `textContent`, never `innerHTML` (tests pin
  this).
- `ui://` URIs stay content-hashed; `GAME_CARDS_URI` / `EVAL_CARD_URI`
  change automatically when the HTML changes.
- Preview scripts (`scripts/preview_game_cards.py`, `scripts/preview_eval_card.py`)
  must keep working: `window.__PREVIEW_DATA__` renders without a host, and
  `window.__PREVIEW_HOST_CONTEXT__` (new, optional) simulates a `hostContext`
  (theme, displayMode, deviceCapabilities, safeAreaInsets, styles.variables)
  so the preview can show Claude's tokens, dark mode and touch mode offline.
- Gates: `.venv/bin/ruff check gamelib_mcp tests scripts`,
  `.venv/bin/mypy gamelib_mcp`, `.venv/bin/python -m pytest tests/test_apps.py
  tests/test_apps_eval.py tests/test_tool_registration.py tests/test_docs_drift.py -q`.
  A test that pins the OLD look is updated to pin the NEW rule, never deleted
  without a replacement assertion.
- No new Python dependency. No new MCP tool, no new response field — with
  one deliberate amendment (2026-10-03, UX over invariant): `record_assessment`'s
  `package.game` gains an additive, optional `steam_appid`, resolved through
  the one existing Steam-appid chain, so the evaluation card can offer a
  store link on a buy/wishlist verdict. Nothing else on the wire changes.

## 1. Phase A — design system foundation (one executor, `apps_shared.py` + both widgets)

### 1.1 Host theming

1. Add `<meta name="color-scheme" content="light dark">` to both documents.
2. `html, body { background: transparent; }` — nothing paints a page
   background. Panels paint their own surface.
3. Every structural color, radius, border width, shadow and font goes through
   a host token **with a fallback** so ChatGPT/Goose/preview still render:

   | Role | Token (fallback) |
   |---|---|
   | page text | `--color-text-primary` (`light-dark(#141413,#FAF9F5)`) |
   | secondary text | `--color-text-secondary` (`light-dark(#3D3D3A,#C2C0B6)`) |
   | caption/muted | `--color-text-tertiary` (`light-dark(#6B6A64,#9C9A92)`, ≥4.5:1 on inset and surface) |
   | panel surface | `--color-background-primary` (`light-dark(#FFFFFF,#30302E)`) |
   | inset surface (skeleton, meter track, chip bg) | `--color-background-secondary` (`light-dark(#F5F4ED,#262624)`) |
   | panel border | `--color-border-tertiary` (`light-dark(rgba(31,30,29,.15),rgba(222,220,209,.15))`) |
   | strong border (focus, stamp) | `--color-border-primary` |
   | good / ok / bad text | `--color-text-success` / `--color-text-warning` / `--color-text-danger` |
   | good / ok / bad fill | `--color-background-success` / `-warning` / `-danger` |
   | good / ok / bad edge | `--color-border-success` / `-warning` / `-danger` |
   | neutral emphasis fill (match bar, primary button) | `--color-background-inverse` + `--color-text-inverse` |
   | radius | `--border-radius-md` (8px) panels, `--border-radius-sm` (6px) chips, `--border-radius-full` pills |
   | border width | `--border-width-regular` (0.5px) |
   | shadow | `--shadow-sm` on panels, none on chips |
   | font | `--font-sans` (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`) |

   Implement the fallbacks as a `:root { --gl-… : var(--color-…, <fallback>) }`
   layer so widget CSS references `--gl-*` names only (one place to change).
   The fallback dark values must also work when the host passes **no**
   variables but sets `theme`, and when nothing is passed at all
   (`prefers-color-scheme` then decides).
4. The bridge (`BRIDGE_JS`) grows a `hostContext` store and an
   `applyHostContext(ctx)` function, called with the `ui/initialize` result's
   `hostContext` and again with every `ui/notifications/host-context-changed`
   params (partial updates — merge, don't replace). It:
   - sets `document.documentElement.dataset.theme = ctx.theme` and
     `document.documentElement.style.colorScheme = ctx.theme`;
   - writes every entry of `ctx.styles.variables` onto `:root` with
     `style.setProperty`;
   - injects `ctx.styles.css.fonts` into a single `<style id="host-fonts">`
     (once; replace on change);
   - applies `ctx.safeAreaInsets.{top,right,bottom,left}` as padding on
     `body` (added to the base 12px gutter; never less than the base);
   - toggles `html.touch` from `ctx.deviceCapabilities.touch` and
     `html.no-hover` when `hover === false`;
   - records `ctx.displayMode` and `ctx.availableDisplayModes` for §1.4;
   - re-runs `reportSize()`.
5. `ui/initialize` sends `appCapabilities: { availableDisplayModes:
   ["inline", "fullscreen"] }` (both widgets). Keep `appInfo` + the legacy
   `clientInfo` alias.
6. Both resources register with `AppConfig(csp=…, prefersBorder=False)`.
   `resourceDomains` gains `https://assets.claude.ai` (host fonts). Update the
   two exact-list CSP tests accordingly and add one asserting `prefersBorder`
   is `False` on both resources.
7. `font-variant-numeric: tabular-nums` on every element that shows numbers
   in a row/column (chips, meta lines, fact rows).
8. Respect `prefers-reduced-motion: reduce` (no transforms/pulses).

### 1.2 Type scale, spacing, touch

- Exactly three sizes and two weights, as tokens:
  `--gl-h` = `--font-heading-md-size` (16px) weight 600;
  `--gl-body` = `--font-text-sm-size` (14px) weight 400 (600 when emphasised);
  `--gl-cap` = `--font-text-xs-size` (12px) weight 400 (600 for labels).
  Card/eval titles may use `--font-heading-lg-size` (20px) at ≥560px
  container width. **Nothing renders below 12px.** Line heights from the
  matching `--font-*-line-height` tokens (1.4 / 1.25).
- Section eyebrows ("Similar in your library") are `--gl-cap` weight 600,
  `--gl-muted`, letter-spacing 0.04em, uppercase — the only uppercase text
  besides the verdict stamp.
- Base gutter 12px (was 16px) on the body; panel padding 14px; grid gap 12px.
- Tap targets: every interactive element has a ≥44×44px hit area on
  `html.touch` and ≥32px otherwise. Visual size may stay smaller: use a
  `::after { inset: -N px }` hit-area extension on chips/links rather than
  inflating the visual chip. Buttons are 40px tall (44px on touch).
- Focus: `:focus-visible { outline: 2px solid var(--gl-border-strong);
  outline-offset: 2px }` on every interactive element.

### 1.3 Shared components (new constants in `apps_shared.py`)

All of these are consumed by Phase B; Phase A must ship them working and
unit-tested through the HTML markers.

- `LABELS_JS` — `PLATFORM_LABELS` and `VERDICT_LABELS` maps **generated in
  Python** from `platforms_registry.PLATFORMS` (`suggested_platform` and
  every platform field on the wire are registry names) and the verdict
  literals in `main.py`, embedded as JSON. Labels: steam → Steam, epic → Epic Games,
  gog → GOG, switch2 → Switch 2, ps5 → PS5, xbox → Xbox, itchio → itch.io,
  ea → EA app, ubisoft → Ubisoft Connect, other → Other. Verdicts: buy_now → Buy now,
  wishlist_for_sale → Wishlist for a sale, try_demo → Try the demo,
  skip → Skip, play_what_you_own → Play what you own. `label(kind, raw)`
  falls back to a title-cased, underscore-stripped form so an unknown value
  never renders raw. A test asserts every registry name and every verdict
  literal has an explicit label.
- `NUMBERS_JS` — `hoursLabel` (one copy; "27h", "2.3h", "~" prefix only for
  estimates), `compactCount` ("114k reviews", never "n=114k"),
  `money(amount, currency)` (symbol for EUR/USD/GBP, else code suffix),
  `plural(n, word)`.
- `SCORE_CHIP_JS` + `CHIP_CSS` — the one chip component for every score:
  `scoreChip({label, value, tier, title, url})` renders
  `<span class="chip tier-good|ok|bad|none"><span class="lbl">Metacritic</span><b>92</b></span>`
  (an `<a>` with the external-link affordance when `url` is set). Tier
  functions live here too: `mcTier` (≥75 good, 50–74 ok, <50 bad),
  `ocTier` (mighty/strong good, fair ok, weak bad; score fallback 84/75/65),
  `steamTier` (the nine phrases → good ≥ "positive", ok = mixed, bad below),
  `craftTier` (≥75/50). **Color encodes quality only; brand is the label
  text.** Status chips keep their outcome colour (Completed/Evergreen good,
  Abandoned bad) because a completion status is evidence quality for fit:
  finished = positive, abandoned = negative. The Steam chip shows the phrase ("Very positive") beside a 28×4px
  meter; the meter never appears without the phrase. Remove the brand hex
  palette and replace `test_rating_chips_use_source_brand_colors` with a
  test that (a) no brand hex remains, (b) the three tier classes map to the
  `--gl-good/ok/bad` tokens, (c) every chip has a text label.
- `MATCH_BAR_JS` — `matchBar(percent)` → labelled 4px bar
  (`<div class="match"><b>87% match</b><span class="track"><span class="fill" style="width:87%"></span></span></div>`),
  fill `--gl-inverse-bg`, track `--gl-inset`, `role="meter"`
  `aria-valuenow`.
- `SKELETON_CSS/JS` — `skeleton(kind)` for `grid` (4 cards: cover block +
  2 lines), `detail` (cover + 4 lines), `eval` (header row + 3 chips + 2
  lines). Rendered at startup until the first `tool-result`; also used while
  a `tools/call` is in flight. Blocks use `--gl-inset`; a 1.6s opacity pulse
  only when motion is allowed.
- `DISPLAY_MODE_JS` — `requestDisplayMode(mode)` → Promise resolving to the
  granted mode (`result.mode`), falling back to the current mode on error or
  2.5s timeout; `canFullscreen()` reads `availableDisplayModes` from the
  stored host context (`true` only if the array contains "fullscreen").
  `html[data-display-mode]` is kept current so CSS can branch.
- `MODEL_CONTEXT_JS` — `updateModelContext(text, structured)` →
  `ui/update-model-context` with `{content:[{type:"text",text}],
  structuredContent}`; `sendMessage(text)` → `ui/message` with
  `{role:"user", content:{type:"text", text}}`. Both are fire-and-forget
  and swallow method-not-found.
- `DISCLOSURE_JS` — `disclosure(parent, label, buildFn)` renders a 40px
  full-width button ("Full breakdown ▾") that on first click builds its
  content below itself, toggles `aria-expanded`, and calls `reportSize()`.
  Used as the in-place fallback when fullscreen is unavailable.
- `NOTICE_JS` — `notice(parent, items)` renders the muted caption
  "Couldn't load: trailer (Steam), studio (IGDB)" from a list of
  `{what, source}`; replaces every "some data unavailable" string.
- `EXTERNAL_LINK_JS` toast copy becomes
  "This host blocked the link." plus the URL as selectable text; no
  right-click instruction.

### 1.4 Bridge protocol details (hand-rolled, spec 2026-01-26)

Handle these inbound notifications: `ui/notifications/tool-input`
(`params.arguments` — store as `lastToolInput`), `tool-input-partial`
(ignore), `tool-result` (render), `tool-cancelled` (show
`notice` "Cancelled" and keep the skeleton out), `host-context-changed`
(§1.1.4), `resource-teardown` (reply `{}` and stop timers). Unknown host
requests still answer `-32601`.

### 1.5 Restyle pass on both widgets (Phase A scope)

Convert every rule in `apps.py` and `apps_eval.py` CSS to the token layer and
the type scale. Remove the 2px ink borders and hard offset shadows from
panels, cards, chips, thumbs and hero (use `--gl-border` 0.5px +
`--shadow-sm`). **Keep two deliberate brand elements**: the verdict stamp
(2px `--gl-border-strong`, 3px hard shadow, −3° rotation, tier fill) and the
cover fallback gradient plates. Grid card hover: translateY(−1px) + shadow-md,
no offset shadow. Rank badge: 12px, not rotated, `--gl-cap` weight 600,
`--gl-muted` on `--gl-surface`, "№ 1" → "#1".

Replace every raw-id render with `label(...)`; every count with the
`NUMBERS_JS` helpers. Drop the 10px truncated tag lines in the similar and
studio rows (those small cards carry: cover, name, year, and one chip
row — ≤3 lines of metadata).

Phase A stopping condition: both widgets render identically-structured
content to before (same sections, same order — structure changes are Phase
B) with the new design system; preview scripts accept
`__PREVIEW_HOST_CONTEXT__`; all gates green; a short return listing the new
shared constants and the tests changed.

## 2. Phase B — structure (three executors in parallel, disjoint files)

### 2.1 B1 — `apps.py` (game cards)

Grid mode:

1. **Header line** above the grid, built from `lastToolInput` when present
   and the payload otherwise: "`{shown} of {total_matches}` · sorted by
   `{taste match | critic score | value}` · `{vibes joined with ' + '}` ·
   `unplayed only` · `≤ {max_hltb_hours}h`" (omit absent parts; default
   sort is "taste match"; with no tool input just "`12 of 143 games`").
   `--gl-cap` muted, with the count in `--gl-text` weight 600.
2. **Card body order**: title (2-line clamp, `--gl-body` 600) → match bar
   (when `match_percent` present; this is the lead signal) → one meta line
   (`~27h · Steam Deck · 2.3h played`) → one chip row (Metacritic,
   OpenCritic, Steam via `scoreChip`, max three) → up to 3 matched tags as
   plain muted text separated by " · " (not pills). The Metacritic cover
   corner chip stays (same chip component, tier-colored) — it is the only
   thing on the cover besides the rank badge and the DLC type chip.
3. **Footer actions** (≤2, bottom): when `has_more`, a secondary button
   "Show next {limit}" → `sendMessage("Show the next {limit} recommendations
   (offset {offset+limit})")`; and when `canFullscreen()`, an "Open full screen"
   button → `requestDisplayMode("fullscreen")`. In fullscreen the grid uses
   `minmax(160px, 1fr)` and the host's close button returns.
4. **Card tap** (replaces the overlay entirely — delete `openOverlay`,
   `openDetail`, the overlay CSS, and `test_screenshots_open_a_carousel_over_the_detail_card`'s
   overlay expectations; the screenshot lightbox stays for the detail card's
   own strip, see 5): always `updateModelContext("User selected {name}
   (game_id {id}) from the recommendations", {game_id, name})`; then if
   `canFullscreen()`: request fullscreen, render `skeleton("detail")`, call
   `tools/call get_game_detail {game_id, media:true}`, render the detail
   stack with a top bar (← Back to results, title) — Back re-renders the
   grid and requests `inline`; else `sendMessage("Show me {name}")`.
5. **Detail mode** (`get_game_detail` single result): order is identity
   panel → media reel → disclosure. Identity panel: cover (120px wide at
   ≥560px, 84px below), title, sub line (`2025 · Steam · 132h played ·
   wishlisted`), chip row via `scoreChip` (Metacritic, OpenCritic, Steam,
   HLTB, ProtonDB — HLTB and ProtonDB tier `none`), "Your rating 9/10" as a
   chip with tier good ≥7 / ok ≥5 / bad, description clamped to 3 lines
   with a "more" toggle, tags as one muted text line (≤8). Media reel
   unchanged in behavior (hero + thumbs + lightbox) but restyled. Then
   `disclosure("Similar games you own · From the studio", …)` builds the
   similar and pedigree rows; when `canFullscreen()` the disclosure button
   instead requests fullscreen and renders everything expanded. Empty-state
   reasons render through `notice` with humanized provider text
   ("IGDB: no match", "Steam: no app id").
6. **Carousel rows** (similar, studio): cards 120px wide, cover, name
   (2-line clamp), year, one chip row (`Your 9/10` or critic score, then
   `owned`/`unplayed`), horizontal scroll with `scroll-snap`, the next card
   peeking (padding-right so the last card is reachable; `scroll-padding`
   from safe-area insets).

Tests: update `test_apps.py` for the removed overlay, the header line, the
match bar, the chip component, the model-context call on tap, the
fullscreen request path, and the `has_more` button. Keep every CSP and
innerHTML test.

### 2.2 B2 — `apps_eval.py` (evaluation card)

New order and tiering:

**Inline (always rendered):**

1. Header panel: cover 84px; title (`--gl-h`, 20px at ≥560px); sub line
   (`2025 · wishlisted` / `Steam · 132h played`); verdict stamp top-right
   (stacks under the title below 420px, never overlapping it).
2. Score row (`scoreChip`): craft (`Craft 93%` + meter, tier), trajectory
   (`↗ Improving` — tier good/none/bad), Metacritic, OpenCritic, fit
   (`Fit · Strong fit`, tier by FIT_CLASSES mapping). Then `craft_note` as
   `--gl-body` secondary text.
3. **The call** moves here, directly under the scores: fact chips HLTB,
   pace, price seen, target, paid — and the `flags` as danger-tier chips.
   This is the block that was last and is now third.
4. Summary (`--gl-body` 600) and `elevator_pitch` (quoted block, secondary
   text, 2px left rule in `--gl-border-strong`), then `why_care` lines with
   their kind eyebrow.
5. Media panel (hero + thumbs) when media exists.
6. One action row at the bottom (≤2 actions): primary "Full breakdown"
   (fullscreen if `canFullscreen()`, else `disclosure` in place) and, when a
   store URL is derivable (Steam app id), secondary "Store page ↗" via
   `openLink`. If nothing below exists (minimal package), no action row.
7. When `package.errors` is non-empty: a single `notice` line under the
   actions naming what failed.

**Full breakdown (fullscreen or disclosure):** for you / not for you,
grounded-in-your-history anchors, lineage, similar in your library, from
the studio, past verdicts. Same section components as today, restyled by
Phase A. In fullscreen render the inline card at the top and the breakdown
below, max-width 760px centered.

Note cards (no package / void): "Recorded — Play what you own · Slay the
Spire II" with the stamp; verdict through `label("verdict", …)`.

Stopping: the inline card at 760px is ≤ 900px tall with the full Steam
sample (media included) and ≤ 1,300px at 360px; `preview_eval_card.py`
samples 0–4 all render; tests updated (render-branch coverage test must
still cover the whole contract; add assertions for the facts-before-pitch
order, the ≤2 actions, the fullscreen request, and the humanized note
card).

### 2.3 B3 — tool descriptions (`main.py`, tests, skills/docs)

Goal: every description reads like onboarding a colleague — first sentence
"when to call this", second "what comes back", then only the rules the
model needs **at call time**. Move methodology and server-internals prose
to `skills/*/` or `docs/patterns/*` and leave a one-line pointer.

- Targets: the six largest descriptions (`get_stats`, `record_assessment`,
  `add_game_to_platform`, `update_game`, `set_acquisition`,
  `get_assessment_context` — confirm by measuring) each cut ≥25% in chars
  with **zero contract loss**: every parameter semantics, error condition,
  default, cap and identity rule that affects how the model forms a call
  stays in the docstring. Narrative justification ("because the mapping is
  not infallible"), repeated examples and duplicated field lists move out.
- Tighten `SchemaBudgetTests` constants to the new achieved numbers (+5%
  headroom) so the gain cannot silently regress.
- `tests/test_docs_drift.py` and the skills' references to field names must
  still pass; if a skill quotes a docstring sentence that moved, update the
  skill's pointer.
- Return: a before/after table (chars per tool, total payload bytes) and the
  list of sentences moved, with destination paths.

## 3. Phase C — judgment loop

A blind reader panel (three `sonnet-worker`s) scores screenshots and code
against the rubric below, per item 1–8, 0–10 each, with one sentence per
deduction. The main session scores independently. Any item < 9 on either
score goes back to an executor with the deductions as the delta. Loop until
every item ≥ 9.

Rubric per item (10 = all true):

1. Host theming: transparent page, tokens with fallbacks, theme follows
   `hostContext` and `host-context-changed`, fonts injected, safe areas,
   `prefersBorder` false, both modes screenshot-verified, no hardcoded
   structural color left.
2. Eval card tiering: verdict + scores + call facts visible in the first
   ~600px at 360px; ≤2 actions at the bottom; fullscreen/disclosure holds
   the rest; nothing lost versus the old card.
3. Score semantics: one chip component; color = quality tier everywhere;
   every chip has a text label; Steam phrase present; match bar leads the
   grid card.
4. Labels: no raw identifier anywhere in either widget; registry-derived
   map; unknown values degrade gracefully.
5. Grid framing and hand-back: header line; `has_more` action; tap → model
   context + fullscreen or chat message; no in-widget overlay.
6. Loading and failure: skeleton per widget shape; named failures; cancel
   handled.
7. Scale and touch: 3 sizes / 2 weights, nothing < 12px, 44px hit areas on
   touch, 32px otherwise, focus rings, reduced motion respected, carousel
   peek + snap.
8. Descriptions: ≥25% cut on the six largest, no contract loss (reviewer
   diff-checks each removed sentence), budgets tightened.

## 4. Phase D — mock-ups

Render with `scripts/preview_*.py` + headless Chromium through a 360px and a
760px iframe, light and dark (`__PREVIEW_HOST_CONTEXT__` carrying Claude's
token table), with cover art downloaded locally so the plates show real art.
Deliver: grid (light/dark/360), detail (light), eval inline (light/dark/360),
eval full breakdown (light).
