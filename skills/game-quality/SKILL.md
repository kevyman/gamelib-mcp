---
name: game-quality
description: Evaluate whether a NAMED game is good and worth the user's time and money — owned or not. Triggers: "is X any good", "should I get X", "thoughts on X", "X vs Y", "is X worth playing", buy/wishlist/skip calls. NOT for picking a game for them ("what should I play", "I have 2 hours") — that's backlog-triage.
version: "2.4.0"
---

# Game Quality Assessment

Quality is three separate questions that must never be collapsed into one number prematurely:

1. **Craft** — is it well-made? (external consensus, sample-adjusted)
2. **Fit** — will *the user* like it? (taste profile, usually the strongest predictor)
3. **Context** — is it the right call *now*? (time, technical state, price, life constraints)

Always compute and report them separately, then give one verdict.

## Scope: this skill vs. backlog-triage

The dividing line is **evaluating a named game** vs. **choosing a game for tonight** — not owned vs. unowned.

- The user names a game and asks how good it is or whether it's worth it → **this skill**, whether or not they own it.
- The user asks what to play, what fits a session, or to be handed a pick from their library → **backlog-triage**.

This skill owns the *methodology* — craft scoring, fit/anchors, evidence standards — and backlog-triage calls into it for fit. Keep that layer intact and rigorous; it's the shared foundation.

Two handoffs:

- If the evaluation resolves to "they already own this, unplayed," say so and evaluate it honestly — but stop at the verdict. Do **not** slide into session-budget advice, ranked play-next lists, or shelving suggestions; that's backlog-triage's job. Offer the handoff instead.
- If they open with a named game but is really asking to be *given* something to play, switch to backlog-triage rather than forcing a single-candidate evaluation.

## Step 0: Gather data

All tools below live on the **Game Library** MCP.

1. **Web search** — Steam review data first: all-time positive %, all-time review count, recent positive %, recent count. SteamDB or the Steam store page are the sources of truth. Also search for any major recent events (big patch, monetization change, review bomb) if the recent/all-time numbers diverge. The server stores no Steam review *counts* (only a cached 1–9 enum/description survive enrichment), so this step is always web search when you want a real craft score.
2. **Assessment context** — one call replaces the old four-tool gather (search + detail + taste profile + anchor lookups + play history):
   ```
   get_assessment_context(
       name="...",  # or appid=..., or game_id=...; omit all three for an unowned/unreleased candidate
       tags=["Survival", "Open World", "Co-op", "Base Building", ...],  # Steam's display order — first 4 = core loop; omit to use the resolved game's own stored tags
       steam_positive_pct=88, steam_total_reviews=114479,
       steam_recent_positive_pct=89, steam_recent_total_reviews=3074,
       early_access=False,
   )
   ```
   At least one of identity (`name`/`appid`/`game_id`) or `tags` is required. This single pure-DB call returns:
   - `craft` — the sample-adjusted sentiment score (see Step 1), computed from the numbers you just web-searched.
   - `fit` — the candidate's tags crossed against the taste profile (see Step 2).
   - `anchors` / `anchor_count` / `anchors_truncated` — up to 8 owned, rated/played library games sharing the candidate's core tags (see Step 2).
   - `pace` — last-30-day play summary, for the Context step's time-shape judgment.
   - `past_assessments` / `past_assessment_count` / `past_assessments_truncated` — present only when this game was assessed before (see Step 4). **If it is present, this is a repeat ask: lead with the prior verdict, its date, and the price seen then, and answer what has CHANGED since** — a price move, patches, a shift in review trajectory, a new anchor they've since played. Do not re-derive the call blind and do not pretend it's the first time; the up-to-5 newest entries carry verdict, summary, fit_call, craft_adjusted, price_seen/price_currency, and target_price.
   - `game` / `game_resolution` — when identity resolves, a compact ownership block: owned platforms with playtime and acquisition (`price_paid`/`bundle_name`/`purchase_source`), `wishlisted`, `completion_status`, `play_state`, `my_rating`, HLTB main/extra hours. `game_resolution="not_found"` is normal for an unowned candidate — the other blocks still come back. Check that `game.name` is actually the candidate: a partial or fuzzy match can land on a sibling title. The `resolution` block says exactly how identity resolved — `mode` (`by_id` / `by_appid` / `by_assessed_appid` / `exact` / `partial` / `fuzzy` / `none`), the `query` used, and `matched_name`. **Whenever mode is not `exact` or `by_id`, diff `matched_name` against the candidate before using the `game` block**; if it's a different game, treat it as unowned and pass `name=` + `appid=` onward rather than that row's `game_id`. A sequel-shaped near miss is rejected for you: "Alan Wake 2" against a library "Alan Wake" (either direction) comes back `not_found` with `resolution.rejected_near_miss: "Alan Wake"` — if that row genuinely IS the candidate (a title they own under a different spelling), re-ask with `game_id`.
