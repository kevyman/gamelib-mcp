---
name: backlog-triage
description: Methodology for deciding what the user should play next from the games they already own, sized to the time and energy they actually have. Use this skill whenever the user asks what to play next, what to start, what fits tonight/this weekend/a trip, what to pick from their backlog, whether to continue or drop their current game, or says they're bored/stuck/can't decide what to play — even if they don't say "backlog". Phrases like "what should I play", "pick something for me", "I have 2 hours", "nothing is grabbing me", "should I keep playing X or move on", or "give me something short" should all trigger this skill. Do NOT use for purchase decisions — that's the game-quality skill.
version: "2.1.0"
---

# Backlog Triage

The user's problem is never a shortage of games — it's a 2,800-title library and no good "play this tonight" answer. Triage exists to shrink the decision, not enumerate options. Three principles govern everything:

1. **The session budget is the primary filter.** A recommendation that doesn't fit the time they actually have is wrong, no matter how good the game is.
2. **Respect the active game.** If something has momentum, "keep playing that" is often the correct verdict — triage is for when nothing is active or they're stalling.
3. **Small output.** 2–3 picks maximum, each with a one-line reason. A list of 10 recreates the paralysis the skill exists to solve.

## Step 0: Establish the session budget

Infer from what they say, or ask one short question if genuinely ambiguous. Buckets:

| Budget | Shape | What qualifies |
|---|---|---|
| **Snack** (≤ 1h, interruptible) | Weeknight, interruptions likely | Run-based games, save-anywhere, pause-anytime. Roguelites are the home genre here. |
| **Evening** (1–2h, mostly uninterrupted) | Post-bedtime block | Chapter-structured games, puzzle games, anything with clean stopping points. |
| **Block** (3h+) | Free weekend afternoon | Games that need immersion to land: narrative-heavy, exploration, "one more turn". |
| **Deep dive** (rare: trip, vacation, sick day) | Sustained multi-day attention | The only slot where a long-form commitment (JRPG-scale) is even eligible. |

Life context matters: when daily life mostly allows short, interruptible windows, the default assumption is snack/evening unless they say otherwise. Session shape (can you save anywhere? does a run take 25 minutes or 90?) matters more than total HLTB length — a 60h roguelite fits a snack budget; a 12h game with 45-minute checkpoint gaps doesn't.

## Step 1: Check current state before recommending anything

Run in parallel on the **Game Library** MCP:

1. `get_play_history` (last ~30 days) — what's actually being played, per-game hours, most-played first.
2. `search_games` / `get_game_detail` on the top active title(s) — completion status, how far in, whether it's flagged as the active/queued game.
3. `get_stats(report="backlog")` — completion %, pace, years-to-clear. Used for framing and shelving decisions, never for guilt.

Read the momentum signal:

- **Active game with recent sessions and rising hours** → default verdict is *continue*, unless they're explicitly asking to switch or the budget doesn't fit that game's session shape. Say so plainly: "Honest answer: keep playing X."
- **Active game stalled 2+ weeks** → treat the ask as a fork: offer one "re-entry" framing for the stalled game (what they'd be returning to, whether it resumes well) *and* fresh picks. Don't silently pretend the stalled game doesn't exist.
- **Nothing active** → clean triage; go to Step 2.

If a game is explicitly queued next (completion status / notes), it gets first consideration — but still has to pass the session-budget filter. Queued ≠ automatic.

## Step 2: Build the candidate pool

**Eligibility rule: everything owned is a candidate unless its `completion_status` is `completed` or `abandoned`.** Playtime is not an eligibility filter. A game with 0.6h on the clock is exactly as eligible as one with 0h — the user bounces off games for reasons that are often situational (wrong week, wrong platform, tried it on a laptop in 2021), and a partial-hours game they'd love is a better answer than a pristine-unplayed game they wouldn't. Quality and fit decide; hours-on-clock does not.

Practically:

- `discover_games` — **always pass `unplayed_only=false`.** The default (`true`) silently drops every game with any recorded playtime, which hides some of the strongest candidates in the library (this is how Valheim and V Rising vanished from a survival query despite being obvious matches). Use vibe mode when they give a mood; inspect `matched_tags` to check the match isn't resting on one incidental tag.
- `get_library_stats` with `filter=all` plus `max_hltb_hours` matched to budget and `tags`/`genres` steering from the taste profile. Then drop anything whose `completion_status` is `completed` or `abandoned`. Do **not** reach for `filter=unplayed` — it's the same trap as `unplayed_only=true`.
- `get_stats(report="taste")` if not already fresh in context — high-affinity tags steer the filters; low-affinity tags (RTS, military, cyberpunk) prune the pool.

Cap the pool at ~8 before scoring. More is noise. **Rank on quality × fit, not on how untouched a game is.**

This rule only works if the statuses are honest, so it comes with an obligation: when a game clearly *is* dead to them, get it marked. `check_library(checks=["completion.unclassified"])` surfaces games whose status looks stale or unset — its `suggested_action` hands the actual write to `update_game`. That pair is the maintenance loop that keeps `abandoned` meaningful — see the shelve section in Step 4. A library where nothing is ever marked abandoned makes this filter useless.

## Step 3: Score candidates on four axes

No script, no fake precision — this is a judgment layer. For each candidate weigh:

1. **Fit** — taste-profile affinity plus anchors, exactly as in the game-quality skill (Step 2 there, backed by `get_assessment_context`'s `fit`/`anchors` blocks). The user's reaction to owned games sharing the core tags is the best predictor. When using `discover_games`, inspect `matched_tags`: a match resting on a single incidental tag is weak evidence, whatever the score says — prefer candidates whose match spans multiple genre/mechanic tags.
2. **Session shape vs. budget** — the gate. Check structure (run-based / chapter / open) via tags and `get_game_detail`; when unsure how a specific game saves or chunks, a quick web search beats guessing.
3. **Freshness** — contrast with what they just played. Coming off a 40h stint in one genre, the best pick is usually a palate cleanse, not more of the same. Use `get_play_history` genre mix to judge.
4. **Stall risk** — pattern-match against known abandonment archetypes: long-form JRPGs (Persona 5 Royal, Persona 4 Golden both stalled), survival base-builders, anything front-loaded with 5+ hours of tutorial. A high-fit game can still be a wrong-*slot* game; flag it for a deep-dive budget instead of forcing it into a weeknight.

   **Prior bounces belong here, not in eligibility.** A game they put 2h into and dropped is still a candidate (Step 2), but the bounce is real evidence and gets weighed on this axis. Two readings, and it's worth naming which one applies:
   - *Situational bounce* — one short session, plausibly bad timing or wrong platform, and the fit signals are strong. Re-entry is cheap; recommend it and say it's a second look.
   - *Structural bounce* — the drop matches a known archetype (survival base-builder, long JRPG) **and** they have several other games in that same lane sitting at 1–3h. That's a pattern, not an accident. Say so plainly rather than re-serving the same wall, and offer the shelve.

   When a partially-played game wins on quality and fit, lead with *why it's a good game for them*, and mention the prior hours as context ("you've got 2h in it") — never as an obligation to finish.

Platform is a tiebreaker, not an axis: for snack budgets, prefer the version/platform that's fastest from couch to gameplay (handheld beats booting a PC).

## Step 4: Verdict output

ALWAYS use this structure, kept short:

```
**Budget:** [snack / evening / block / deep dive] — [one-line context]

**Honest default:** [continue Active Game / nothing active — fresh pick below]

1. **[Game]** ([platform], [HLTB or session length]) — [why it fits: anchor + shape, one line]
2. **[Game]** — [one line]
3. **[Game]** — [one line, optional]

**Shelve candidates:** [game(s) stalled >60 days with low return odds — offer to mark abandoned/shelved via update_game], or omit the section.
```

The shelve section is a feature, not an afterthought: triage includes *permission to quit*. A game stalled for months in a genre they historically abandon is dead weight in every future triage — offering to formally shelve it (status change, not deletion) keeps the pool honest. Never frame shelving as failure; framing is "clearing the queue."

## Anti-patterns (never do these)

- Recommending a purchase. Triage is play-what-you-own by definition; if the library genuinely has nothing for the ask (rare at 2,800 games), say that rather than pivoting to a store.
- Listing more than 3 picks, or hedging with "it depends" instead of committing to a #1.
- Recommending a known stall-archetype game (long JRPG, survival builder) for a snack/evening budget without flagging the mismatch.
- Ignoring the active game — either endorse continuing it or explicitly justify switching.
- Using backlog stats as guilt ("you own 2,800 games and finished 4%"). Stats frame the shelving conversation; they never scold.
- Treating backlog age as obligation. Sunk cost applies to games too — "you bought it in 2019" is not a reason to play it in 2026.
- Recommending from memory of the library instead of querying it. Ownership, playtime, and status come from the MCP, every time.
- Calling `discover_games` with `unplayed_only=true`, or `get_library_stats` with `filter=unplayed`, when building the pool. Both silently delete every partially-played game — including the best candidates. Only `completed` and `abandoned` are disqualifying.
- Preferring a pristine-unplayed game over a better-fitting one just because it has 0h on the clock. Hours played are not a scoring axis.
