---
name: bundle-evaluation
description: Decide whether John should buy a game BUNDLE — Humble Bundle/Choice, Fanatical build-your-own, platform-sale multi-game sets, any list of games with one price. Triggers: "is this bundle worth it", "should I grab this month's Humble Choice", a pasted game list + price, "worth it for X alone?". NOT for one named game (game-quality) and NOT for picking what to play (backlog-triage).
version: "1.0.0"
---

# Bundle Evaluation

A bundle is worth **what you'd pay for the games you actually want in it — nothing else**. Games he already owns contribute €0. Filler he'll never launch contributes €0, whatever its Steam rating. The publisher's "€466 value!" MSRP-sum is price anchoring and never enters the math. The honest comparison is:

> bundle price **vs.** the sum of realistic individual prices (historical lows) of the *wanted subset only*.

Everything below exists to compute those two numbers honestly and turn them into one verdict: **Buy / Buy at a lower tier / Buy only if you want X and Y / Skip**.

## Scope: this skill vs. its siblings

- A **set** of games with one price → this skill.
- One named game ("is Hades II any good?") → **game-quality**. This skill calls into game-quality's methodology per shortlisted constituent; the craft/fit/anchor layer lives there and is not duplicated here.
- "Which of these should I play first?" after buying → **backlog-triage**. Never answer that here.

## Step 0: Establish contents, price, and structure

Get a concrete constituent list and price(s). If John pasted the list, use it verbatim (raw store SKU titles are fine — see Step 6). If he gave only a bundle name or URL, web-search the contents; IsThereAnyDeal's bundle pages and barter.vg are the best sources because they also show **per-game bundled-count and recency** ("14× bundled — last time 7 months ago"), which Step 4 uses.

Note the structure while you're there:

- **Tiers / build-your-own**: record the per-tier prices. The question is never "is tier 2 worth it" but "is each *additional* game worth its marginal cost" (Step 4).
- **Key platform**: where do the keys land (Steam / GOG / DRM-free)? A Steam-key bundle is worth less for a game he'd rather own on another platform per his stored hardware preference.
- **Humble Choice specifics**: skipping a month must be actioned before the last Tuesday; claimed games are kept forever even after cancelling; skipping forfeits that month's Vault access. State these as context when the ask is a Choice month.
- **Is he already subscribed?** Check before anything else — Step 1 gives it away, since claimed Choice games land in the library with `purchase_source: "subscription"` and a per-game share as their `price_paid`. If this month's games are already owned that way, the bundle is *already bought*: the real question is whether to keep the subscription running (pause/skip next month), and the honest per-game cost is the subscription share, not the sticker price. Answer that question instead of a buy/skip he's already past.

## Step 1: Ownership screen

One batch call on the **Game Library** MCP resolves the whole list:

```
search_games(queries=[...all constituent titles...], limit_per_query=3)
```

Each hit carries `owned`, `wishlisted`, and the full `platforms` array (with `price_paid`/`bundle_name` acquisition data). Read them per title:

- **Owned** → marginal value €0, out of consideration. If acquisition data exists, note what he paid ("already owns 4 of 8, paid €12 for three of them in past bundles") — it's good verdict framing.
- **Wishlisted** → a pre-declared want. These jump straight onto the Step 2 shortlist *and* get live pricing in Step 4.
- **No result** → **not proof of non-ownership.** Three caveats, all real:
  1. Batch mode skips the alias fallback that single-query mode runs — re-check any miss that matters with a single `search_games(query="...")`.
  2. Search does not strip edition/SKU suffixes ("Ultimate", "GOTY", "ROW", "Steam Key", "Complete"). For a suffixed bundle title, also query the stripped base name before concluding he doesn't own it.
  3. A `match_type: "fuzzy"` hit follows game-quality's rule: trust it only if the queried title is plainly among the results.
- **DLC/edition constituents** resolve via the nested-content fallback — a DLC whose base game he doesn't own is filler by default (game-quality's DLC rule: base unplayed → hard skip, base loved → often the best €-per-joy in the bundle).

## Step 2: Triage to the wanted shortlist

Do **not** run a full assessment on every constituent — a 12-game bundle would cost 12+ tool calls to mostly conclude "filler". Triage first, cheaply:

1. `get_stats(report="taste")` once — the high/low-affinity tags.
2. **Get tags for the unowned constituents — the library cannot give you them.** Search results carry review and critic signal (`steam_review_desc`, `metacritic_score`, `opencritic_score`, `hltb_main`) but *no tags at all*, and an unowned game returns no row in the first place. So the tags this step compares against the profile have to come from outside: the bundle/store page usually lists genre per title, and an ITAD bundle page gives Steam tags for every constituent in one fetch — that single page covers the whole bundle and is the cheapest source. Do this before assigning tiers rather than guessing from the title, or unfamiliar games get dumped into filler because you didn't recognise them.
3. Cross those tags plus the review signal against the profile and the standing priors (single-player story-rich indie ↑, roguelite deckbuilders proven, multiplayer-only/live-service hard penalty).
4. Sort every constituent into three want-tiers:
   - **Must-have** — he'd plausibly buy it on its own. Full assessment in Step 3.
   - **Nice** — would play if it showed up, wouldn't seek out. Counts at a heavy discount in Step 4; assess fully only if the verdict turns on it.
   - **Filler** — wrong genre, live-service, DLC without the base, shovelware. Worth **€0**, never "might play someday". At 2,800 mostly-unplayed games, an unwanted free game is a backlog tax, not upside.

Expect the shortlist to be small: 0–5 games. If it's 0, the verdict is already Skip — go straight to Step 5.

## Step 3: Assess the shortlist (game-quality methodology, capped)

For each must-have (and any verdict-deciding nice-tier game), run game-quality's core loop:

- Web-search Steam review numbers, then `get_assessment_context(name=..., steam_positive_pct=..., steam_total_reviews=..., ...)` for sample-adjusted craft, fit, and anchors. `get_game_detail` for OpenCritic/ProtonDB/DLC links where needed.
- **Never call `get_assessment_context` for all N constituents** — shortlist only. It has no batch mode, and per-filler assessment is exactly the effort explosion the triage exists to prevent.
- Check for an unplayed near-substitute he already owns (`get_library_stats` with `tags`/`genres` + `filter=unplayed`): a bundle headliner duplicating something already shelved unplayed is a demotion to nice-tier at best.
- A game released after your knowledge cutoff or unknown to you gets searched, never assessed from memory.

Anything that comes out "probable miss" or worse drops to filler. The survivors are the **wanted subset**.

## Step 4: Value math

Price the wanted subset per game — what would acquiring *this game, the way he'd actually want it* really cost outside the bundle?

- **Wishlisted games**: `get_wishlist(with_prices=True)` — live best price, cut %, and the cross-platform recommendation. Respect `recommendation_reason`: the hardware-preference order is stored server-side (`set_hardware_preference`), not a rule to hardcode, and a preferred-platform version is only overridden when another platform is dramatically cheaper. If the bundle's keys land on a platform the recommendation argues *against*, that game's contribution shrinks — a Steam key is not a Switch 2 copy.
- **Unwishlisted wanted games**: web-search current price and historical low (ITAD / gg.deals / DekuDeals). If a game is wanted *regardless of this bundle*, offering to wishlist it first (`add_game_to_platform(..., owned=False, identifier_type="steam_appid", identifier_value=...)`) is legitimate — it's a real want-to-play signal and gets live ITAD pricing — but it is a library write; say so, don't do it silently.
- **No ITAD key configured**: `get_wishlist(with_prices=True)` returns `"itad": "unconfigured"` and Steam titles land in `unpriced`. Degrade to web-searched prices and say the comparison is web-sourced; the methodology still works.
- **Use historical lows, not list prices**, as each game's realistic cost: most games hit −50% within 18 months. **Say which store the low came from** — gg.deals and ITAD both mix official stores with grey-market keyshops (Yuplay, Instant Gaming, Kinguin), and a keyshop low is often a fraction of the official one. Compare like with like: if the bundle sells official Steam keys, an official-store low is the honest comparison, with the keyshop price mentioned as a caveat, not as the headline. Then apply the **bundled-before decay**: a game bundled many times recently will be this cheap again soon (skipping is cheap); a never-bundled recent title at a first-ever discount is genuinely scarce (skipping is expensive). Cite the count/recency from Step 0's sources when it moves the verdict.
- **Tiered/BYO bundles**: walk tiers marginally. Tier N+1 pays off only if the games it adds are themselves wanted at that marginal price — "Buy at lower tier" is a first-class verdict.
- **His own track record as evidence.** Two different sources, and it matters which says what. `get_stats(report="spending")` → `by_bundle` is **spend-only** — bundle name, currency, total spent, constituent count. It does *not* carry playtime, so it can never answer "did he play the last one"; treat it as "how much has gone into bundles" and nothing more. The play side comes free from Step 1: the ownership screen already returned `playtime_hours` per owned constituent, so "he owns 6 of these 7 and has played 2 hours across all of them" is a fact you already hold. That is the sharpest evidence in the whole evaluation — lead with it. `report="backlog"` → `unplayed_spend` grounds "does he need 8 more games". All descriptive evidence, never a score — and per-game cost-per-hour stays banned exactly as in game-quality.

