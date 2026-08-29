# Evaluation Package — design sketch

**Status:** design/brainstorm, not yet an implementation plan.
**Feature:** when the game-quality skill delivers a verdict, a rich visual
"evaluation package" appears in the chat — trailer, screenshots, description,
an AI-written elevator pitch, "for you if / not for you if", comparisons and
lineage, price and time context, and the verdict itself — everything needed to
decide whether the game is right for John, in one card.

## Why this is cheap to build here

The hard infrastructure already exists:

- `apps.py` is a working MCP Apps widget (toybox visual language, hand-rolled
  postMessage bridge, content-hashed `ui://` URI, CSP allowlisting the two
  cover-art hosts) attached to `discover_games`/`get_game_detail`, rendering
  on claude.ai today. The evaluation package is a **second widget** in the
  same style, attached to a different tool.
- The game-quality skill already computes almost everything the card shows:
  craft (sample-adjusted), fit + anchors, pace, price, flags, verdict — and
  already ends every assessment with a `record_assessment` call carrying
  those exact components.
- What's genuinely new: **media fetching** (nothing in the codebase touches
  screenshots/trailers/similar-games today) and **presentation fields** the
  model authors (pitch, for-you-if bullets, lineage).

## The central design decision: where does the card attach?

The AI-synthesized content (elevator pitch, "for you if…") is authored by the
assessing model, not the server — so it must flow **into** a tool call for the
widget to render it. Three options:

1. **Extend `record_assessment` (recommended).** The skill already calls it
   once per verdict with the verdict components. Add optional presentation
   fields (see below); the response assembles the full package (components +
   media + comparisons resolved against the library) and carries the new
   `EVAL_CARD_APP`. One call, no new tool (ADR 0004 stays happy), and the
   "silent bookkeeping" step becomes the visible finale of every assessment.
   Hosts without Apps support see the normal JSON — purely additive.
2. A separate display-only `present_evaluation` tool. Cleaner annotations
   (read-only), but the skill would make two calls with ~identical payloads,
   and ADR 0004 pushes against a second tool for the same operation.
3. Attach to `get_assessment_context`. Wrong timing — Step 0 runs before the
   verdict or pitch exists.

Option 1 consequences to accept:

- `record_assessment` stays a MUTATION tool (it already is); the package
  response makes it slower. Media must come from cache or a short-timeout
  fetch that **degrades gracefully** (card renders without a trailer rather
  than the recording failing — recording the verdict must never be blocked
  by a media fetch).
- The presentation fields get persisted with the assessment (one JSON
  `presentation` column, capped size, migration vN). Rationale: the pitch
  and for-you-if reasoning are part of the verdict record — repeat asks
  (`past_assessments`) can then show what was said last time, and
  calibration can later compare the pitch against what actually happened.
  Declared content, like the provenance columns: stored as claimed, never
  server-synthesized.
- Skill bump to 3.0: Step 4 grows an "author the package" section; recording
  is no longer described as silent.

## Data sourcing (all new work lives in `data/`)

**Steam (candidates with an appid — the common case, owned or not):**
appdetails already returns media; we just don't ask for it. Add
`screenshots,movies` to the `filters` param in `fetch_store_appdetails`.
`screenshots[]` → `path_thumbnail`/`path_full` on
`cdn.cloudflare.steamstatic.com` (already CSP-allowlisted for images);
`movies[]` → thumbnail + mp4/webm URLs (`480`/`max`) on Steam's CDN.
`short_description` is already fetched/stored. Note the 7-day store cache:
cached rows predate the new filters, so the package path should fetch
on demand (through the existing quota-budgeted gate) rather than trust the
stored row to carry media.

**IGDB (non-Steam candidates — Switch, PSN):** add
`screenshots.image_id, videos.video_id, videos.name, summary, similar_games`
to the fetch fields (or a dedicated media query to keep the hot search
queries lean). Screenshots resolve to `images.igdb.com` (already
CSP-allowlisted); videos are YouTube IDs.

**Caching:** a small `data/media.py` (fetch-by-appid/igdb-id, meta-KV cache,
~7-day TTL, hard caps: ≤6 screenshots, ≤1 trailer) rather than new columns on
`games` — evaluation candidates are often unowned/minted rows that
enrichment never touches, so fetch-on-demand keyed by store identity is the
right model.