3. **Critic/technical detail** — `get_game_detail` (accepts `name`, `game_id`, or Steam `appid`) still, for what assessment context doesn't carry: OpenCritic score + percentile, Metacritic score, ProtonDB rating, the full tags list (if you didn't already pass them), and — for the DLC note in Step 3 — `related_content`, `parent_game_id`, `dlc_ownership`. No need to re-derive ownership, wishlist status, completion status, HLTB, or personal rating — assessment context already returned those.
4. For Switch 2 / non-Steam titles: substitute OpenCritic + Metacritic user score + reputable outlet consensus from `get_game_detail`; skip the Steam review web search and pass no review numbers to `get_assessment_context` (its `craft` block falls back to `source="server_cache"`, or stays absent).

Note: `discover_games` belongs to backlog-triage, not here — it answers "what should I play," which is out of scope. For a named candidate, go straight to `get_assessment_context` + `get_game_detail`.

If the game released after your knowledge cutoff or you don't recognize it, searching is mandatory — never assess from training memory.

## Step 1: Craft score (sample-adjusted player sentiment)

Raw Steam percentages lie at low sample sizes. `get_assessment_context`'s `craft` block already applies the adjustment — never compute it by hand:

`adjusted = p − (p − 0.5) × 2^(−log₁₀(n + 1))`

Read `craft.adjusted`, `craft.band`/`band_label`, `craft.insufficient_data` (n < 50), `craft.trajectory`, `craft.early_access_discount_applied`, and the ready-made `craft.formatted_line` straight from the response — passing `early_access=True` in Step 0 discounts the band one step automatically.

If you called without caller review numbers (unowned candidate, or the web search came up empty), `craft.source` is `"server_cache"` — the server only holds the cached 1–9 review-score enum/description, never counts, so no adjusted score exists; `craft.limitations` explains why and `craft.as_of` dates the cache. Treat that case like insufficient data: say so explicitly and lean entirely on Fit + demo.

Interpretation bands (adjusted score):

| Adjusted | Read |
|---|---|
| ≥ 0.92 | Elite — top tier of all of Steam |
| 0.85–0.92 | Excellent |
| 0.78–0.85 | Very good |
| 0.70–0.78 | Good but divisive — read *why* the negatives exist |
| < 0.70 | Caution — negatives are usually structural, not taste |
| n < 50 | Insufficient data — say so explicitly; lean entirely on Fit + demo |

**Trajectory** (`craft.trajectory`, computed by the tool): improving (recent ≥ +5pp) means weight recent higher; REGRESSING (recent ≤ −7pp) means search for the cause — bad update, monetization change, or review bomb — before trusting the all-time number. Distinguish genuine regression from off-topic review bombing.

**Genre calibration**: Steam scores are not comparable across genres. Cozy games, visual novels, and roguelites cluster high (90%+ is normal); strategy, MMOs, sports, and early-access survival games run structurally lower. Judge a game against its genre's distribution, not the global scale. State this when relevant ("84% is mid for a roguelite but strong for an RTS").

