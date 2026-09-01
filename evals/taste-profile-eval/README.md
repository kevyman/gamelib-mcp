# Taste-profile eval

Cross-validated backtest of the taste profile: `tag_affinity` (built by
`data/db/affinity.py::recompute_tag_affinity`) plus the match score
`tools/discover.py` computes from it.

## What it measures, and why it exists

Two shipped features read the profile, and until this harness neither had a
number attached to it:

- **`discover_games`** — "what should I play", ranked by `_MATCH_SCORE_SQL`.
- **`get_assessment_context`'s fit check** (`tools/assessment.py::compute_fit`,
  `strong_affinity_cut`) — the taste half of a *purchase* verdict. A miscalibrated
  profile does not just recommend a dull evening, it recommends buying the game.

Since the 07-06 audit asked for this, the model has moved twice — the shrinkage
weight `k` became data-estimated (#131) and `_MATCH_PRIOR` went 3.0 → 9.0 — with
no measurement either way. The harness supplies one.

**Method.** Copy the snapshot to a temp directory (the input is never written
to), enumerate the rated games that carry tags, and K-fold them by game. Per
fold: delete that fold's `ratings` rows *and* mark those games farmed for the
duration — deleting the rating alone leaks, because the recompute would feed the
same game back in as a 0.3-weight playtime pseudo-rating — then
`recompute_tag_affinity()` and score only the held-out games with discover's own
`_SCORING_CTES` + `_MATCH_SCORE_SQL`. The formula is imported, never re-typed: a
local copy would silently start measuring a different model. Ratings and farmed
flags are restored before the next fold.

**The rating target** is the *source-weighted mean* of a game's
`normalized_score` values (0–10), weighted by `affinity.SOURCE_WEIGHTS` —
Backloggd/manual 1.0, `steam_review` 0.5, unknown source 0.5 — i.e. exactly the
scale and the per-source weighting the recompute trains on.

## Getting a snapshot

Raw data never enters git: a library snapshot fingerprints a real person's
purchases, ratings and play behavior. `.gitignore` here drops `results/`,
`*.sqlite`, `*.db` and `*.bak`; **only metrics are ever committed or pasted.**

Use the nightly backup described in `deploy.md` → "Database backups": either
`/root/mcps/data/library/gamelib-nightly.bak` on the server (a consistent
`sqlite3 .backup`, taken 04:15 UTC) or its off-machine copy on the Windows box
(`C:\Users\porta\Backups\gamelib\gamelib-<date>.bak`, 14 rotated). Locally you
can produce one the same way:

```bash
sqlite3 data/gamelib.db ".backup /tmp/gamelib-snapshot.bak"
```

Do not point `--db` at a database an app is writing to. The harness copies the
file (plus its `-wal` sidecar) before opening anything, so it will not corrupt a
live database — but a torn copy is a meaningless measurement.

## Running it

```bash
.venv/bin/python evals/taste-profile-eval/eval_profile.py --db /tmp/gamelib-snapshot.bak
.venv/bin/python evals/taste-profile-eval/eval_profile.py --db /tmp/gamelib-snapshot.bak --baseline
.venv/bin/python evals/taste-profile-eval/eval_profile.py \
    --db /tmp/gamelib-snapshot.bak --folds 0 --json evals/taste-profile-eval/results/loo.json
```

- `--folds N` — K-fold count (default 10); `--folds 0` is leave-one-out, which
  costs one full `recompute_tag_affinity()` per rated game.
- `--seed` — fold shuffle seed. Report it; fold assignment moves the numbers.
- `--baseline` — score with the taste model neutralised: affinity is shuffled
  across tags, keeping the distribution but destroying the tag↔score
  correspondence. **This is the number every other number should be read
  against** — "rho 0.35" means nothing until you know the null model scores 0.05.
- `--json PATH` — same metrics plus the config block.
- `--min-ratings N` — refuse below N rated games (default 20). A profile built
  from a handful of ratings is noise, and a backtest of it is worse than none.

It also prints the tunables in force (`_MATCH_PRIOR`, `_IDF_DF_FLOOR`,
`VIBE_TAG_PROMINENCE_CUTOFF`), the estimated shrinkage `k` per fold and for the
full model, n rated / n unrated, and wall time.

## Reading the four numbers

1. **Spearman rho (held-out)** — rank correlation between the predicted match
   score and the actual rating, over every held-out game pooled across folds.
   The headline. Sign matters more than magnitude: a rho near zero says the
   profile carries no ranking information; a *negative* rho says it is
   anti-correlated with taste, which is what the pre-#131 hardcoded `k=2` did.
2. **precision@10 (rating ≥ 8)** — of the ten highest-predicted held-out games,
   how many he actually loved. This is the number `discover_games` lives or dies
   by: nobody reads past the first screen.
3. **Separation** — mean prediction for ratings ≤ 4 vs ≥ 8, and the gap. Rho can
   look healthy while both camps sit on top of each other; a wide gap is what
   `compute_fit` needs to call a candidate a match or a miss at all.
4. **Playtime control** — Spearman between the *full-model* match score and
   `log1p(total playtime)` over UNRATED owned games. Ratings are sparse and
   self-selected; playtime is the profile's other ground truth. Caveat: unrated
   owned games with ≥2h ARE the playtime pseudo-rating population, so the model
   has seen this signal — it is a consistency check, not a hold-out, and it
   should move in step with rho rather than replace it. `spearman_played_only`
   drops the zero-playtime tie mass.

Per-fold rho and `k` catch instability the pooled number hides: `k` swinging
across folds means the variance decomposition is running on too little data.

## Workflow rule

**Any PR that touches the taste model pastes before/after metrics from this
harness into its description** — same snapshot, same `--seed`, both the model
and the `--baseline` run. That covers:

- `gamelib_mcp/data/db/affinity.py` (any of it: the formula, `SOURCE_WEIGHTS`,
  the playtime pseudo-rating weight/cap/threshold, the shrinkage estimator or
  its guard rails, `STRONG_AFFINITY_RANK`/`_SUPPORT`)
- `_MATCH_PRIOR`, `_IDF_DF_FLOOR`, `_MATCH_SCORE_SQL`, `_SCORING_CTES` or
  `VIBE_TAG_PROMINENCE_CUTOFF` in `tools/discover.py`
- `MERGED_TAG_CAP` and the tag vocabulary (`tag_synonyms.py`, `tags.py`) — they
  change what the profile is computed *over*

Metrics only. The snapshot, the per-game predictions, and anything naming
individual games stay out of git and out of the PR.

`tests/test_eval_profile.py` guards the harness itself against a synthetic
library whose taste is knowably learnable.
