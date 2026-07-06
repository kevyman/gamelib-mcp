# Architecture Review & Roadmap v2 — 2026-07-06

Scope: critical "what's actually wrong and what should be built next" review.
Successor to `2026-07-01-repo-audit-and-roadmap.md`, whose roadmap (items 1–7)
is now **fully executed**: CI/lint/types, backups, LICENSE, OAuth, wishlist
deals, completion status, series gaps, play history, Xbox sync, single-user
ADR. Zero open issues, zero TODOs in source. This doc is the next roadmap.

## Overall verdict

The architecture is in unusually good shape for a personal project: the
dependency DAG is clean, invariants are written down where they're enforced,
the scraper layer is self-healing, and the previous audit's process gaps are
closed. The honest criticism is no longer "what's broken" but two things:

1. **The recommender flies blind.** Scoring v2 (#70) shipped a materially more
   sophisticated model (mean-centered affinity, IDF weighting, playtime
   pseudo-ratings, shrinkage priors) with **no way to measure whether it's
   better**. Quality judgment is currently "eyeball prod output" (per the
   dev-DB-has-no-ratings note). Every future scoring change compounds this:
   tweaks are unverifiable, regressions invisible.

2. **The system answers "what should I play?" but not the question it's now
   positioned to answer: "what should I buy?"** All the ingredients exist —
   prices (ITAD/DekuDeals), taste (tag affinity), length (HLTB), critic
   scores, backlog pressure, hardware preference — but `get_wishlist_deals`
   ranks purely by price. The taste model and the deals pipeline have never
   been joined.

Secondary observations (not roadmap drivers): `data/db/__init__.py` (1,936
lines), `igdb.py` (1,427), `tools/admin.py` (1,208) are large but cohesive —
split opportunistically when touched, not proactively. Backups run nightly but
a restore has never been drilled. PSN remains manual-only; that's an external
API limitation, not a gap you can close.

---

## Roadmap — ordered by value-for-effort

### 1. Recommender eval harness (do first — makes everything after it safe)

**Problem.** `discover_games` match ranking has real tuning surface
(`_MATCH_PRIOR = 3.0`, playtime pseudo-rating weight 0.3, cap 9.5, shrinkage
constant 2.0, `VIBE_TAG_PROMINENCE_CUTOFF = 8`, `MERGED_TAG_CAP`) and no
measurement. These constants were picked by feel; nobody knows if they're
right, and nobody will know if a future change makes them wrong.

**Design.** Offline leave-one-out evaluation over the ratings table, run
against a **prod DB snapshot** (the nightly CLOSET backups make this free):

- `scripts/eval_recsys.py --db <snapshot>`:
  - For each rated game: recompute tag affinity with that rating held out
    (reuse `recompute_tag_affinity` against a temp copy, or an in-memory
    variant that accepts an exclusion), score the held-out game with the
    same `_MATCH_SCORE_SQL`, record (predicted_score, actual_rating).
  - Report: Spearman rank correlation; precision@10 for "loved" (rating ≥ 8);
    mean predicted score of ratings ≤ 4 vs ≥ 8 (separation).
  - A `--baseline` mode that scores with affinity weights zeroed (popularity/
    critic ordering) so improvements are measured against something.
- Store each run's metrics as JSON in `docs/audit/recsys-evals/` so scoring
  PRs can paste before/after numbers.
- Non-goals: no online A/B, no interaction logging (single user; the ratings
  table *is* the ground truth).

**Effort:** small — one script plus a fixture test. **Payoff:** every future
scoring change (including roadmap item 2's purchase score) becomes a number
instead of a vibe.

### 2. Purchase advisor: "what should I buy this sale?"

**Problem.** `get_wishlist_deals` sorts by price ascending. A €2 shovelware
deal outranks a €19.99-from-€59.99 game the taste profile would score 95%.
The buying decision John actually makes weighs taste, discount quality,
length-per-euro, and whether the backlog already covers that itch.

**Design.** Extend `get_wishlist_deals` (new `sort_by="advice"` plus new
response fields) rather than adding a parallel tool — the pipeline (candidate
platforms, refresh, `_pick_recommended`) is already right; only ranking and
enrichment are missing.

Prerequisites, in order:

1. **Tag coverage for wishlist rows.** `_MATCH_SCORE_SQL` needs `games.tags`,
   but background enrichment targets the owned library. Extend
   `enrich_bg`'s claim query to include games with a `game_wishlist` row:
   SteamSpy by appid works for unowned Steam titles; DekuDeals-sourced titles
   need the IGDB path (themes/keywords). Same writers, same `COALESCE`/union
   rules — no new code paths, just a broadened claim set.
2. **Historical-low from ITAD.** `game_prices` stores current price only;
   ITAD's API exposes storelow/historylow. Add `history_low` (and
   `history_low_currency`) columns to `game_prices`, populated on the same
   fetch. "78% off but still above its historical low" and "at its lowest
   price ever" are the two facts that actually decide a purchase.
3. **The score.** Components, each returned explicitly (transparency over a
   black-box number — the MCP client is an LLM that can explain them):
   - `match_percent` — same anchor mechanism as `discover_games` (max match
     over the owned library), so percentages mean the same thing everywhere.
   - `deal_quality` — 0–1: how close current price is to historical low,
     floored by cut_pct when no history exists.
   - `hours_per_euro` — `hltb_main / price` (null-safe; omit when unknown).
   - `backlog_note` — count of owned unplayed games with match_percent ≥ this
     game's. Honest caution ("you own 14 better-matched unplayed games"), not
     suppression — evergreen multiplayer purchases legitimately ignore it.
   - Composite `advice_score` = weighted product of match × deal_quality with
     critic score as a tiebreaker; weights as module constants so item 1's
     harness (extended with a "did I end up buying/playing it" retro check)
     can tune them later.
   - Hardware preference stays where it is: `_pick_recommended` already
     chooses the platform; advice ranks *games*, not platforms.

**Effort:** medium (three PRs: enrichment claim set, ITAD history columns,
scoring). **Payoff:** the tool John explicitly wants during every seasonal
sale; converts the wishlist from a price list into a decision.

### 3. Deal alerts: push, not pull

**Problem.** Deals expire. `get_wishlist_deals` only answers when asked;
a historical low that lasts 48h during a week John doesn't ask is missed.

**Design.** The periodic refresh loop in `lifecycle.py` already wakes
regularly. Add a post-refresh hook: re-price wishlist games (respecting the
12h TTL — this is nearly free since `get_wishlist_deals` caches), then fire a
Discord webhook (`DEAL_ALERT_WEBHOOK_URL`, optional → feature off when unset;
the daily_brief repo proves the embed pattern) when a game crosses a trigger:

- at or below its historical low (needs item 2's columns), or
- below a per-game `alert_price` (new nullable column on `game_wishlist`,
  settable via a small `set_wishlist_alert` tool or `update_game`-style edit).

Debounce with a `last_alerted_at` column — alert once per price event, not
per refresh cycle. Failure mode matches existing conventions: webhook errors
log a warning, never fail the refresh.

**Effort:** small once item 2 lands. **Payoff:** the highest-leverage feature
per line of code in this list — it acts while you sleep.

### 4. Taste-profile coverage: `suggest_games_to_rate`

**Problem.** Affinity quality is bounded by ratings coverage. The playtime
pseudo-rating (0.3 weight) papers over gaps, but some heavily-played or
tag-rare games carry outsized information the profile is guessing at.

**Design.** A read-only tool ranking unrated games by how much a rating would
teach the model: weight = playtime (strong prior exists, unconfirmed) + sum of
IDF over tags with low affinity sample counts (rare-tag coverage) + recency of
play. Returns top-N with "why" (mirroring `matched_tags`). Active learning
with zero new infrastructure; pairs with `sync_ratings` sessions.

**Effort:** small. **Payoff:** compounds items 1–3, since all of them consume
affinity.

### 5. Smaller items, worth a line each

- **Restore drill.** Run one scripted restore from a CLOSET backup into a
  scratch dir + `init_db` + row-count sanity check; document in deploy.md.
  Backups that have never been restored are Schrödinger's backups.
- **Deals widget.** `apps.py`'s game-cards widget + item 2's fields =
  a natural "sale shelf" card view (cover, cut badge, match %). Cosmetic;
  do it when the advisor stabilizes.
- **Sale-window urgency.** ITAD deal objects carry expiry timestamps; expose
  `deal_ends_at` so the advisor (and alerts) can say "ends in 2 days".
  Trivial add-on to item 2's fetch changes.
- **`data/db/__init__.py` split.** Move connection/migration machinery to
  `db/engine.py`, keep `__init__.py` as the re-export façade (the documented
  stable API surface is unchanged). Do this the next time migrations are
  touched, not before.

## What NOT to build

- **Per-user anything** — ADR 0001 stands; nothing above strains it.
- **Automatic split of collapsed games** — the manual-only stance is correct
  (playtime can't be re-attributed); resist the temptation.
- **A general "price history" table** — ITAD is the historical source of
  record (per CLAUDE.md); item 2 caches two extra scalars, not a time series.
- **More platforms.** PSN has no API; everything else is synced. The platform
  acquisition phase of this project is done — the value is now in the brain,
  not the plumbing.