**Critic blend**: when OpenCritic data exists (from `get_game_detail`, Step 0), report Top Critic Average and the percentile. Weight critics *lower* for indie/niche titles (sparse, lottery-like coverage — absence of reviews ≠ bad game) and for games that improved heavily post-launch (critics scored v1.0). Weight them *higher* for narrative-driven games. Never use review scores from a single outlet as consensus.

## Step 2: Fit score (the user-specific layer)

This usually matters more than craft. A 95% game in a genre the user bounces off is a worse buy than an 80% game in their wheelhouse.

1. Read the `fit` block from Step 0's `get_assessment_context` call: `matched_top_tags`/`matched_bottom_tags` (with affinities), `top_coverage`, `core_gap` (the candidate's first 4 tags are mostly absent from the taste data), `tag_affinities` (raw per-tag rows for every candidate tag, including ones outside the top/bottom lists), and `suggested_call`.
2. `suggested_call` (strong fit / probable fit / coin flip / probable miss) is a starting point, not the answer. Anchors decide:
3. The `anchors` block already returns up to 8 owned/primary/non-farmed games sharing the candidate's core tags (most shared core tags first, then rated, then most played) — `anchor_count` is the true total, `anchors_truncated` flags the cap. Each anchor carries `matched_core_tags`, `rating`, `playtime_hours`, and `completion_status`. The user's reaction to anchors is the single best predictor. Owned-but-unplayed entries in the same genre count as *negative* anchor evidence. Completion statuses sharpen this: an **abandoned** anchor is the strongest negative evidence there is (they tried the loop and quit), while **evergreen** or **completed** anchors are the strongest positives — name the status when citing an anchor. Need a specific known title as an extra anchor beyond the tag-based set? `search_games(queries=[...])` resolves it — but discard any result tagged `match_type:"fuzzy"` unless the title you searched for is plainly among them (a fuzzy tag on a known-title lookup is usually token/substring noise, e.g. querying "Ys" returns "7 Days to Die", "Abyss Odyssey"; still useful for genuine misspellings, so don't discard it globally — only when the real game isn't in the results). **For sequels or series entries, the user's history with the franchise dominates everything else** — use `get_stats(report="series")` (or `search_games(query="", series="<Series Name>")` / `get_library_stats(series=["<Series Name>"])`) to pull every owned entry in that series with playtime, so you can see whether they engage with the franchise or hoard it unplayed.
4. Report the final call with the anchors named as evidence. Don't fake numeric precision here.

Standing priors: strong preference for single-player, story-rich, indie adventure; roguelite deckbuilders proven (Slay the Spire 244h); multiplayer-only and live-service games are a hard fit penalty unless the user says otherwise.

## Step 3: Context modifiers

These gate the verdict; they don't change the quality scores.

