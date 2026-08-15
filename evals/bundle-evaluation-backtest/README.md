# Bundle-evaluation backtest

Blind backtest of `skills/bundle-evaluation` against John's historical bundle purchases.
Read `backtest-report.md` for the findings; `inventory.md` for the test-set selection.

**The raw data files are deliberately not committed** — this is a public repo, and the
fixtures, ground-truth answer key, per-bundle results, taste-profile snapshot, and the
dated library dump (`work/owned_dated.tsv`) together fingerprint a real person's complete
game library, purchase history, and play behavior. They are regenerable against the live
Game Library MCP server:

1. `work/owned_dated.tsv` — the paginated `query_library` pull documented in
   `work/build_fixtures.py` (owned primary games with first-acquired dates).
2. `fixtures/*.json` — `python work/build_fixtures.py` (needs the TSV + `lineups/*.json`).
3. Ground truth — per-constituent all-platform playtime/ratings/farmed flags via
   `query_library`; the classification rules live in `work/score.py`'s docstring.
4. Blind evaluations — one fresh agent per fixture using `work/eval_prompt_template.md`.
5. `work/score.py` — builds the confusion matrices from `results/` + ground truth.

`lineups/*.json` stay committed: they are reconstructed public facts (bundle contents,
tier prices, dates, sources), not personal data.
