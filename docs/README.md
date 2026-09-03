# docs/

| Directory | What it is |
| --- | --- |
| `adr/` | **Source of truth for decisions.** Numbered, status-stamped architecture decision records. A decision changes here first. |
| `patterns/` | Implementation rules moved out of the root `CLAUDE.md` (2026-09-01) so they load on demand. Current-state truth for how a rule is implemented; the decision behind it is in `adr/`. |
| `audit/` | Dated whole-repo audits, newest last. Each is a snapshot of measured state plus a ranked to-do list — accurate as of its date, not maintained afterwards. |
| `plans/`, `superpowers/` | Historical pre-implementation design docs, kept as a record of how a feature was reasoned about. **Not current-state truth** — read the code, the ADRs and `patterns/` for that. |

Loose files at this level (e.g. `2026-07-03-igdb-wishlist-backfill-resolution-gaps.md`) are one-off investigation notes, dated and historical like `plans/`.
