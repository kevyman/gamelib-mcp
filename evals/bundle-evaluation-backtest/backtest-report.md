# Bundle-evaluation skill backtest — report

**TL;DR:** Run blind against 24 historical bundle decisions (19 purchased, 5 skipped Humble
Choice months), the skill's per-game triage ranks correctly in aggregate — strong-engagement
rate falls 21% → 16% → 7% across must-have → nice → filler, and it almost never dismisses a
game John truly ends up loving (5 misses in 71 filler calls). But its **verdict machine is
broken in one direction: it said Buy on 21 of 24 bundles**, including all five months John
actually looked at and skipped, and all four purchases he now says he wouldn't repeat. The
two verdicts that deviated from Buy (Muv-Luv conditional-lean-skip, Atari Recharged skip)
were both vindicated by John's own labels. The systematic failures are (1) a buy trigger
that any $12 8-game bundle passes trivially, (2) must-have oversell of short narrative/arty
indies the taste profile loves on paper but John never launches, and (3) a bottom-tag veto
that files single-player AA campaigns (Sniper Elite 4, completed; Dawn of War III, 18h) as
filler because of "military/tactical/multiplayer" tag penalties. Proposed skill edits at the
end; none applied.

## Method (summary)

- Test set (John-approved): 8 Humble Monthly months (2016–2019), 6 Humble Choice months
  (2020–2024), 5 named Humble bundles (2023–2024), plus 5 Gmail-confirmed post-reveal
  skipped Choice months as true negatives.
- Full original lineups reconstructed from ITAD/barter.vg/press (tier structures, early
  unlocks, pick-N rules, mid-month additions, unredeemed keys). Six extra explicit
  negatives found: games John declined in pick-10-of-12 months or never redeemed.
- Frozen fixture per bundle: lineup, price, structure, prior-ownership flags, and the
  library as of the decision date (dated acquisitions only; 863 undated games excluded as
  a stated lower-bound caveat). No playtime, ratings, or completion of anything.
- One fresh subagent per fixture ran the full skill text with the fixture as its only
  library knowledge; web search allowed for constituent-level facts only; bundle-verdict
  searches banned. Required structured output: tier per constituent + verdict.
- Ground truth from the current DB: **wanted** = ≥2h non-farmed playtime, rated ≥7, or
  completion status; **never** = never launched, unrated; ambiguous excluded. A stricter
  **strong-wanted** tier (≥8h / completed / rated ≥7 / relaunched ≥1y later) separates real
  engagement from the mass idle sessions John confirmed ("mostly idling") pollute 2017–2020
  playtime. Engagement whose `last_played` predates the bundle decision date is excluded
  entirely — it came through another copy/family share and cannot be attributed to the
  purchase (HITMAN and Cities: Skylines were the two such rows).

## Per-game confusion matrix

Decided rows only (ambiguous excluded; owned/excluded rows removed). **Strict** is the
honest read per John's idling confirmation; the loose matrix is shown for comparison.

| Predicted tier | strong-wanted | idle-band 2–8h | never launched | n |
|---|---|---|---|---|
| must-have | **7 (21%)** | 11 (33%) | **15 (45%)** | 33 |
| nice | 8 (16%) | 15 (30%) | 27 (54%) | 50 |
| filler | **5 (7%)** | 13 (19%) | 52 (74%) | 70 |

Loose (any ≥2h counted as wanted): must-have 55% wanted, nice 46%, filler 26%.

Reading it: the ordering is right and the bottom end is well-calibrated — of six explicit
negatives (games John declined or never redeemed), five were tiered filler and one nice.
The top end is the problem: a must-have call converts to real engagement about one time
in five, and never-launched (45%) is its single most likely outcome.

## Bundle-level verdicts

| Bundle | Skill verdict | Reality | Buy again? (John) | Agree? |
|---|---|---|---|---|
| Monthly Oct 2016 | buy | bought | yes | ✓ |
| Monthly Oct 2017 | buy | bought | yes | ✓ |
| Monthly Jan 2018 | buy | bought | **no** | ✗ |
| Monthly Feb 2018 | buy | bought | yes | ✓ |
| Monthly Sep 2018 | buy | bought | **no** | ✗ |
| Monthly Nov 2018 | buy | bought | yes | ✓ |
| Monthly Dec 2018 | buy | bought | yes | ✓ |
| Monthly Feb 2019 | buy | bought | yes | ✓ |
| Choice Feb 2020 | buy (claim) | claimed | yes | ✓ |
| Choice Apr 2020 | buy (claim) | claimed | yes | ✓ |
| Choice Apr 2023 | buy (claim) | claimed | yes | ✓ |
| Choice Aug 2023 | buy (claim) | claimed | **no** | ✗ |
| Choice May 2024 | buy (claim) | claimed | yes | ✓ |
| Choice Dec 2024 | buy (claim) | claimed | **no** | ✗ |
| RPG Legends 2023 | buy_lower_tier (= tier he bought) | bought t2 | yes | ✓ |
| Action Roguelikes 2023 | buy_lower_tier | bought top | (no label) | – |
| Luck of the Draw 2023 | buy (t2 = his) | bought t2 | (no label) | – |
| Muv-Luv 2024 | conditional (lean skip) | bought top, 0 min played | **no** | ✓ |
| Atari Recharged 2024 | **skip** | bought top, ~0 played | (no label) | ✓* |
| Skipped Oct 2020 | buy | **skipped** | – | ✗ |
| Skipped Jan 2021 | buy | **skipped** | – | ✗ |
| Skipped Sep 2021 | buy | **skipped** | – | ✗ |
| Skipped May 2022 | buy | **skipped** | – | ✗ |
| Skipped Aug 2022 | buy | **skipped** | – | ✗ |