- **Time shape**: HLTB main / main+extra vs reality — limited, mostly evening-length play sessions. Ground the pace in Step 0's `pace` block (from `get_assessment_context`; `get_play_history` is the drill-down for longer windows or per-game detail), don't assume it: the user's actual weekly minutes tell you how long an HLTB estimate really takes *them*. An 80-hour epic needs save-anywhere and good session granularity to be recommendable *now*. A 9/10 game can still get "wishlist, wrong season of life." Shorter is currently better.
- **Active-game competition**: if `get_play_history` shows a game in active rotation, a new buy competes with it. "Finish X first" is a legitimate verdict component; a purchase that would sit until the active game wraps is a wishlist, not a buy.
- **Technical state**: ProtonDB rating if relevant; scan recent reviews for performance complaints on PC.
- **Early Access**: discount craft score one band; check update cadence and the developer's completion track record (abandonment risk).
- **Price/value**: prices in euros. If the game is **wishlisted** (`get_game_detail` → `wishlisted: true`), `get_wishlist(with_prices=True)` gives the live best price, cut %, and a cross-platform recommendation honoring their hardware preference (check `alternatives` + `recommendation_reason` — sometimes another platform's deal is decisively cheaper). If **not wishlisted**, web-search the current price and historical low (ITAD/DekuDeals). Flag if it's frequently discounted (most games hit −50% within 18 months) — "wishlist for sale" is a legitimate verdict.
- **Cost-per-hour: still banned per-candidate.** It structurally rewards long games (opposite of the current priority) and HLTB hours ≠ hours they'll actually play. But `get_stats(report="spending")` data IS usable as *descriptive evidence*: cite `unplayed_spend` ("€X already spent on unplayed games") and purchase-pattern history (which sources/price points the user actually plays vs. shelves) when they bear on the verdict. Evidence, never a score.
- **Backlog pressure**: The user is a heavy collector with a large unplayed pile. Don't guess the count — `get_stats(report="backlog")` gives current completion %, weekly pace, years-to-clear, **and an `unplayed_spend` block** for a grounded "do you need another game right now" read. Before recommending a buy, check for a near-identical game they already own unplayed: `get_library_stats` with `tags`/`genres`/`series` filters + `filter=unplayed` (e.g. `genres=["RPG"], max_hltb_hours=15, filter=unplayed`) is faster and more complete than guessing comparison titles for `search_games(queries=[...])`. If a close substitute is already sitting unplayed, "play what you own instead: X" is the verdict.
- **DLC / editions / upgrades**: when the candidate is nested content, evaluate it against the **base game's** engagement, not in isolation. `get_game_detail` gives the `parent_game_id` link, `related_content` (owned siblings with prices), and `dlc_ownership` (owned vs. known catalog). High base-game playtime + loved rating → DLC is usually the best €-per-joy buy available; base game unplayed → hard skip regardless of DLC reviews.

## Step 4: Verdict output

ALWAYS use this exact structure, in prose-light form:

```
**[Game]** — [one-line verdict]

Craft: [adjusted score as %] (n=[reviews], recent [trend]) · OpenCritic [x] ([percentile]) [if available]
Fit: [strong/probable/coin flip/miss] — anchors: [Game A (their rating/hours)], [Game B]
Time: [HLTB main / main+extra] · [session-friendliness note]
Price: [current best €X (−Y%) on [platform] / full price €X] · [historical low or "rarely discounted" note]
Flags: [red flags, or "none"]

Verdict: [Buy now / Wishlist for sale / Try the demo / Skip / Play what you own instead: X]
```

Confidence statement is mandatory when data is thin (low review count, no anchors, pre-release).

When the verdict is **Wishlist for sale**, offer to promote it onto the internal wishlist in the same conversation — it's a library write; say so, don't do it silently. Skip the offer entirely when Step 0 already reported `wishlisted: true` — the row exists, and a re-write wouldn't change its provenance anyway (an existing row keeps its stored source; the server never lets a hand write relabel one). One confirmation writes: `add_game_to_platform(name=... or game_id=..., platform=..., owned=False, wishlist_source="assessment")`. Platform: steam for a PC candidate, switch2 when the Switch version is the recommendation (their stored hardware preference — the same signal the price/value step above reads via `recommendation_reason`), ps5 for a PSN-only title. **On `platform="steam"` only**, also pass `identifier_type="steam_appid", identifier_value=<appid>` whenever Step 0 surfaced one — it makes the row immediately priceable via ITAD and resolvable by future syncs; the tool rejects a steam appid on any other platform, so a switch2/ps5 promotion goes without identifiers. Use `wishlist_source="assessment"`, never plain manual — promoted rows stay distinct from hand-curated entries, are bulk-removable by source, and sit outside reconciliation's reach. Steam only, and only after they've confirmed the promotion itself: offer a second, separate confirmation for `push_to_store=True` on the same call, which additionally pushes the add to their real Steam wishlist for store-side sale notifications — it needs a known appid, and a failed push still records the local row. From there it's a price-watched entry: `get_wishlist(with_prices=True)` covers it like any other.

**Always record the verdict.** After delivering the verdict block (and after making the wishlist-promotion offer above, if any — recording never waits on their answer to it), call `record_assessment` once, mapping the lines you just wrote to its components:

```
record_assessment(
    game_id=...,            # PREFER this — Step 0 returns game.game_id whenever the candidate resolved
    verdict="wishlist_for_sale",   # buy_now | wishlist_for_sale | try_demo | skip | play_what_you_own
    summary="<the one-line verdict>",
    craft_adjusted=0.87, craft_positive_pct=88, review_count=114479,   # from the craft block
    recent_trajectory="stable",    # improving | stable | regressing
    opencritic_score=85,
    fit_call="probable fit",       # the same four strings the fit block uses
    anchors_cited=[{"name": "Hollow Knight", "game_id": 412}, ...],    # game_ids from Step 0's anchors
    flags=["live service", "80h main story"],
    price_seen=29.99, price_currency="EUR", price_platform="steam",
    target_price=19.99,            # whenever "Wishlist for sale" names a threshold
    instead_game_id=...,           # for "Play what you own instead: X"
    context="bundle: Humble Choice 2026-08",   # when the assessment came out of a bundle/sale context
    skill="game-quality",
    skill_version="<this file's frontmatter version — read it above, don't hardcode it>",
    model="<the model id YOUR environment declares — see below>",
)
```

**Provenance is declared, never guessed.** `skill_version` is whatever the `version:` field at the top of *this* file says — read it there, since an installed copy may lag the server's. `model` is the identifier your environment states about itself: Claude Code names the exact model id in its system prompt; claude.ai names its model there too; ChatGPT declares a model FAMILY — record the family or the picker selection, never a router variant (fast/thinking) you cannot actually see. Copy it verbatim, lowercased. If your environment declares no model at all — some configurations withhold it — **omit the field**: never answer from training memory, and never infer it. The server stamps nothing, so a missing value stays NULL, which honestly means "unknown"; a confident wrong value silently corrupts `get_stats(report="calibration")`'s `by_model`.

**Identity: pass `game_id` whenever Step 0 resolved the candidate** (`game.game_id`, and only when `resolution.matched_name` really is the candidate). Pass `name=` — plus `appid=` when you have one — only for a candidate Step 0 could NOT resolve. Unlike Step 0's lookup, `name` here matches exactly or MINTS a new row: a name miss minting a row is correct for an unowned candidate, and a typo makes a visible phantom row (repairable with `merge_games`) rather than silently filing the verdict onto a sibling title.

Then check the response: `resolution.matched_name` is the row that was written to. If it isn't the candidate, repair it — `record_assessment(void_assessment_id=<assessment_id>)` hard-deletes the misfiled row (it's exclusive: nothing else in that call), then re-record with the right `game_id`. Voiding is also the fix for any assessment recorded on a past day that shouldn't stand; same-day mistakes need no void, since re-recording replaces that day's entry.

