# SQLite write contention

Why this exists: `SQLITE_BUSY_SNAPSHOT` is not what `busy_timeout` protects
against, and mistaking one for the other silently froze a production Steam sync
for three days. The root `CLAUDE.md` keeps the two rules (retry only around
idempotent sync writers; `BEGIN IMMEDIATE` on the Steam chunk path); the
diagnosis moved here on 2026-09-01.

## The failure mode and the two layers of defense

**SQLite write contention**: WAL + `busy_timeout=30s` cover a writer waiting on a writer. They do NOT cover `SQLITE_BUSY_SNAPSHOT` — a transaction that READ the main DB then tries to WRITE it after another connection committed fails instantly, reported as "database is locked", with the busy handler deliberately skipped (no wait can refresh a stale snapshot). That is the platform-sync shape (read-then-write while background enrichment commits alongside), and it silently froze a production Steam sync for 3 days. Two layers of defense: `retry_on_write_contention` (data/db/__init__.py) restarts the transaction — it wraps the idempotent sync writers with 0.1/0.2/0.4/0.8s backoff, and only they, never anything that mints rows from partially-committed state — and `bulk_upsert_steam_library` additionally opens each chunk with `BEGIN IMMEDIATE`, because retries alone cannot save a long read-then-write transaction against writers that commit for longer than the whole backoff budget: during a full refresh the other five platforms commit continuously for ~90s, and Steam lost all 5 attempts (~1.5s) on 100% of full refreshes (2026-08-02/03) while succeeding instantly when synced alone. IMMEDIATE makes the chunk a writer from its first statement, so it queues under `busy_timeout` instead of building a snapshot a concurrent commit invalidates; the single-row sync upserts don't need it (their first statement already writes). `check_library`'s `sync.platform_error` reports the outcome so a silent multi-day failure surfaces without a manual `get_sync_status`.