**Similar games:** IGDB `similar_games` cross-referenced against the library
server-side → each entry annotated `owned`/`unplayed`/`rating`/`playtime`.
"You already own 3 of the 8 most similar games, 2 unplayed" is the visual
form of the *play-what-you-own* verdict.

## The card, top to bottom

Server-supplied (resolved/fetched at record time):

- **Hero**: trailer — inline `<video>` with the constructed Steam mp4 (see
  spike results), YouTube embed for IGDB-only candidates, poster + link-out
  as the last rung — or lead screenshot; cover art; title, year, platforms.
- **Verdict stamp**: BUY NOW / WISHLIST FOR SALE / TRY DEMO / SKIP / PLAY
  WHAT YOU OWN INSTEAD — big toybox-style rotated stamp. The one-line
  summary under it.
- **Screenshot strip**: 4–6 thumbnails, click-to-enlarge in-widget
  (overlay pattern already exists in game-cards).
- **Craft / Fit / Time / Price rows**, mirroring the skill's verdict block:
  craft meter with band label + trajectory arrow; fit call with **anchor
  chips** (cover + his rating + completion status — an `abandoned` anchor
  renders as a warning, `evergreen`/`completed` as strong positives); HLTB vs
  his actual pace ("≈9 weeks at your current pace" from the `pace` block);
  price seen / target price / already-owned acquisition line.
- **Flags** as warning stickers.
- **Similar games row** (IGDB, library-annotated as above).
- **Past verdicts timeline** when `past_assessments` exists — repeat asks
  show the prior call, date, and price seen then.

Model-authored (new optional `record_assessment` fields, all capped):

