# Game Library widget design language — "The Binder"

Chosen direction: the trading card (canvas artboard 5 · Holo). This document
generalises it into one system for every widget surface. Every artboard
built from it inlines the SAME `gl.css` verbatim (no edits, no forks) and adds
only board-specific layout rules. If a component is missing, extend `gl.css`
in phase 1, never invent a local variant in phase 2.

## 1. The idea

Every game is a collectible card. A results grid is a binder page. The
evaluation is the card plus its verdict ribbon. The detail view is the card
up close with its full stat block and the rest of the set. The user is a
video-game nerd: the pleasure comes from stat blocks with dotted leaders,
rarity borders that MEAN something, pips, card numbers, flavor text in
italic serif, a grain you can almost feel — never from gloss or neon.

Rarity (the border) is never decoration. It encodes the one quality tier
the card is about:

| Surface | The card's tier comes from | Lead number in the badge |
|---|---|---|
| Evaluation card | verdict: buy_now/play_what_you_own → good, wishlist_for_sale/try_demo → ok, skip → bad | OpenCritic (Metacritic fallback) |
| Discover grid (sort match) | match_percent: ≥70 good, 45–69 ok, <45 common (no tier) | match % |
| Discover grid (sort critic / value) | Metacritic/OpenCritic tier (≥75 good, 50–74 ok, <50 bad) | Metacritic (OpenCritic fallback) |
| Detail card | your rating: ≥7 good, 5–6.9 ok, <5 bad; unrated → critic tier; nothing → common | your rating "8" with "/10" (else critic) |
| Note card (recorded verdict) | verdict tier | none |
| Skeleton | common | none |

