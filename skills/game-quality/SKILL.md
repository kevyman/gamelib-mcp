---
name: game-quality
description: Evaluate whether a NAMED game is good and worth John's time and money — owned or not. Triggers: "is X any good", "should I get X", "thoughts on X", "X vs Y", "is X worth playing", buy/wishlist/skip calls. NOT for picking a game for him ("what should I play", "I have 2 hours") — that's backlog-triage.
version: "2.1.0"
---

# Game Quality Assessment

Quality is three separate questions that must never be collapsed into one number prematurely:

1. **Craft** — is it well-made? (external consensus, sample-adjusted)
2. **Fit** — will *John* like it? (taste profile, usually the strongest predictor)
3. **Context** — is it the right call *now*? (time, technical state, price, life constraints)

Always compute and report them separately, then give one verdict.

## Scope: this skill vs. backlog-triage

The dividing line is **evaluating a named game** vs. **choosing a game for tonight** — not owned vs. unowned.

- John names a game and asks how good it is or whether it's worth it → **this skill**, whether or not he owns it.
- John asks what to play, what fits a session, or to be handed a pick from his library → **backlog-triage**.

This skill owns the *methodology* — craft scoring, fit/anchors, evidence standards — and backlog-triage calls into it for fit. Keep that layer intact and rigorous; it's the shared foundation.

Two handoffs:

- If the evaluation resolves to "he already owns this, unplayed," say so and evaluate it honestly — but stop at the verdict. Do **not** slide into session-budget advice, ranked play-next lists, or shelving suggestions; that's backlog-triage's job. Offer the handoff instead.
- If he opens with a named game but is really asking to be *given* something to play, switch to backlog-triage rather than forcing a single-candidate evaluation.

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
   - `game` / `game_resolution` — when identity resolves, a compact ownership block: owned platforms with playtime and acquisition (`price_paid`/`bundle_name`/`purchase_source`), `wishlisted`, `completion_status`, `play_state`, `my_rating`, HLTB main/extra hours. `game_resolution="not_found"` is normal for an unowned candidate — the other blocks still come back. Check that `game.name` is actually the candidate: a fuzzy match can land on a sibling title.
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

## Step 2: Fit score (the John-specific layer)

This usually matters more than craft. A 95% game in a genre he bounces off is a worse buy than an 80% game in his wheelhouse.

1. Read the `fit` block from Step 0's `get_assessment_context` call: `matched_top_tags`/`matched_bottom_tags` (with affinities), `top_coverage`, `core_gap` (the candidate's first 4 tags are mostly absent from the taste data), `tag_affinities` (raw per-tag rows for every candidate tag, including ones outside the top/bottom lists), and `suggested_call`.
2. `suggested_call` (strong fit / probable fit / coin flip / probable miss) is a starting point, not the answer. Anchors decide:
3. The `anchors` block already returns up to 8 owned/primary/non-farmed games sharing the candidate's core tags (most shared core tags first, then rated, then most played) — `anchor_count` is the true total, `anchors_truncated` flags the cap. Each anchor carries `matched_core_tags`, `rating`, `playtime_hours`, and `completion_status`. His reaction to anchors is the single best predictor. Owned-but-unplayed entries in the same genre count as *negative* anchor evidence. Completion statuses sharpen this: an **abandoned** anchor is the strongest negative evidence there is (he tried the loop and quit), while **evergreen** or **completed** anchors are the strongest positives — name the status when citing an anchor. Need a specific known title as an extra anchor beyond the tag-based set? `search_games(queries=[...])` resolves it — but discard any result tagged `match_type:"fuzzy"` unless the title you searched for is plainly among them (a fuzzy tag on a known-title lookup is usually token/substring noise, e.g. querying "Ys" returns "7 Days to Die", "Abyss Odyssey"; still useful for genuine misspellings, so don't discard it globally — only when the real game isn't in the results). **For sequels or series entries, his history with the franchise dominates everything else** — use `get_stats(report="series")` (or `search_games(query="", series="<Series Name>")` / `get_library_stats(series=["<Series Name>"])`) to pull every owned entry in that series with playtime, so you can see whether he engages with the franchise or hoards it unplayed.
4. Report the final call with the anchors named as evidence. Don't fake numeric precision here.

Standing priors: strong preference for single-player, story-rich, indie adventure; roguelite deckbuilders proven (Slay the Spire 244h); multiplayer-only and live-service games are a hard fit penalty unless he says otherwise.

## Step 3: Context modifiers

These gate the verdict; they don't change the quality scores.

