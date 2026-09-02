# Review and merge: one cross-model pass, one fix pass, squash

Why this exists: shipping here is automated end to end by a Claude Code
session, and the one step that has repeatedly paid for itself is a single
review by a model from another family (Codex) before the squash merge. This
file records what the process is, why it is shaped this way, the settings
that make it hold, and what the prior art says. The runbook a session
follows is `.claude/skills/ship/SKILL.md`; the review standards Codex
applies are `AGENTS.md` → "Code Review Rules".

## The process

1. **Preflight with the cheap reviewer.** Gates as CI runs them, then
   `/code-review` (same model family as the author) and, for security-adjacent
   diffs, `/security-review`. Same-family review finds the obvious; spending
   it first means the cross-model pass is spent on blind spots.
2. **One PR, one theme, reviewable size.** Title is the squash subject, body is
   the squash message, "Review scope for Codex" says where the risk is.
3. **One Codex pass.** Opening a PR for review starts the review, given
   two Codex settings: the repository's **Automatic reviews** toggle
   (chatgpt.com/codex/settings/code-review) and a Codex cloud
   **environment** for the repo (chatgpt.com/codex/cloud/settings/environments).
   `@codex review` is the manual fallback — it runs without the
   environment — posted only when the automatic review is not going to
   happen (nothing after 10 minutes, or Codex's "create an environment"
   reply), since both firing would be two reviews. (Measured: the toggle
   was off until 2026-09-02, so #162, #166 and #167 needed the comment;
   #168 then hit the missing environment.) Codex reads `AGENTS.md`,
   reviews the diff statically, posts P0/P1 inline, reacts 👍 when it has
   nothing.
4. **Refute or fix, once.** Every finding gets the test that would prove it.
   Fails → fix. Cannot be made to fail → refuted on the thread with evidence.
   Reply, resolve. No second full review; one targeted re-check only for a
   large or security-touching fix.
5. **Green, then squash.** CI on the fix commit, then a squash merge whose
   message is the PR's Why + What, branch deleted, subscription dropped.

Record on the PR: `findings N · fixed F · refuted R`. The 2026-09-01 audit
counted four PRs whose tests cite a Codex finding (#141, #142, #152, #154);
the next audit can count hits, misses and refutations per PR from those
lines and decide whether the pass still earns its place.

## Why one pass, and why a different model

- **Blind spots are a family property.** The failure classes that survive
  same-model review — silent invariant violations, spec drift, edge-case
  omissions — are exactly what a model with a different training distribution
  sees; cross-vendor review is the recommended shape in the prior art below,
  and this repo's own history agrees (the ordinal near-miss guard in
  `record_assessment`, the packaging-validity P1, the wishlist promotion
  collision were all Codex catches).
- **Precision beats recall for a maintainer of one.** The 2026 literature on
  LLM defect discovery reports that the majority of plausible candidates die
  under adversarial refutation (the *Refute-or-Promote* study killed ~79–83%
  before disclosure, and found ten reviewers unanimously endorsing a
  non-existent vulnerability that only an empirical test disproved). Repeated
  passes raise recall a little and raise the refutation burden a lot; the
  vendor claims of "first pass 60%, second 25%" are unverified marketing, but
  even taken at face value they describe steep diminishing returns. Hence:
  one pass, and every finding must be reproduced by a test before it changes
  code.
- **The reviewer gets smarter through `AGENTS.md`, not through repetition.**
  Codex's documented mechanism for repo-specific standards is the
  `## Code Review Rules` section; without it the pass is generic. This repo
  had none until 2026-09-01.
- **Timing.** Review a final diff: after gates and the same-family pass, and
  never push while the review is running (a new commit invalidates the
  reviewed one and invites a second pass).

## Repository settings that make it hold (set once, GitHub UI)

- **Codex → Code review → Automatic reviews: on** (it is; the review banner
  confirms it).
- **Pull requests:** allow squash merging only; default squash message
  "Pull request title and description"; automatically delete head branches.
- **Branch protection on `main`:** require status checks
  `Test (py3.11)` and `Test (py3.12)` to pass; require conversation
  resolution before merging (an unresolved Codex thread blocks the merge,
  which is the whole point); no required approvals (single maintainer);
  include administrators so the rule applies to the session's token.
- Optional: enable auto-merge on the repo, then `enable_pr_auto_merge`
  (SQUASH) after the fix push turns "wait for green" into "GitHub merges when
  green". Not enabled by default here because the P0 stop rule wants a human
  on the merge for auth/data paths.

## What was considered and rejected

- **Same-model second pass** (Claude reviews Claude's PR on GitHub): already
  covered more cheaply by `/code-review` before the PR exists.
- **Consensus of three models per PR:** more findings to refute, no evidence
  the third catches what the second missed at this repo's size.
- **Review-fix loops until zero findings:** converges on refutations, not
  fixes; the loop is capped at one fix pass plus one targeted re-check.
- **Letting `@codex fix` push the fix:** it removes the refute-with-a-test
  gate and mixes reviewer and author; Codex proposes, the session verifies.
- **Reviewing draft PRs:** a review of a moving diff is a review of nothing.

## Prior art and sources

- OpenAI, *Review GitHub pull requests with Codex* — triggers (`@codex
  review`, `@codex security review`), automatic reviews, `AGENTS.md`
  "Code Review Rules", P0/P1 display, 👀/👍 reactions:
  https://learn.chatgpt.com/docs/third-party/github
- Salman Ali Banani, *A Two-Agent PR Workflow: Claude Writes, Codex Reviews*
  (2026-07) — one review pass, one fix pass, documented non-fixes, merge:
  https://salmanalibanani.com/2026/07/04/a-two-agent-pr-workflow-claude-writes-codex-reviews/
- Easton, *OpenAI Codex PR Review: @codex review and Human Triage* (2026-07)
  — P0/P1/P2 handling table, "inspect the diff line before acting":
  https://eastondev.com/blog/en/posts/ai/20260709-codex-ai-code-review-pr/
- Daniel Vaughan, *Cross-Model Adversarial Review* (2026-03) — builder/critic
  in separate sessions, fresh critic per retry, circuit breakers:
  https://codex.danielvaughan.com/2026/03/28/cross-model-adversarial-review/
- *Refute-or-Promote: An Adversarial Stage-Gated Multi-Agent Review
  Methodology for High-Precision LLM-Assisted Defect Discovery* (arXiv
  2604.19049) — refutation gates, empirical validation as the final filter:
  https://arxiv.org/abs/2604.19049
- GitHub Docs, *Configuring commit squashing for pull requests* — default
  squash message from PR title and description:
  https://docs.github.com/articles/configuring-commit-squashing-for-pull-requests