"Common" = plain 1px `--gl-border` with a 2px inner keyline. Tiered =
6px (big cards) / 4px (grid cards) brushed-metal conic border:
good = emerald (#437426 → #7AB948 → #A8D98A → #437426), ok = amber
(#8A6A24 → #D1A041 → #F0D08A → #8A6A24), bad = ruby (#602A28 → #EE8884 →
#A73D39 → #602A28). Light mode uses the same hues at the light-token
lightness (good #265B19/#437426/#7AB948, ok #805C1F/#B8891F/#D1A041, bad
#7F2C28/#A73D39/#EE8884). Colour outside the border, the ribbon, pips and
badge ring is forbidden: tier never tints text blocks or backgrounds.

## 2. Tokens (all through the existing `--gl-*` layer, host vars + fallbacks)

| Role | Dark | Light |
|---|---|---|
| chat ground (never painted by us) | #262624 | #FAF9F5 |
| card body `--gl-surface` | #30302E | #FFFFFF |
| inset `--gl-inset` (stat rows, thumbs bg) | #262624 | #F5F4ED |
| deep plate `--gl-deep` (badge fill, ribbon text) | #141413 | #141413 |
| text primary / secondary / tertiary | #FAF9F5 / #C2C0B6 / #9C9A92 | #141413 / #3D3D3A / #73726C |
| hairline `--gl-border` | rgba(222,220,209,.15) | rgba(31,30,29,.15) |
| keyline (inside art) | rgba(250,249,245,.14) | rgba(20,20,19,.12) |
| good / ok / bad text | #7AB948 / #D1A041 / #EE8884 | #265B19 / #5A4815 / #7F2C28 |
| good / ok / bad fill | #1B4614 / #483A0F / #602A28 | #E9F1DC / #F6EEDF / #F7ECEC |
| ribbon fill (tier) | good #7AB948, ok #D1A041, bad #EE8884 — text always #141413 | same |
| inverse (primary button, badge) | #FAF9F5 bg / #141413 text | #141413 bg / #FFFFFF text |
| grain opacity | .06 | .035 |

Type (host font = Anthropic Sans → system-ui; never a web font):

| Token | Size / weight / family | Use |
|---|---|---|
| `display` | 28px / 800 / sans, tracking −.02em | nothing yet; reserved for fullscreen titles |
| `title` | 20px / 800 / sans | card title on 300px cards |
| `title-s` | 16px / 800 / sans, 2-line clamp | grid card title |
| `h` | 16px / 600 / sans | section heads, summary line |
| `body` | 14px / 400 / sans | prose |
| `cap` | 12px / 400 or 600 / sans | labels, meta |
| `label` | 12px / 600 / sans, uppercase, tracking .06em | stat labels, ability run-ins, badge tags — the ONLY uppercase |
| `num` | 16px / 500 / `ui-monospace, Menlo, Consolas, monospace`, tabular | stat values, prices, hours |
| `num-l` | 20px / 700 / mono | badge number |
| `flavor` | 14px / 400 italic / `ui-serif, Georgia, serif` | craft note, description, your own review quote |

Nothing under 12px. Line heights 1.4 (body/cap), 1.25 (titles), 1.0 (nums
in badges). Tabular numerals on every number.

Space: 4px base. Card padding 14. Grid gap 12. Page gutter 12 (+ safe area).
Radii: card 14, grid card 12, art inside card 8, stat rows 0, chips 4, badge
and buttons 9999. Nothing else is rounded.

## 3. Components (class names are the contract; build them in `gl.css`)

1. `.gl-card` — the frame. `position:relative; border: 6px solid transparent; border-radius:14px; background: linear-gradient(var(--gl-surface),var(--gl-surface)) padding-box, <tier conic> border-box`. Modifiers `.tier-good|.tier-ok|.tier-bad|.tier-none`, `.gl-card--s` (4px border, radius 12, for grids). Contains `.gl-grain` (an inline `<svg>` with `feTurbulence baseFrequency .9 numOctaves 2`, absolutely positioned, `pointer-events:none`, opacity per theme, mix-blend-mode: overlay) as its first child — NEVER a data-URI background (CSP). A whole card that is tappable is a `<button class="gl-card ...">` (or `<a>`), focus ring `outline:2px solid var(--gl-border-strong); outline-offset:3px`.
2. `.gl-art` — the art window: `aspect-ratio` 2/3 (cover) or 16/9 (hero), `overflow:hidden`, radius 8, `box-shadow: inset 0 0 0 1px <keyline>`, one static specular: `::after` a 1px white line at 12% opacity, 35deg, from top-right. `.gl-art--plate` is the no-art fallback: the existing name-seeded gradient plate with the title centered in `title-s`.
3. `.gl-badge` — overall badge: 52px circle, inverse fill, `num-l` number, 2px tier ring (`box-shadow: 0 0 0 2px var(--tier)`), positioned top-left over the art (`.gl-badge--on-art` at 10px/10px). Tag beneath: `.gl-badge__tag` a 12px `label` on a deep plate (e.g. OPENCRITIC, MATCH, YOUR RATING). The badge can carry a suffix span `/10` in 12px.
4. `.gl-plate` — the title plate under the art: `title` (or `title-s`) left, `.gl-no` card number right (`cap` mono, "No. 046" — the game_id or assessment id; this is honest data, never invented). Second line `.gl-sub`: separate spans (studio, year, platform lozenge) separated by `gap:10px`, never by middots or pipes.
5. `.gl-loz` — platform lozenge: `label` text on `--gl-inset`, 1px border, radius 4, 20px tall ("PS5", "Steam", "Switch 2", "Epic" — the registry labels).
6. `.gl-stats` — stat block: rows `.gl-stat` with `label` left, dotted leader (`flex:1; border-bottom:1px dotted <tertiary>; margin: 0 8px 4px`), `num` right. Optional `.gl-stat__note` (`cap` tertiary) after the label ("last 30d", "on PS5", "main story"). The strongest value gets `.is-key` (primary colour, weight 700). Max 6 rows on a card; the detail card may reach 7 when it shows the three HowLongToBeat lengths.
7. `.gl-pips` — diamonds (`<span>` rotated 45°, 8px, 4px gap). `.gl-pips--3` fit (lit count 1–3), `.gl-pips--10` rating (lit = round(rating), `.half` = half-lit via linear-gradient). Lit colour = tier of the value; unlit = `--gl-border`.
8. `.gl-ribbon` — the verdict/status band: 40px tall (grid variant `.gl-ribbon--s` 24px, 12px text), tier fill, `#141413` text, `label` at 14px/800 tracking .1em, notched ends via `clip-path: polygon(0 0,100% 0,calc(100% - 10px) 50%,100% 100%,0 100%,10px 50%)`, overhanging the card by 20px each side when `.gl-ribbon--straddle` (eval), or sitting flush at the art's bottom edge inside the frame (`.gl-ribbon--art`, grid/detail). Two spans allowed: the verdict and a short right-aligned mono note ("wait for ~€40", "82h").
9. `.gl-trait` — for-you / weakness rows: 20px inline SVG icon + 14px text; `.gl-trait--plus` a four-point spark in good text colour, `.gl-trait--minus` the shield outline in bad text colour. `.gl-traits__head` is a `label` in the matching tier colour ("FOR YOU IF", "NOT FOR YOU IF", "WEAKNESS").
10. `.gl-ability` — run-in: `<b class="label">STUDIO</b>` then 14px body on the same line. Kinds: STUDIO, PEOPLE, MOMENT, ANTICIPATION, AWARD.
11. `.gl-flavor` — italic serif 14px secondary, optional `.gl-flavor--quote` with a hanging 28px serif quotation mark in tertiary (your own review).
12. `.gl-chip` — secondary score chip: 1px tier border, radius 4, 24px tall, `cap` label + `num` value (e.g. "Metacritic 83", "Steam Very positive" + the 28×4 meter, "HLTB 25h" tier none). Tier colour only on the border and value.
13. `.gl-mini` — mini card for strips (similar, studio, anchors, lineage): 48×64 art with 2px tier border radius 6, name 14/600 2-line clamp, two `cap` lines (e.g. "9/10 · 50h" is BANNED — use two spans: `<span>9/10</span><span>50h</span>` with gap). `.gl-strip` = horizontal scroll-snap row, 12px gap, last card peeking, `scroll-padding` from safe area.
14. `.gl-reel` — media: `.gl-art` 16/9 stage with `.gl-play` (44px round inverse chip, triangle SVG, bottom-right so it never covers title lettering) + `.gl-thumbs` 3-up (or 4-up ≥560px) 16/9 thumbs, 1px keyline, active thumb gets a 2px inverse outline.
15. `.gl-btn` — pill, 44px tall (touch) / 40px, `h` weight 600, `.gl-btn--primary` inverse fill, `.gl-btn--secondary` 1px `--gl-border-strong`, optional leading 20px SVG glyph (chevron, external-arrow). Max two per surface, in one `.gl-actions` row.
16. `.gl-set` — the set line above a grid (what this binder page IS): `<b>8 of 2090</b>` (`h`) + `.gl-loz`-style facets ("taste match", "unplayed", "roguelike", "≤ 10h") as lozenges; never a middot string.
17. `.gl-skel` — skeleton: a `.gl-card.tier-none` with inset blocks (art block + 2 lines, or hero + 4 lines); 1.6s opacity pulse only when motion is allowed.
18. `.gl-notice` — 12px tertiary with a 16px outlined circle-"!" SVG: "Couldn't load: trailer (Steam)".
19. `.gl-eyebrow` — section head above strips: `label` tertiary ("IN YOUR LIBRARY", "FROM THE STUDIO", "GROUNDED IN YOUR HISTORY", "LINEAGE", "PAST VERDICTS").

Motion: hover on a tappable card = translateY(−2px) and the specular line
shifts 20px (200ms); press = translateY(0). Nothing animates on load. All
of it is inside `@media (prefers-reduced-motion: no-preference)`.

## 4. Surface recipes

**Eval inline (390)** — as artboard 5 with the system classes: card
(tier = verdict) → art (cover) + badge (OpenCritic) → plate (title, No.,
sub) → stats (PACE, SEEN, TARGET key, LENGTH, FIT pips) → straddling ribbon
(verdict + "wait for ~€40"). Below on the ground: candidate line (cap),
summary (`h`), WEAKNESS traits, pitch (body), abilities (STUDIO, MOMENT),
craft note as flavor, reel, IN YOUR LIBRARY strip, actions, provenance cap.

**Eval full breakdown (760)** — two columns: the inline card (300) sticky
left; right: summary + pitch, FOR YOU IF / NOT FOR YOU IF traits in two
columns, GROUNDED IN YOUR HISTORY strip of minis (rating pips + hours),
LINEAGE as two mini columns (Ancestors / Similar) with their note as cap,
IN YOUR LIBRARY, FROM THE STUDIO, PAST VERDICTS as a small table (date,
verdict lozenge in tier colour, price seen → target), reel, actions.

**Grid (390)** — set line; 2 columns of `.gl-card--s` (tier = match): art
2:3 + badge (match %) top-left + `.gl-no` rank "No. 1" on the plate right;
slim ribbon at the art's bottom only when the card is ≥70 ("TOP MATCH")
or has a Metacritic ≥90 ("CRITICS' PICK") — otherwise none; plate:
title-s, sub spans (hours or "~4h", platform lozenge); one chip row (≤2);
matched tags as one cap tertiary line (" · " banned → use gap-separated
spans with a 4px dot ONLY as a CSS `::before` bullet, if needed, or just
spaces). Footer: `.gl-actions` with "Show next 8" (secondary) and "Open
full screen" (primary). **Grid (760)** — 4 columns; the set line sticks.

**Detail (390)** — the big card (tier = your rating): art = cover 2:3 at
full card width with badge "8 /10" + tag YOUR RATING; plate; stats
(PLAYED 82h key, LAST Sep 2022, LENGTH 25h main story, PAID free, SCORES
chip row as its own row); 10 rating pips; art-edge ribbon "COMPLETED".
Below: description as body (3-line clamp + "more"), your review as
`.gl-flavor--quote`, AWARD abilities (real `features` data), tags as cap
line, reel (trailer + 4 thumbs), IN YOUR LIBRARY strip, FROM THE STUDIO
strip (or the notice when empty), actions ("Full breakdown" / "Back to
results" when drilled in from a grid).

**Note card (390)** — a horizontal `.gl-card--s` (tier = verdict): 48×64
art, title-s, ribbon--s "PLAY WHAT YOU OWN", cap "Recorded 3 Oct 2026",
one secondary action "See the full card".

**States (390)** — skeleton grid (4 cards), skeleton eval, a notice row, a
"Cancelled" note, an empty grid ("No games match · try fewer vibes" as
two spans).

**Light (390)** — eval inline and grid in light tokens, proving the
rarity borders and grain still read on white.

## 5. Non-negotiables carried from the repo spec

Transparent page; everything through `--gl-*` tokens with fallbacks; host
font only; no CDN, no data: URIs, images only from allow-listed hosts; all
text via textContent in the real build; ≥44px touch targets; focus rings;
reduced motion respected; nothing under 12px; responses stay bounded (strips
show ≤8 minis); no raw ids (labels through `label()`); colour = quality
tier only.