- **Time shape**: HLTB main / main+extra vs reality — toddler, second kid incoming, ~evening-session play. Ground the pace in Step 0's `pace` block (from `get_assessment_context`; `get_play_history` is the drill-down for longer windows or per-game detail), don't assume it: his actual weekly minutes tell you how long an HLTB estimate really takes *him*. An 80-hour epic needs save-anywhere and good session granularity to be recommendable *now*. A 9/10 game can still get "wishlist, wrong season of life." Shorter is currently better.
- **Active-game competition**: if `get_play_history` shows a game in active rotation, a new buy competes with it. "Finish X first" is a legitimate verdict component; a purchase that would sit until the active game wraps is a wishlist, not a buy.
- **Technical state**: ProtonDB rating if relevant; scan recent reviews for performance complaints on PC.
- **Early Access**: discount craft score one band; check update cadence and the developer's completion track record (abandonment risk).
- **Price/value**: prices in euros. If the game is **wishlisted** (`get_game_detail` → `wishlisted: true`), `get_wishlist(with_prices=True)` gives the live best price, cut %, and a cross-platform recommendation honoring his hardware preference (check `alternatives` + `recommendation_reason` — sometimes another platform's deal is decisively cheaper). If **not wishlisted**, web-search the current price and historical low (ITAD/DekuDeals). Flag if it's frequently discounted (most games hit −50% within 18 months) — "wishlist for sale" is a legitimate verdict.
- **Cost-per-hour: still banned per-candidate.** It structurally rewards long games (opposite of the current priority) and HLTB hours ≠ hours he'll actually play. But `get_stats(report="spending")` data IS usable as *descriptive evidence*: cite `unplayed_spend` ("€X already spent on unplayed games") and purchase-pattern history (which sources/price points he actually plays vs. shelves) when they bear on the verdict. Evidence, never a score.
- **Backlog pressure**: John's a heavy collector with a large unplayed pile. Don't guess the count — `get_stats(report="backlog")` gives current completion %, weekly pace, years-to-clear, **and an `unplayed_spend` block** for a grounded "do you need another game right now" read. Before recommending a buy, check for a near-identical game he already owns unplayed: `get_library_stats` with `tags`/`genres`/`series` filters + `filter=unplayed` (e.g. `genres=["RPG"], max_hltb_hours=15, filter=unplayed`) is faster and more complete than guessing comparison titles for `search_games(queries=[...])`. If a close substitute is already sitting unplayed, "play what you own instead: X" is the verdict.
- **DLC / editions / upgrades**: when the candidate is nested content, evaluate it against the **base game's** engagement, not in isolation. `get_game_detail` gives the `parent_game_id` link, `related_content` (owned siblings with prices), and `dlc_ownership` (owned vs. known catalog). High base-game playtime + loved rating → DLC is usually the best €-per-joy buy available; base game unplayed → hard skip regardless of DLC reviews.

## Step 4: Verdict output

ALWAYS use this exact structure, in prose-light form:

```
**[Game]** — [one-line verdict]

Craft: [adjusted score as %] (n=[reviews], recent [trend]) · OpenCritic [x] ([percentile]) [if available]
Fit: [strong/probable/coin flip/miss] — anchors: [Game A (his rating/hours)], [Game B]
Time: [HLTB main / main+extra] · [session-friendliness note]
Price: [current best €X (−Y%) on [platform] / full price €X] · [historical low or "rarely discounted" note]
Flags: [red flags, or "none"]

Verdict: [Buy now / Wishlist for sale / Try the demo / Skip / Play what you own instead: X]
```

Confidence statement is mandatory when data is thin (low review count, no anchors, pre-release).

If he already owns the game, the Price line reports what he paid from the acquisition data (`already owned — paid €X in [bundle] via [source]`, or "already owned — price unrecorded") and the verdict answers *is this worth your time*, not *play this tonight*. Never treat what he paid as a reason to play it; sunk cost is not an argument.

## Anti-patterns (never do these)

- Quoting a raw Steam % without the review count or adjustment.
- Averaging Steam %, OpenCritic, and fit into one blended number — report components, verdict in words.
- Treating hype signals (wishlists, followers, streamer volume) as quality evidence.
- Treating missing critic coverage as a negative for an indie.
- Assessing an unfamiliar or post-cutoff game from memory instead of searching.
- Recommending a long-session game without flagging the session-shape problem.
- Computing per-candidate cost-per-hour, or treating HLTB hours as hours he'll actually play.
- Web-searching a price for a wishlisted game when `get_wishlist(with_prices=True)` already has the live deal and platform recommendation.
- Assuming his play pace instead of reading it from `get_play_history`.
- Drifting into backlog-triage's territory: session-budget advice, ranked "play this next" lists, or shelving suggestions. Evaluate the game, give the verdict, offer the handoff.
- Treating what he already paid as a reason to play a game.
