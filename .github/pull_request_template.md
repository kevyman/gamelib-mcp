<!-- Title = the squash commit subject: imperative, ≤ 72 chars, no period.
     Body = the squash commit message. Keep it true after the fix pass. -->

## Why

<!-- The problem or decision, one paragraph. Link the issue / ADR / audit item. -->

## What changed

<!-- Bullets a reviewer can verify against the diff. Name invariants touched. -->

## How it was verified

- [ ] `ruff`, `mypy`, full `pytest` green locally (3.12)
- [ ] Same-family self-review done before requesting Codex (`/code-review`)
- [ ] `/security-review` (only if auth, HTTP surface, sessions, webhooks or subprocess changed)

## Review scope for Codex

<!-- Where a second model should look hardest: new write paths, migrations,
     concurrency, anything AGENTS.md "Code Review Rules" names. Or: "nothing
     unusual — routine". -->

## Codex review outcome

<!-- Filled after the single review pass, before merge:
     findings N · fixed F (commit sha) · refuted R (thread links) · P0 0 -->