This is silent bookkeeping — one line ("logged for calibration"), never a re-explanation of the verdict. Re-recording the same game on the same day replaces that day's entry, so refining a call mid-conversation is safe. The recorded verdict feeds nothing but future context and `get_stats(report="calibration")`; it never touches the wishlist, the taste profile, or recommendations.

If the user already owns the game, the Price line reports what they paid from the acquisition data (`already owned — paid €X in [bundle] via [source]`, or "already owned — price unrecorded") and the verdict answers *is this worth your time*, not *play this tonight*. Never treat what they paid as a reason to play it; sunk cost is not an argument.

## Anti-patterns (never do these)

- Quoting a raw Steam % without the review count or adjustment.
- Averaging Steam %, OpenCritic, and fit into one blended number — report components, verdict in words.
- Treating hype signals (wishlists, followers, streamer volume) as quality evidence.
- Treating missing critic coverage as a negative for an indie.
- Assessing an unfamiliar or post-cutoff game from memory instead of searching.
- Recommending a long-session game without flagging the session-shape problem.
- Computing per-candidate cost-per-hour, or treating HLTB hours as hours they'll actually play.
- Web-searching a price for a wishlisted game when `get_wishlist(with_prices=True)` already has the live deal and platform recommendation.
- Assuming the user's play pace instead of reading it from `get_play_history`.
- Drifting into backlog-triage's territory: session-budget advice, ranked "play this next" lists, or shelving suggestions. Evaluate the game, give the verdict, offer the handoff.
- Treating what they already paid as a reason to play a game.
