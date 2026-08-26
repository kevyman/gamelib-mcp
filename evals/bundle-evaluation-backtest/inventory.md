# Bundle-evaluation backtest — Phase 1 inventory

Goal: test whether the `bundle-evaluation` skill's triage (must-have / nice / filler per
constituent) and verdicts match the owner's actual historical engagement. This file is the
candidate test set for the owner to pick/veto before fixtures are built. Failure-mode
discovery, not a benchmark score.

## Ground-truth definitions (per constituent, from current DB)

- **Wanted**: non-farmed playtime ≥ 2h, OR rated ≥ 7, OR completion_status ∈ (completed, playing, evergreen).
- **Never launched**: 0 playtime (or farmed-only playtime) and no rating.
- **Ambiguous**: everything in between (launched < 2h unrated, or rated < 7) — excluded from the matrix, listed separately.
- **Farmed rows** (`games.is_farmed = 1`) are treated as never-launched unless rated — card-farming playtime is fake engagement.

## Headline findings from the raw inventory

1. **163 named-bundle groups (2011–2026) + 115 Humble Monthly/Choice months (2015-10 → 2026-08).** Far more than needed; selection below optimizes for label quality.
2. **Engagement collapse after 2020.** Humble Monthly months 2016–2020 average ~40–90% wanted constituents. From 2021-01 onward, nearly every Choice month has **0–1 wanted games out of ~8** — they kept subscribing for 5+ years while almost never launching a single Choice game. (Steam playtime is lifetime-cumulative, so this is real, not a data window artifact.) Consequence: recent months give almost no positive labels, and the bundle-level "they bought it" label is worth very little there. Their "would you buy this again?" answers will matter most for these.
3. **Card-farming era pollutes 2015–2017.** Many mid-2010s months have 3–6 farmed rows; those months' ground truth is degraded. Candidates below prefer low-farmed months.
4. **Ratings are sparse everywhere** (0–1 rated constituents per bundle) — playtime carries the ground truth almost alone.
5. **Skipped-month negatives exist**: gaps at 2020-05, 2020-10, 2021-01, 2021-04, 2021-09, and 2022-05…08. Whether each was a real "looked and passed" skip vs. a paused/cancelled subscription needs Gmail confirmation in Phase 2 (the 4-month 2022 gap smells like a pause).

## Data-quality caveats that shape fixtures

- **`acquired_at` is NULL on 1,079 of 4,064 owned primary rows (27%).** Per the design, NULL-dated games are excluded from `owned_as_of_date` unless independently datable; the count will be noted per fixture.
- **Pre-2019 named-bundle dates are unreliable** — e.g. "Humble Telltale Bundle 2017" carries acquired_at 2015-08-30, "Hooked on Multiplayer 2018" carries 2015-02-19. These look like import artifacts. Monthly/Choice month attribution (from `purchase_source='subscription'`) looks consistent throughout, and 2019+ named-bundle dates look sane.
- **"Best of Boomer Shooters" (both volumes) and "Choose Wisely Bundle" have split/duplicate purchase records** across years (one constituent via a Choice month, the rest bought later). Excluded to avoid ambiguous purchase dates.
- **eShop multi-game SKUs** (BioShock Collection, Castlevania Collections, MGS Master Collection…) are 1–6 game fixed collections — closer to game-quality territory, thin triage signal. Excluded.
- Bundles **younger than ~12 months** (Indie Fears 2025-10, Idlers 2026-07, all 2026 eShop buys) are excluded from ground truth — "never launched" means nothing that soon in a hoarder library. Indie Fears is listed as an optional borderline pick.

## Recommended test set (17 purchased + up to 5 negatives)

### A. Humble Monthly era (2016–2020) — richest labels, $12/month

| # | Month | n | wanted | never | ambig | farmed | Notable contents |
|---|-------|---|--------|-------|-------|--------|------------------|
| 1 | 2016-09 | 8 | 2 | 6 | 0 | 4 | Grim Dawn, Hotline Miami 2, Slime Rancher |
| 2 | 2017-09 | 8 | 5 | 2 | 1 | 0 | Rise of the Tomb Raider, Furi, Getting Over It, Orwell |
| 3 | 2017-12 | 7 | 7 | 0 | 0 | 0 | Sleeping Dogs, Quantum Break, The Long Dark, DoW III |
| 4 | 2018-01 | 10 | 7 | 3 | 0 | 0 | Civilization VI, Owlboy, Life is Strange, Tacoma |
| 5 | 2018-08 | 8 | 8 | 0 | 0 | 0 | Sniper Elite 4, Tales of Berseria, Little Nightmares |
| 6 | 2018-10 | 9 | 7 | 2 | 0 | 0 | HITMAN, Hollow Knight, 7 Days to Die, Dead Island |
| 7 | 2018-11 | 11 | 5 | 6 | 1 | 1 | Cities: Skylines, MGSV Ground Zeroes, Mega Man LC |
| 8 | 2019-01 | 9 | 2 | 7 | 0 | 4 | Yakuza 0, Sniper Elite 3, Full Metal Furies |
| 9 | 2020-02 | 12 | 6 | 6 | 0 | 0 | Frostpunk, Okami HD, Pathfinder: Kingmaker, SHENZHEN I/O |
| 10 | 2020-04 | 12 | 6 | 5 | 1 | 0 | HITMAN 2, GRIS, Opus Magnum, Bard's Tale IV |