- `elevator_pitch` — ≤ ~280 chars, synthesized, spoiler-free.
- `for_you_if` / `not_for_you_if` — up to 3 bullets each, **grounded in his
  data** ("you put 244h into Slay the Spire", "you abandoned both survival
  crafters you tried"), not generic genre talk. The skill instructs the
  model to cite anchors/affinities, and the server resolves any named games
  to library rows so the card can render them with covers/ratings.
- `comparisons[]` — `{name, relation, note}` where relation ∈
  `better_version` ("a better version of this exists"), `similar`,
  `ancestor` ("this game is a baby of…"), `descendant` ("…games that are
  babies of this game"), `cheaper_substitute`. Rendered as a little
  **lineage strip**: ancestors → candidate → descendants, with
  better-versions called out. True inspired-by genealogy isn't in any
  structured source — it's model knowledge, which is exactly why it's a
  declared field; the server only annotates ownership.

## Brainstorm — further ideas, roughly ordered by value/effort

- **"What players say" pull-quotes**: 2–3 short review snippets. The
  healable `steam_reviews` scraper already exists; a snippet mode is a
  natural extension. High value — reviews-in-their-own-words beat scores.
- **Session-shape badges**: "save anywhere", "run-based", controller
  support — the `STEAM_FEATURE_FLAGS` machinery already quarantines these
  out of the taste vocabulary, but they're exactly the *context* metadata
  the card wants. Reuse the flag lists as a positive surface here.
- **Price-history context**: "historical low €9.99, hits −50% ~3×/year" via
  the existing ITAD client (currently wishlist-only) keyed by appid — turns
  "wishlist for sale" into a concrete waiting game.
- **Backlog-pressure footer**: "€412 already spent on unplayed games; 3
  unplayed close substitutes" from `get_stats(report="backlog")` data —
  the do-you-need-this-now gut check, one quiet line.
- **Demo availability**: appdetails carries demo appids; a "Try the demo"
  verdict could link straight to it.
- **Franchise history strip** for sequels: every owned series entry with
  playtime (data already behind `get_stats(report="series")`) — "you own 4
  Yakuza games, played 1".
- **Deferred**: review-sparkline over time (needs SteamDB-grade data we
  don't have), accessibility metadata (no good source), Deck-verified
  status (no public API field in appdetails).

## Spike results (2026-08-28) — inline video is GO, with one caveat

The CSP/media spike ran; findings, most important first:

1. **The spec supports everything we want.** MCP Apps (SEP-1865, the
   ext-apps spec fastmcp 3.4.7 implements) maps `resourceDomains` to
   `img-src, script-src, style-src, font-src, media-src` — so `<video>` is
   spec-legal, not a stretch goal — and `frameDomains` maps to `frame-src`
   with `["https://www.youtube.com"]` as the spec's *own example*.
   `ResourceCSP` in fastmcp 3.4.7 already exposes all four fields
   (`connect_domains`, `resource_domains`, `frame_domains`,
   `base_uri_domains`); no SDK blocker. Iframe sandbox is
   `allow-scripts allow-same-origin` (downloads blocked), which is enough
   for a nested YouTube player.
2. **Steam media filters verified live.** Adding `screenshots,movies` to
   `fetch_store_appdetails`' `filters` adds exactly those two keys — no
   filter breakage (`basic` was always the big group). Screenshots come as
   `path_thumbnail` (600x338) / `path_full` (1920x1080) on
   `shared.akamai.steamstatic.com` — NOT the currently allowlisted
   `cdn.cloudflare.steamstatic.com`; the `shared.cloudflare.steamstatic.com`
   mirror 301s. Allowlist both `shared.*` hosts.
3. **appdetails trailers are now DASH/HLS manifests only** (`dash_av1`,
   `dash_h264`, `hls_h264` on `video.akamai.steamstatic.com`; no mp4/webm
   keys anymore) — a bare `<video>` cannot play those in Chrome/Firefox,
   and the widget is deliberately player-library-free. **BUT the legacy
   constructible mp4 renditions still exist**, verified even for
   2025-uploaded trailers (Hades II v1.0 Showcase, Silksong release
   trailer): `https://cdn.cloudflare.steamstatic.com/steam/apps/<movie_id>/movie480.mp4`
   (also `movie_max.mp4`, also on `cdn.akamai.` and `shared.akamai.`),
   `content-type: video/mp4`, CORS `*` — and `cdn.cloudflare.steamstatic.com`
   is already in the widget allowlist. So the hero is a plain
   `<video controls preload="none" poster=<movie thumbnail>>` with the
   constructed mp4 URL — zero bandwidth until play, no JS player.
   *Caveat*: the mp4 pattern is undocumented legacy surface Valve could
   drop. Mitigation: the server HEAD-validates the constructed URL at
   package-assembly time (cheap, cacheable) and omits the video on 404;
   the widget additionally falls back on the `<video>` error event to
   poster + `ui/open-link`. That fallback chain also covers a host that
   strips `media-src`, making the remaining claude.ai question
   non-blocking.
4. **IGDB untested live** (no creds in the spike environment) but
   documented: `videos.video_id` is a YouTube ID → `frameDomains`
   YouTube embed (`www.youtube-nocookie.com`) is the trailer route for
   non-Steam candidates. A CSP-blocked nested iframe is hard to detect
   from JS, so YouTube stays the secondary route; Steam mp4 is primary
   whenever an appid exists.
5. **The one thing not verifiable from a container**: whether claude.ai's
   host actually honors `media-src`/`frame-src` declarations (the current
   widget only exercises `img-src`). A spec-compliant host must; the
   fallback chain in (3) means we don't need the answer before building —
   the first deploy IS the probe. Check both claude.ai and the chatgpt.com
   client (both are registered against prod, per ADR 0004's probe habit).

Evaluation-card CSP, as of the spike:
`resource_domains`: `images.igdb.com`, `cdn.cloudflare.steamstatic.com`,
`cdn.akamai.steamstatic.com`, `shared.akamai.steamstatic.com`,
`shared.cloudflare.steamstatic.com`; `frame_domains`:
`https://www.youtube-nocookie.com`.

## Constraints

1. **Bounded responses**: every media/comparison list capped with true
   totals + truncation flags; add the package response to
   `ResponseSizeGuardTests`.
2. **Hard rule unchanged**: assessments (presentation fields included)
   never feed `tag_affinity` or `discover_games`.
3. New widget = new content-hashed `ui://` URI + a
   `scripts/preview_eval_card.py` twin of the game-cards preview script.
4. Unowned candidates with no appid and no IGDB match get a media-less
   card — every media block must be optional in the layout.

## Phases

1. ~~**Spike**~~ — done, see "Spike results" above.
2. **Data**: `data/media.py` (Steam + IGDB media, KV cache, caps) +
   `similar_games` fetch with library annotation.
3. **Server**: `record_assessment` presentation fields + `presentation`
   column (migration), package assembly in the response, response-size
   guard entries.
4. **Widget**: `apps_eval.py` evaluation card + preview script.
5. **Skill**: game-quality 3.0 — author-the-package instructions, grounding
   rules for for-you-if/comparisons, re-package via
   `scripts/package_skills.py`.
6. **Extensions** (each independent): review pull-quotes, ITAD price
   history, session-shape badges, franchise strip, demo link.
