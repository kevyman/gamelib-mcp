# Single-User Decision (ADR) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (this is a short, mostly-documentation plan; subagent-driven development is overkill). Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Delegation guidance (Sonnet 5 executor):** the ADR wording is the whole deliverable — write it yourself. The CLAUDE.md/README cross-reference edits are Haiku-delegable.

**Goal:** Close roadmap item 7 by *deciding* the single-user question and writing the decision down where future contributors (human or AI) will actually see it, so each new feature stops re-hardening an unexamined assumption.

**Architecture:** This plan recommends **Option A: declare single-user an explicit non-goal**, recorded as an ADR at `docs/adr/0001-single-user.md` and cross-referenced from `CLAUDE.md`. Rationale: every identity input is a process-level env var (`STEAM_ID`, `MCP_OAUTH_GITHUB_USER_IDS`, the Nintendo session files), every table assumes one owner (`ratings`, `tag_affinity`, `game_wishlist`, `meta`'s hardware preference, `nintendo_play_summary`), the deployment is one container serving one person, and multi-user has no driver — OAuth already supports multiple GitHub IDs but they all act on *the same* library, which is the intended "household" semantics. The alternative (Option B: add `user_id` columns now "while tables are small") buys nothing until a second *library* exists, and would tax every query, migration, and tool signature indefinitely. If the owner disagrees, the issue is where to say so — Task 1 explicitly pauses for that decision.

**Tech Stack:** Markdown only. No code, no migration, no new tests (one grep-check step guards the docs).

## Global Constraints

- The ADR must be honest that this is *revisable*: it documents what a future multi-user retrofit would touch, so the cost is known rather than mythologized.
- No code changes in this plan — if writing the ADR surfaces a cheap hardening (e.g. a comment), file it separately.

---

### Task 1: Confirm the decision with the owner

**Files:** none

- [ ] **Step 1:** Present the recommendation (Option A, non-goal) and the alternative (Option B, plan `user_id` now) to the repo owner for sign-off — in the driving conversation, or by opening the GitHub issue for comment. Do not proceed to Task 2 on a different option than the owner picked. If executing autonomously with no owner response, proceed with Option A (it is reversible — the ADR can be superseded; a premature `user_id` migration is much stickier).

### Task 2: Write the ADR

**Files:**
- Create: `docs/adr/0001-single-user.md`

- [ ] **Step 1: Write the ADR** with exactly this structure (content to write, not placeholders — flesh each bullet into a sentence or two):

```markdown
# ADR 0001: gamelib-mcp is single-user by design

Status: accepted (2026-07-02)

## Context
- Every identity input is process-level: STEAM_ID, PSN/Nintendo/Epic session
  material, DEKUDEALS_WISHLIST_URL are env vars or mounted files owned by one
  person. MCP_OAUTH_GITHUB_USER_IDS may list several GitHub accounts, but they
  authorize access to the *same* library (household semantics), not per-user data.
- Every table assumes one owner: ratings, tag_affinity, game_wishlist,
  nintendo_play_summary, and meta (hardware_preference, sync timestamps) have
  no user dimension.
- The audit (docs/audit/2026-07-01-repo-audit-and-roadmap.md, item 7) asked us
  to either write this down or plan a user_id column now.

## Decision
Single-user (single-library) is an explicit non-goal. New features MUST NOT
add per-user parameters, tables, or auth distinctions; they may assume "the
user" is the deployment owner.

## Consequences
- Positive: tool signatures, queries, migrations, and caching stay simple;
  the OAuth allowlist remains an access-control list, not an identity system.
- Negative / revisit triggers: a second person wanting their *own* ratings,
  taste profile, or wishlist requires superseding this ADR. The retrofit
  surface at that point: a users table; user_id on ratings, tag_affinity,
  game_wishlist, meta-per-user keys; per-user env/session storage for
  platform credentials; and AuthMiddleware mapping GitHub identity → user_id.
  Nothing else in the data model (games, game_platforms, enrichment) is
  per-user — the split is preferences-vs-catalog, and the catalog stays shared.
- The simplest multi-person accommodation — several GitHub IDs sharing one
  household library — already works today and is unaffected.
```

- [ ] **Step 2: Self-check** — the "retrofit surface" list must match the actual schema (verify against `gamelib_mcp/data/db/schema.py` `_V17_SCHEMA_DDL`+: confirm `ratings`, `tag_affinity`, `game_wishlist`, `meta`, `nintendo_play_summary` are the owner-scoped tables and no other table holds preference data; adjust the list to reality at execution time, e.g. if `game_prices`/`play_history` have landed, classify them — both are catalog/owner-scoped respectively; `play_history` joins the retrofit list).

- [ ] **Step 3: Commit**

```bash
git add docs/adr/0001-single-user.md
git commit -m "docs: ADR 0001 — single-user is an explicit non-goal"
```

### Task 3: Cross-reference from the docs agents actually read

**Files:**
- Modify: `CLAUDE.md` (one bullet under "Key Design Patterns": `**Single-user by design**: one deployment = one owner = one library; see docs/adr/0001-single-user.md before adding any per-user parameter or table.`)
- Modify: `README.md` (one line in whatever setup/overview section exists, pointing at the ADR)
- Modify: `docs/audit/2026-07-01-repo-audit-and-roadmap.md` (mark roadmap item 7 resolved with a pointer to the ADR, in the Status section's style)

- [ ] **Step 1: Make the three edits** (Haiku-delegable).
- [ ] **Step 2: Verify** — `grep -rn "adr/0001" CLAUDE.md README.md docs/` shows all three references; `.venv/bin/python -m pytest` still passes (docs-only change; the suite is the regression net for accidental file damage).
- [ ] **Step 3: Commit** — `docs: cross-reference single-user ADR; close audit roadmap item 7`.

## Explicit non-goals (YAGNI)

- No `user_id` column, no users table, no per-user meta namespace — that is the point of the ADR.
- No enforcement tooling (lint rules etc.); the CLAUDE.md bullet is the guardrail that AI contributors actually obey.