\* Atari: no buy-again label, but 10 of 11 constituents never launched — scored as a correct skip.

Against buy-again labels: 12/16 agreement, and **all four misses are false Buys — with a
frame caveat.** The Monthly-era verdicts above are *hindsight-frame*: the evaluator saw
the full post-reveal lineup, which the real purchase decision never did. Every monthly
evaluator also recorded a *decision-frame* verdict from the early unlocks alone: 6 buy /
2 skip, and both skips (Oct 2016, Dec 2018) hit months John would re-buy — so at decision
level the skill produces false Skips too (2/8 on Monthly months), and the hindsight table
overstates its Monthly agreement. Choice-era and named-bundle verdicts are unaffected
(their full lineups were visible at decision time). Against the skip ground truth: 0/5.
The Buy-bias asymmetry remains the dominant finding either way.

## Failure modes

### F1. The verdict machine cannot say Skip on a generic multi-genre bundle
21 of 24 verdicts were Buy-family. The buy trigger — "≥2 wanted games whose combined
realistic price clears the bundle price" — is trivially satisfied when 8–12 games cost
$10–12: any two plausible-fit indies sum past $12 in historical-low prices. Every skipped
month "cleared" it (e.g. Aug 2022: Omno + Emily is Away <3 ≈ $15–19 vs $11.99). The only
Skip-family verdicts came where deviation was unambiguous (single-franchise VN bundle;
uniform retro-penalty pile). Note the fairness caveat: the evaluators were blinded to the
skill's sharpest real-world evidence — "he claimed the last N months and launched nothing"
— because that data is the answer key. In live use Step 4's track-record check has this;
but the skill treats it as *descriptive framing*, not as a gate, so nothing forces it to
flip a verdict. It should.

### F2. Must-have oversell of short narrative/arty indies (the profile's aspirational tail)
The 15 must-have→never rows are dominated by exactly one shape: short, story-rich,
emotional, minimalist, artistically acclaimed — Eliza, GRIS, The Life and Suffering of Sir
Brante, Road 96, The Invincible, Venba, Scanner Sombre, MOLEK-SYNTEZ, Moonstone Island.
The taste profile's top tags (short 0.18, emotional 0.152, minimalist 0.149) are built from
what John *rates highly when he does play* (avg rating 9.01, mostly indie darlings) — not
from what he *reaches for*. Tag-affinity fit alone is an aspiration signal, and the skill
currently lets it mint must-haves. Taste-profile leakage makes this finding *stronger*:
even with a profile partly trained on these very outcomes, must-have precision was 21%.

### F3. The bottom-tag veto overrides demonstrated behavior
The five strict filler misses — the worst cell — are Sniper Elite 4 (**completed**, 25h),
Dawn of War III (18h), 7 Days to Die (10h+), Dead Island (10h), Pathfinder: Kingmaker
(12h) — plus buy-again evidence that John values these months. Each was tanked by
bottom-tag penalties (military, tactical, multiplayer, violent, rts) applied to a
single-player campaign, or by CRPG-length skepticism. The penalties describe modes he
avoids (PvP, live-service), but the evaluators applied them to genres — while the library's
own playtime record (Sniper Elite V2/3 played; strategy titles played) said otherwise.

### F4. Engagement ≠ worth it — the ground truth cuts both ways
John would *not* re-buy Sep 2018 (he completed Sniper Elite 4 from it) or Jan 2018 (18h in
DoW III), and *would* re-buy Feb 2019 (Yakuza 0 farmed, barely played). Playtime is a
noisy proxy in both directions, which is why this report leans on the strict tier, the
explicit negatives, and the buy-again labels together. Post-2020 the proxy is stark:
almost nothing from any claimed Choice month was ever launched — by engagement, most
recent claims were mistakes, and the two most recent NO labels (Aug 2023, Dec 2024) agree.

