# Blind evaluator prompt template

Substitute {ID} and launch one fresh subagent per fixture with exactly this prompt.

---

You are evaluating a game bundle for John using his bundle-evaluation skill methodology. This is a historical backtest: the bundle is from the past, and you must decide as if it were live, from the fixture's frozen data.

Read these three files first:
1. /home/user/gamelib-mcp/evals/bundle-evaluation-backtest/work/skill.md — the methodology. Follow it.
2. /home/user/gamelib-mcp/evals/bundle-evaluation-backtest/fixtures/{ID}.json — the bundle: constituents, price, structure, decision date, and John's library as of that date.
3. /home/user/gamelib-mcp/evals/bundle-evaluation-backtest/work/taste_profile.json — John's tag-affinity taste profile (stands in for `get_stats(report="taste")`).

HARD RULES — violating any of these invalidates the run:
- You have NO access to John's library beyond the fixture. Do NOT call any Game_Library MCP tool, Gmail tool, or any other MCP tool. Allowed tools: Read, WebSearch, WebFetch, Write only.
- The fixture's `owned_as_of_date` list and per-constituent `owned_before` flags REPLACE the skill's Step 1 ownership screen. A constituent without an `owned_before` flag was not owned at decision time. Use `owned_as_of_date` for the skill's near-substitute checks.
- Web search is allowed ONLY for constituent-level facts: what a game is, its genre/tags, review percentages, critic scores, HLTB length, historical price ranges. 
- BANNED searches (community-verdict leakage): anything about the bundle itself — its name ("Humble Choice {month} worth it"), lineup roundups, Reddit/forum verdict threads, "is this bundle good". If a search result page is primarily about the bundle rather than a single game, do not use it.
- BANNED: searching whether John (or "a player") owns/played anything. John's data comes only from the fixture.
- Treat your knowledge cutoff carefully: for any game you don't confidently know, search it (constituent-level only) rather than assess from memory.
- Prices: as-of-date historical lows are not reliably recoverable; use what you can find, treat euro/dollar math as approximate, and grade tiers strictly. The tier assignment matters more than the exact arithmetic.
- The skill's MCP tool calls (`search_games`, `get_assessment_context`, `get_wishlist`, `get_library_stats`, `import_purchases`, etc.) are unavailable — the fixture substitutes for Step 1, the taste profile for Step 2.1, and web review data for Step 3's assessment context. Skip Step 6 (purchase handoff) entirely.
- For a "mystery box" structure (Humble Monthly): evaluate the bundle verdict as if the full lineup were known (this is a backtest convention; the `early_unlock` flags tell you what was actually visible pre-purchase — note in your output if your verdict would differ on early-unlocks-only).
- For a "choice: pick N of M" structure: the verdict question is "subscribe/claim this month at this price vs skip", and additionally rank which N you would pick.

Deliverable: write EXACTLY one JSON file to /home/user/gamelib-mcp/evals/bundle-evaluation-backtest/results/{ID}.json:

{
  "id": "{ID}",
  "verdict": "buy" | "buy_lower_tier" | "conditional" | "skip",
  "verdict_rationale": "2-3 sentences",
  "confidence": "high" | "medium" | "low",
  "confidence_note": "what data was thin",
  "wanted_subset": ["titles you'd count as paying for the bundle"],
  "wanted_subset_value": "approx sum vs bundle price, one line",
  "per_game": [
    {
      "title": "exact title from fixture",
      "tier": "must-have" | "nice" | "filler" | "owned",
      "rationale": "one line (required for must-have and nice; 'filler' may be terse)"
    }
  ],
  "picks_if_choice_month": ["ranked titles"] | null,
  "early_unlock_only_verdict": "buy|skip + one line" | null,
  "notes": "anything methodologically noteworthy"
}

Every constituent in the fixture MUST appear exactly once in per_game. Use tier "owned" for constituents with an owned_before flag. Your final report message: just the verdict line and file path.