## Step 5: Verdict output

ALWAYS this structure:

```
**[Bundle] — €[price]** ([N] games: [n_owned] owned, [n_wanted] wanted, [n_filler] filler)

| Game | Owned? | Want | Why (one line) | Marginal value |
|---|---|---|---|---|
| Headliner | no | must-have | strong fit — anchors: X, Y · hist. low €15 | €15 |
| Game B | Steam (2019, €4) | — | already owned | €0 |
| Game C | no | filler | live-service, hard fit penalty | €0 |
...

Wanted subset: [games] ≈ €[sum] vs. bundle €[price]
[bundled-before / platform-mismatch / tier notes, one or two lines]

Verdict: [Buy / Buy at €X tier / Buy only if you want [X] and [Y] / Skip]
```

Apply the buy trigger honestly: **≥2 wanted games whose combined realistic price clears the bundle price, or 1 wanted game whose realistic price alone does.** "Worth it for X alone" is only true when `bundle price ≤ historical low of X` — it rarely is; say so when it isn't. Confidence statement is mandatory when the shortlist assessment ran on thin data.

If the verdict is Skip but a constituent is genuinely wanted, close the loop: offer to wishlist it so the deals machinery watches its price instead.

## Step 6: Purchase handoff

If he buys it, the evaluation's constituent list becomes the acquisition record — same list, no re-derivation:

- **Importable source** (Humble, Steam, Epic, Nintendo eShop with a stored session): `import_purchases(sources=[...])` — multi-game bundles land in `bundles_needing_split`, then `split_bundle_acquisition(dry_run=True)` from that entry.
- **Humble Choice is the exception, and it's the most common case here.** A Choice month is imported as `subscriptioncontent`: the importer splits the month's price across the games itself and writes them with `purchase_source: "subscription"` and **no** `bundle_name`, so nothing lands in `bundles_needing_split` and there is no split left to run. `import_purchases` alone *is* the completed handoff — don't go hunting for a missing entry or hand-build one. You can confirm it worked straight from Step 1: the month's games come back owned, with a per-game share as `price_paid` and `purchase_source: "subscription"`. That same signature is how you spot an already-claimed month before evaluating one.
- **Anything else**: `split_bundle_acquisition(bundle_name=..., platform=..., total_price=..., games=[...the evaluated constituents...], dry_run=True)` directly. Raw store SKU titles are safe here — the acquisition matcher strips edition/SKU suffixes itself and never fuzzy-matches.
- Price allocation is an **even split** unless per-game `price_paid` is passed explicitly; if the evaluation's per-game values matter for spending stats, pass them.
- Always `dry_run=True` first, and doubly so with `create_missing=True` — created rows have no delete tool. Review `unmatched` before applying.
- Wishlist entries for now-owned constituents clear automatically on the next sync/refresh; no manual cleanup.

## Anti-patterns (never do these)

- Quoting the publisher's MSRP-sum "value", or comparing the bundle price against anything but the wanted subset.
- Counting owned games or filler as value — "12 games for €10" is not an argument; "2 games he wants for €10" is.
- Running `get_assessment_context` (or full game-quality) on every constituent instead of triaging first.
- Treating a batch `search_games` miss as proof of non-ownership without the single-query/stripped-title re-check.
- Hardcoding "Switch 2 over Steam" — the preference order and override threshold live server-side; read `recommendation_reason`.
- Cost-per-hour math, per game or per bundle. HLTB-hours-per-euro is the marketing version of it.
- Assessing an unfamiliar or post-cutoff constituent from training memory instead of searching.
- Wishlisting constituents silently to get ITAD prices — it's a library write; offer it.
- Recording the purchase with a different constituent list than the one evaluated.
- Sliding into backlog-triage ("play Game B first") — hand off instead.