### F5. What works — keep it
Per-game triage ordering; ownership screening (owned/€0 handling was clean everywhere,
including prior Epic-freebie copies); tier walking (both buy_lower_tier calls landed on
sensible tiers; RPG Legends tier-2 exactly matched what John actually did); DLC-without-
base and franchise-sequencing logic (Muv-Luv's 8 sequenced fan-content items correctly
zeroed); explicit-negative rejection (5/6).

## Caveats

- **Taste-profile leakage**: current profile partly built from these purchases. Direction:
  favors the skill. Oversell findings survive it; the 21% must-have precision is an upper
  bound estimate of true precision.
- **Track-record blinding**: fixtures deliberately excluded constituent playtime of past
  bundles, which also blinded the skill's legitimate Step-4 evidence. F1 is partly an
  artifact of that — but only partly, since the skill text makes track record framing, not
  a gate.
- **Selection bias**: purchased bundles dominate; triage scoring and the 5 skipped months
  + 6 explicit negatives mitigate.
- **Anachronistic reviews**: evaluators used today's review data flagged as such.
- **Idle contamination**: 2017–2020 playtime includes mass idle sessions (John-confirmed);
  handled via the strict tier, imperfectly.
- **Model confound**: 22 of 24 evaluations ran on Sonnet, 2 (Muv-Luv, Nov 2018) on the
  session model after a mid-run limit reset forced a cheaper rerun. Both nuanced
  deviating verdicts came from mixed sources (Muv-Luv: session model; Atari skip: Sonnet),
  so the Buy-bias is not obviously a small-model artifact, but per-verdict boldness may
  vary by model.

## Proposed skill edits (not applied — for John's review)

**E1 — Make the subscription-month track record a gate, not framing (Step 0/Step 4).**
Add to Step 0's "Is he already subscribed?" block:
> Before evaluating the lineup at all, compute the recent claim-to-launch record: of the
> last 6 claimed Choice months (Step 1 search results carry `playtime_hours` for their
> constituents), how many produced ≥2h of play on anything? If the answer is ≈0, the
> default verdict for this month is **Skip (pause)** and only a must-have whose realistic
> solo price clears the month price on its own may override it. State the record in the
> verdict ("last 6 claimed months: 0 games launched").

**E2 — Behavioral corroboration required for must-have (Step 2).**
Append to the tier definitions:
> **Must-have requires more than tag fit.** Promote to must-have only with (a) a wishlist
> entry or series/franchise he has played, or (b) profile-tag fit *plus* a played (≥2h,
> non-farmed) library neighbor in the same genre. Tag fit alone — especially the
> short/emotional/story-rich cluster, which measures what he rates highly, not what he
> launches — caps a game at **nice**. The affinity profile is an aspiration signal; the
> playtime record is the behavior signal; must-have needs both.

**E3 — Negative tags penalize modes, not campaigns (Step 2).**
Append after the standing-priors line:
> The multiplayer/PvP/live-service penalty applies to games that *require* those modes.
> A single-player campaign with strong reviews does not inherit the penalty from
> military/tactical/violent genre tags — check the library first: if he has real playtime
> on same-franchise or same-genre titles (Sniper Elite, strategy campaigns), the penalty
> does not apply, and prior-franchise playtime is itself must-have corroboration per E2.

**E4 — Harden the buy trigger for subscription months (Step 5).**
> For a Choice/subscription month, only **must-have** games count toward the buy trigger
> (nice-tier value is margin, never the case), and their realistic prices must be taken
> *after* the bundled-before decay — a twice-a-year-bundled indie's realistic price is its
> next bundle appearance, not its ITAD low. "≥2 wanted games clear $12" is no bar at all
> when every month ships 8 games; the bar is "this month is better than the pause button."

**E5 — Mystery boxes are decided on early unlocks only (Step 0).**
> For Humble-Monthly-style mystery boxes (if they return): the decision is the early
> unlocks vs the price; never argue from the expected value of the unrevealed remainder.
> (Backtest: two months' verdicts flipped between frames — Oct 2016 and Dec 2018 looked
> skippable on early unlocks alone, yet John would re-buy both. A mystery box's value
> genuinely wasn't knowable at decision time; judging only what's visible is the only
> honest frame, and the bundle-level table's Monthly rows are hindsight-frame — see the
> frame caveat there.)

## Files

- `inventory.md` — Phase 1 candidate set (John-approved).
- `lineups/*.json` — reconstructed lineups, tiers, early unlocks, sources (public facts).
- `work/build_fixtures.py`, `work/score.py`, `work/eval_prompt_template.md` — the harness.
- The raw data (fixtures, results, ground truth, taste profile, dated library dump) is
  **not committed** — personal data on a public repo; see `README.md` for regeneration.