Mix rationale: two all-wanted months (3, 5), two mostly-dead months (1, 8), balanced rest —
tests both over-dismissal and over-selling at bundle level and per game.

### B. Humble Choice era (2023–2024) — near-zero engagement, tests the "should have skipped?" question

| # | Month | n | wanted | never | ambig | Notable contents |
|---|-------|---|--------|-------|-------|------------------|
| 11 | 2023-04 | 8 | 2 | 5 | 1 | DEATH STRANDING, Life is Strange 2, Rollerdrome |
| 12 | 2023-08 | 8 | 0 | 7 | 1 | Disco Elysium, Trek to Yomi, Road 96, Chivalry 2 |
| 13 | 2024-05 | 8 | 1 | 6 | 1 | Hi-Fi RUSH, Yakuza: Like a Dragon, Steelrising |
| 14 | 2024-12 | 10 | 1 | 9 | 0 | Old World, The Invincible, Bomb Rush Cyberfunk, Venba |

### C. Named bundles (2023–2024) — fixed-lineup Humble bundles, real sticker prices

| # | Bundle | Date | Paid | n | wanted | never | ambig |
|---|--------|------|------|---|--------|-------|-------|
| 15 | RPG Legends: Baldur's Gate & Beyond | 2023-07 | $11.14 | 13 | 0 | 13 | 0 |
| 16 | Action Roguelikes: What Kills You Makes You Stronger | 2023-11 | $13.00 | 7 | 2 | 2 | 3 |
| 17 | Luck of the Draw: Roguelike Deckbuilders Encore | 2023-12 | $15.43 | 6 | 2 | 1 | 3 |
| 18 | The Many Worlds of Muv-Luv | 2024-07 | €23.85 | 10 | 0 | 10 | 0 |
| 19 | Atari: Recharged Retro Revival | 2024-10 | €18.32 | 11 | 0 | 10 | 1 |

(15, 18, 19 are pure hoard purchases by engagement — the interesting question is whether the
skill would have said Skip. 16 and 17 hit the owner's roguelite-deckbuilder prior — good
discrimination tests. #18 Muv-Luv and #19 Atari at real €18–24 prices are the highest-stakes
verdicts in the set.)

Optional borderline: **Indie Fears Bundle** (2025-10, €13.50, 13 games, 0 wanted) — only 10
months old, ground truth weak; include only if the owner confirms they're already sure they'd skip it.

### D. True negatives — skipped/paused months (pending Gmail verification)

2020-05, 2020-10, 2021-01, 2021-04, 2021-09, 2022-05, 2022-06, 2022-07, 2022-08.

Phase 2 will check Gmail for (a) lineup announcement emails received while subscribed and
(b) skip confirmations vs. pause/cancel emails. Only confirmed "looked and passed" months
count as negatives; target 3–5. If none are confirmable, the backtest proceeds triage-only
as designed.

## Label volume

The 19 recommended purchased bundles contribute ~170 constituent rows (~150 primary), of
which ~64 wanted / ~90 never-launched / ~12 ambiguous — enough for a per-tier confusion
matrix with visible failure modes.

## Alternates (swap-ins if any candidate is vetoed)

2016-08 (SOMA, Banner Saga — 4/6 wanted), 2017-10 (Shadow Tactics, ESO — 6/8), 2018-03
(Deus Ex: MD, Mafia III — 7/9), 2018-12 (Just Cause 3, Wizard of Legend — 7/8), 2019-07
(Kingdom Come: Deliverance, Moonlighter — 6/7), 2019-10 (CoD:WWII, Crash/Spyro trilogies —
7/8), 2020-01 (Middle-earth: SoW, Two Point Hospital, Graveyard Keeper — 8/14), 2023-02
(Fallout 3, Thronebreaker — 0/9), 2024-04 (Victoria 3, HUMANKIND — 0/6), Fight T1D With
JRPGs! 2023-12 ($10, 4 games, 0 wanted).
