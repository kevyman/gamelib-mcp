# Testing conventions

Why this exists: the root `CLAUDE.md` keeps the three test conventions as
one-line rules, because breaking one of them is what makes the suite slow or
flaky. The reasoning behind each rule — how much time it saved, which failure
mode it was written against — lives here, moved out of `CLAUDE.md` on
2026-09-01 so it is read when working on tests rather than in every session.

## The sandbox gotcha

Test gotcha: in Codex sandboxing, aiosqlite tests can hang at `aiosqlite.connect()` or early migration setup (the thread-safe event-loop callback never resumes the awaiting coroutine). Re-run outside the sandbox before changing test fixtures or DB paths. DB tests use temp SQLite files; no checked-in `data/gamelib.db` needed.

## The three conventions

Three conventions keep the suite fast and honest about time, all in `tests/conftest.py`:
- **Migrate once, copy per test.** A session fixture runs the real migration chain into a template database; `ToolDBTestCase` copies the finished file and marks it ready. Never re-add a per-test `init_db()` — that alone was ~40% of a serial run.
- **`DEADLOCK_TIMEOUT` for every `wait_for`.** The async orchestration tests wait on events set by the code under test, with no real I/O in between, so a wall-clock budget measures machine load and nothing else. Tight budgets (0.1s) were the suite's main source of false failures. Never assert liveness with a timed `asyncio.sleep` either — use an event that is never set. Backstopping all of it, `faulthandler_timeout = 300` (pyproject) dumps every thread's stack when any single test runs past 5 minutes — a hang that slips past the guards leaves a diagnosis in the CI log instead of a silently cancelled job (issue #155: two 10-minute deploy burns with zero evidence).
- **`virtual_clock(module)` for anything that backs off.** The Steam and IGDB request gates sleep for real (up to 10s after a 429). Serve them a fake clock and assert `clock.sleeps` instead of sitting through the delay.

## Parallelism

`pytest-xdist` runs the suite across all cores by default (`addopts` in `pyproject.toml`). It only works because tests share no mutable state — each gets its own temp DB, and module-level globals are per-process.
