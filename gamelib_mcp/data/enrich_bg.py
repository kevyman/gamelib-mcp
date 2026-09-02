"""Concurrent background enrichment with claim-aware worker families."""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import Any

import httpx

from . import igdb, provider_health
from .db import (
    _claim_cutoff_iso,
    claim_game_ids_for_hltb,
    claim_game_platform_ids_for_metacritic,
    claim_game_platform_ids_for_opencritic,
    claim_steam_platform_ids_for_protondb,
    claim_steam_platform_ids_for_steamspy,
    claim_steam_platform_ids_for_store,
    clear_claim,
    get_db,
    load_hltb_batch_rows,
    load_metacritic_batch_rows,
    load_opencritic_batch_rows,
    load_steam_platform_batch_rows,
    load_store_batch_rows,
    recompute_tag_affinity,
    upsert_game_platform_enrichment,
)
from .hltb import get_hltb
from .metacritic import enrich_metacritic
from .opencritic import enrich_opencritic
from .protondb import get_protondb
from .steam_store import enrich_game
from .steamspy import enrich_steamspy

logger = logging.getLogger(__name__)

_STORE_CONCURRENCY = 4
_STORE_START_INTERVAL = 0.35
_HLTB_DELAY = 1.0
_PROTON_DELAY = 0.5
_STEAMSPY_DELAY = 1.0
_OPENCRITIC_DELAY = 1.0
_METACRITIC_DELAY = 2.0
_IGDB_WORKER_CONCURRENCY = 2
_BATCH_SIZE = 3
_IDLE_POLLS = 3
_IDLE_SLEEP_SECONDS = 1.0
_SUPERVISOR_PROGRESS: ContextVar["_ProgressTracker | None"] = ContextVar(
    "enrich_supervisor_progress",
    default=None,
)
_BACKGROUND_ENRICHMENT_PAUSE_COUNT = 0
_OPENCRITIC_SUCCESS_STATUSES = {
    "matched",
    "cached",
    "no_match",
    "ambiguous",
    "parse_failed",
    "http_error",
}
# The subset of those that means "the fetch did not work". They stay in
# _OPENCRITIC_SUCCESS_STATUSES because that set answers a different question —
# whether the claim was resolved rather than re-queued — but a run in which
# every row came back http_error is an outage, not an enrichment pass.
_OPENCRITIC_HEALTH_FAILURE_STATUSES = {"parse_failed", "http_error"}

# Per-provider outcome counters for one background_enrich() pass. Every batch
# function returns "rows handled" whether the fetch worked or threw, so without
# these a provider that broke outright (markup change, expired token) is
# indistinguishable at INFO from one that enriched everything.
#
# Counting exceptions here is not enough, and was the original bug: the
# providers keep the data layer's best-effort contract and swallow their own
# transport/parse failures (see data/provider_health.py), so a dead provider
# raised nothing and every row landed in `processed`. Each batch therefore
# diffs provider_health around the rows it just handled and folds the swallowed
# failures in beside the exceptions that still escape.
_PROVIDERS = ("store", "hltb", "protondb", "steamspy", "opencritic", "metacritic", "igdb")
_PROVIDER_LABELS = {
    "store": "Store",
    "hltb": "HLTB",
    "protondb": "ProtonDB",
    "steamspy": "SteamSpy",
    "opencritic": "OpenCritic",
    "metacritic": "Metacritic",
    "igdb": "IGDB",
}
_LAST_ERROR_CAP = 200
# Warn on a provider that is failing outright, not on the odd flaky fetch: three
# failures in a run, or half of everything it attempted.
_WARN_FAILURE_COUNT = 3
_WARN_FAILURE_RATIO = 0.5


def _empty_run_stats() -> dict[str, dict[str, Any]]:
    return {
        provider: {"processed": 0, "failed": 0, "last_error": None} for provider in _PROVIDERS
    }


_RUN_STATS: dict[str, dict[str, Any]] = _empty_run_stats()


def _reset_run_stats() -> None:
    global _RUN_STATS
    _RUN_STATS = _empty_run_stats()


def _record_processed(provider: str, count: int = 1) -> None:
    _RUN_STATS[provider]["processed"] += count


def _record_failure(provider: str, exc: BaseException | str) -> None:
    stats = _RUN_STATS[provider]
    stats["failed"] += 1
    detail = exc if isinstance(exc, str) else repr(exc)
    stats["last_error"] = detail[:_LAST_ERROR_CAP]


class _BatchOutcome:
    """One batch's real outcome, folded into this run's counters.

    A provider failure reaches a batch three ways: the provider SWALLOWED it and
    answered None (invisible here — that is what ``provider_health`` counts),
    it RETURNED a failure status (``record_reported_failure``), or it RAISED
    (``record_raised``). The batch's failed count is the explicit ones plus the
    swallowed ones it hasn't already accounted for, clamped to the rows actually
    attempted: one row can swallow several failures (Metacritic tries up to
    three candidate URLs; OpenCritic can fail bearer discovery and then its
    search fallback), and a lazy ``get_game_detail`` enrichment failing mid-pass
    adds to the same process-wide counter. The clamp keeps the ratio in the
    WARNING meaningful; it can still round a partly-broken row up to a whole
    failed one, which errs toward surfacing an outage.
    """

    def __init__(self, provider: str) -> None:
        self._provider = provider
        self._baseline = provider_health.failures(provider)
        self._explicit = 0
        self._absorbed = 0
        self._last_detail: str | None = None

    def record_raised(self, exc: BaseException) -> None:
        """An exception that escaped the provider's own swallow."""
        self._explicit += 1
        self._last_detail = repr(exc)

    def record_reported_failure(self, detail: str) -> None:
        """The provider RETURNED a failure (OpenCritic's http_error status).

        Absorbs one swallowed record, because the provider that reported this
        status is the same one that counted it on its way out — without that,
        one bad row would be counted twice.
        """
        self._explicit += 1
        self._absorbed += 1
        self._last_detail = detail

    def _failed(self) -> int:
        swallowed = max(0, provider_health.failures(self._provider) - self._baseline)
        return self._explicit + max(0, swallowed - self._absorbed)

    def _settle(self, failed: int, processed: int) -> None:
        stats = _RUN_STATS[self._provider]
        stats["failed"] += failed
        stats["processed"] += processed
        if failed:
            detail = self._last_detail or provider_health.snapshot().get(
                self._provider, {}
            ).get("last_error")
            stats["last_error"] = detail[:_LAST_ERROR_CAP] if detail else detail

    def settle_rows(self, rows: int) -> None:
        """For a batch that attempted ``rows`` items: the rest are processed."""
        failed = min(rows, self._failed())
        self._settle(failed, max(0, rows - failed))

    def settle_processed(self, processed: int) -> None:
        """For a batch that reports successes rather than attempts (IGDB).

        ``backfill_missing_games`` returns rows RESOLVED, not rows tried, so
        there is no attempt count to clamp against — the swallowed failures are
        the only evidence that the pass did anything but run out of work.
        """
        self._settle(self._failed(), processed)


def last_run_stats() -> dict[str, dict[str, Any]]:
    """Per-provider processed/failed/last_error for the most recent enrichment pass.

    Returns a copy, so a caller (health/status reporting) can hold or mutate the
    result without touching what the next pass reports.
    """
    return {provider: dict(values) for provider, values in _RUN_STATS.items()}


def _format_run_stats() -> str:
    return ", ".join(
        f"{provider} processed={values['processed']} failed={values['failed']}"
        for provider, values in _RUN_STATS.items()
    )


def _log_worker_summary(provider: str, rows: int) -> None:
    label = _PROVIDER_LABELS[provider]
    stats = _RUN_STATS[provider]
    failed = int(stats["failed"])
    logger.info("%s worker complete: processed %d rows, %d failed", label, rows, failed)
    attempted = int(stats["processed"]) + failed
    if failed > 0 and (failed >= _WARN_FAILURE_COUNT or failed / attempted >= _WARN_FAILURE_RATIO):
        logger.warning(
            "%s enrichment: %d of %d items failed this run; last error: %s",
            label,
            failed,
            attempted,
            stats["last_error"],
        )


class _RequestStartGate:
    """Serialize request starts to avoid bursty launches while allowing overlap."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval_seconds = interval_seconds
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait_turn(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            if now < self._next_allowed:
                await asyncio.sleep(self._next_allowed - now)
                now = loop.time()
            self._next_allowed = now + self._interval_seconds


class _ProgressTracker:
    def __init__(self) -> None:
        self._epoch = 0

    @property
    def epoch(self) -> int:
        return self._epoch

    def record_progress(self) -> int:
        self._epoch += 1
        return self._epoch


def pause_background_enrichment() -> None:
    """Prevent enrichment workers from claiming new batches."""
    global _BACKGROUND_ENRICHMENT_PAUSE_COUNT
    _BACKGROUND_ENRICHMENT_PAUSE_COUNT += 1


def resume_background_enrichment() -> None:
    """Release one pause request for enrichment workers."""
    global _BACKGROUND_ENRICHMENT_PAUSE_COUNT
    if _BACKGROUND_ENRICHMENT_PAUSE_COUNT > 0:
        _BACKGROUND_ENRICHMENT_PAUSE_COUNT -= 1


def is_background_enrichment_paused() -> bool:
    return _BACKGROUND_ENRICHMENT_PAUSE_COUNT > 0


async def background_enrich() -> None:
    """Run enrichment families concurrently until all queues go quiescent."""
    logger.info("Background enrichment started")
    _reset_run_stats()
    token = _SUPERVISOR_PROGRESS.set(_ProgressTracker())
    try:
        jobs = [
            ("store", _run_store_workers()),
            ("hltb", _run_hltb_workers()),
            ("protondb", _run_protondb_workers()),
            ("steamspy", _run_steamspy_workers()),
            ("opencritic", _run_opencritic_workers()),
            ("metacritic", _run_metacritic_workers()),
            ("igdb", _run_igdb_workers()),
        ]

        results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        processed_any = False
        for (family, _), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception):
                logger.error("Background enrichment family failed: %s: %s", family, result)
            elif result:
                processed_any = True
        logger.info(
            "Background enrichment complete: %r (%s)",
            results,
            _format_run_stats(),
        )
        # Tags may have changed (SteamSpy community tags, IGDB union); refresh the
        # tag_affinity table so the taste profile reflects the new vocabulary.
        if processed_any:
            try:
                await recompute_tag_affinity()
            except Exception as exc:
                logger.error("tag_affinity recompute after enrichment failed: %s", exc)
    finally:
        _SUPERVISOR_PROGRESS.reset(token)


async def _run_until_quiescent(run_batch: Callable[[], Awaitable[int]]) -> int:
    idle_polls = 0
    total = 0
    tracker = _SUPERVISOR_PROGRESS.get()
    observed_epoch = tracker.epoch if tracker is not None else 0
    while idle_polls < _IDLE_POLLS:
        if is_background_enrichment_paused():
            return total
        processed = await run_batch()
        total += processed
        if processed:
            idle_polls = 0
            if tracker is not None:
                observed_epoch = tracker.record_progress()
            continue
        idle_polls += 1
        if idle_polls >= _IDLE_POLLS and tracker is not None and tracker.epoch != observed_epoch:
            observed_epoch = tracker.epoch
            idle_polls = 0
            continue
        await asyncio.sleep(_IDLE_SLEEP_SECONDS)
    return total


async def _run_store_workers() -> int:
    total = await _run_until_quiescent(_run_store_batch)
    _log_worker_summary("store", total)
    return total


async def _run_hltb_workers() -> int:
    total = await _run_until_quiescent(_run_hltb_batch)
    _log_worker_summary("hltb", total)
    return total


async def _run_protondb_workers() -> int:
    total = await _run_until_quiescent(_run_protondb_batch)
    _log_worker_summary("protondb", total)
    return total


async def _run_steamspy_workers() -> int:
    total = await _run_until_quiescent(_run_steamspy_batch)
    _log_worker_summary("steamspy", total)
    return total


async def _run_opencritic_workers() -> int:
    total = await _run_until_quiescent(_run_opencritic_batch)
    _log_worker_summary("opencritic", total)
    return total


async def _run_metacritic_workers() -> int:
    total = await _run_until_quiescent(_run_metacritic_batch)
    _log_worker_summary("metacritic", total)
    return total


async def _run_igdb_workers() -> int:
    total = await _run_until_quiescent(_run_igdb_batch)
    _log_worker_summary("igdb", total)
    return total


async def _run_store_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_store(limit=50, stale_before=_claim_cutoff_iso())
    rows = await load_store_batch_rows(claimed_ids)
    if not rows:
        return 0

    semaphore = asyncio.Semaphore(_STORE_CONCURRENCY)
    start_gate = _RequestStartGate(_STORE_START_INTERVAL)
    outcome = _BatchOutcome("store")

    # Matches the per-request timeout every call through this client already
    # passes (steam_store's appdetails/appreviews fetches), so an unattended
    # background worker can never inherit httpx's silent default instead.
    async with httpx.AsyncClient(timeout=15) as client:
        async def enrich_one(row: sqlite3.Row) -> int:
            async with semaphore:
                try:
                    await start_gate.wait_turn()
                    await enrich_game(row["appid"], client=client)
                except Exception as exc:
                    logger.debug("Store enrich failed for %s: %s", row["name"], exc)
                    outcome.record_raised(exc)
                finally:
                    await _finalize_store_claim(row["game_platform_id"])
                return 1

        handled = sum(await asyncio.gather(*(enrich_one(row) for row in rows)))

    outcome.settle_rows(handled)
    return handled


async def _run_hltb_batch() -> int:
    claimed_ids = await claim_game_ids_for_hltb(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_hltb_batch_rows(claimed_ids)
    if not rows:
        return 0

    logger.info("HLTB worker claimed %d rows", len(rows))

    outcome = _BatchOutcome("hltb")
    total = 0
    for index in range(0, len(rows), _BATCH_SIZE):
        batch = rows[index : index + _BATCH_SIZE]

        async def run_one(row: sqlite3.Row) -> int:
            try:
                await get_hltb(row["game_id"], row["name"])
            except Exception as exc:
                logger.debug("HLTB enrich failed for %s: %s", row["name"], exc)
                outcome.record_raised(exc)
            finally:
                await _clear_claim_or_defer("games", "hltb_claimed_at", row["game_id"])
            return 1

        total += sum(await asyncio.gather(*(run_one(row) for row in batch)))
        await asyncio.sleep(_HLTB_DELAY)
    outcome.settle_rows(total)
    return total


async def _run_protondb_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_protondb(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_steam_platform_batch_rows(claimed_ids)
    if not rows:
        return 0

    outcome = _BatchOutcome("protondb")
    processed = 0
    for row in rows:
        try:
            await get_protondb(row["appid"])
        except Exception as exc:
            logger.debug("ProtonDB enrich failed for %s: %s", row["name"], exc)
            outcome.record_raised(exc)
        finally:
            await _finalize_steam_claim(row["game_platform_id"], "protondb_claimed_at")
        processed += 1
        await asyncio.sleep(_PROTON_DELAY)
    outcome.settle_rows(processed)
    return processed


async def _run_steamspy_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_steamspy(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_steam_platform_batch_rows(claimed_ids)
    if not rows:
        return 0

    outcome = _BatchOutcome("steamspy")
    processed = 0
    for row in rows:
        try:
            await enrich_steamspy(row["appid"])
        except Exception as exc:
            logger.debug("SteamSpy enrich failed for %s: %s", row["name"], exc)
            outcome.record_raised(exc)
        finally:
            await _finalize_steam_claim(row["game_platform_id"], "steamspy_claimed_at")
        processed += 1
        await asyncio.sleep(_STEAMSPY_DELAY)
    outcome.settle_rows(processed)
    return processed


async def _run_opencritic_batch() -> int:
    claimed_ids = await claim_game_platform_ids_for_opencritic(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_opencritic_batch_rows(claimed_ids)
    if not rows:
        return 0

    outcome = _BatchOutcome("opencritic")
    processed = 0
    for row in rows:
        success = True
        try:
            result = await enrich_opencritic(row["game_platform_id"], row["name"])
            success = result.get("status") in _OPENCRITIC_SUCCESS_STATUSES
            status = result.get("status")
            if status in _OPENCRITIC_HEALTH_FAILURE_STATUSES:
                # The provider answered with a status that MEANS the fetch did
                # not work. It stays "handled" for the claim (the row is not
                # re-queued forever), but it is not an enrichment that worked.
                outcome.record_reported_failure(
                    f"OpenCritic returned status {status!r} for {row['name']!r}"
                )
        except Exception as exc:
            success = False
            logger.debug("OpenCritic enrich failed for %s: %s", row["name"], exc)
            outcome.record_raised(exc)
        finally:
            await _finalize_platform_enrichment_claim(
                row["game_platform_id"],
                "opencritic_claimed_at",
                "opencritic_cached_at",
                success,
            )
        processed += 1
        await asyncio.sleep(_OPENCRITIC_DELAY)
    outcome.settle_rows(processed)
    return processed


async def _run_metacritic_batch() -> int:
    claimed_ids = await claim_game_platform_ids_for_metacritic(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_metacritic_batch_rows(claimed_ids)
    if not rows:
        return 0

    outcome = _BatchOutcome("metacritic")
    processed = 0
    for row in rows:
        success = True
        try:
            await enrich_metacritic(row["game_platform_id"], row["name"], row["platform"])
        except Exception as exc:
            success = False
            logger.debug("Metacritic enrich failed for %s: %s", row["name"], exc)
            outcome.record_raised(exc)
        finally:
            await _finalize_platform_enrichment_claim(
                row["game_platform_id"],
                "metacritic_claimed_at",
                "metacritic_cached_at",
                success,
            )
        processed += 1
        await asyncio.sleep(_METACRITIC_DELAY)
    outcome.settle_rows(processed)
    return processed


async def _run_igdb_batch() -> int:
    outcome = _BatchOutcome("igdb")
    total = 0
    try:
        for _ in range(_IGDB_WORKER_CONCURRENCY):
            total += await igdb.backfill_missing_games(limit=10)
    except Exception as exc:
        # backfill_missing_games handles IGDB's own failures internally; an
        # exception out of it is something else entirely (a DB error), but it
        # is still this provider's batch failing.
        outcome.record_raised(exc)
        outcome.settle_processed(total)
        raise
    outcome.settle_processed(total)
    return total


async def _finalize_store_claim(platform_id: int) -> None:
    if _defer_claim_release_if_paused("steam_platform_data", "store_claimed_at", platform_id):
        return

    try:
        async with get_db() as db:
            await db.execute(
                "UPDATE steam_platform_data SET store_claimed_at = NULL WHERE game_platform_id = ?",
                (platform_id,),
            )
            await db.commit()
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_lock(exc):
            _log_deferred_claim_release("steam_platform_data", "store_claimed_at", platform_id, exc)
            return
        raise


async def _finalize_steam_claim(
    platform_id: int,
    claim_column: str,
) -> None:
    if _defer_claim_release_if_paused("steam_platform_data", claim_column, platform_id):
        return

    try:
        async with get_db() as db:
            await db.execute(
                f"UPDATE steam_platform_data SET {claim_column} = NULL WHERE game_platform_id = ?",
                (platform_id,),
            )
            await db.commit()
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_lock(exc):
            _log_deferred_claim_release("steam_platform_data", claim_column, platform_id, exc)
            return
        raise


async def _finalize_platform_enrichment_claim(
    platform_id: int,
    claim_column: str,
    cached_column: str,
    success: bool,
) -> None:
    if success:
        await _clear_claim_or_defer(
            "game_platform_enrichment",
            claim_column,
            platform_id,
            id_column="game_platform_id",
        )
        return

    if _defer_claim_release_if_paused("game_platform_enrichment", claim_column, platform_id):
        return

    try:
        await upsert_game_platform_enrichment(
            platform_id,
            **{claim_column: None, cached_column: "FAILED"},
        )
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_lock(exc):
            _log_deferred_claim_release("game_platform_enrichment", claim_column, platform_id, exc)
            return
        raise


async def _clear_claim_or_defer(
    table: str,
    claim_column: str,
    row_id: int,
    *,
    id_column: str = "id",
) -> None:
    if _defer_claim_release_if_paused(table, claim_column, row_id):
        return

    try:
        await clear_claim(table, claim_column, row_id, id_column=id_column)
    except sqlite3.OperationalError as exc:
        if _is_transient_sqlite_lock(exc):
            _log_deferred_claim_release(table, claim_column, row_id, exc)
            return
        raise


def _defer_claim_release_if_paused(table: str, claim_column: str, row_id: int) -> bool:
    if not is_background_enrichment_paused():
        return False

    logger.info(
        "Deferring enrichment claim release for %s.%s row %s while enrichment is paused",
        table,
        claim_column,
        row_id,
    )
    return True


def _is_transient_sqlite_lock(exc: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _log_deferred_claim_release(
    table: str,
    claim_column: str,
    row_id: int,
    exc: sqlite3.OperationalError,
) -> None:
    logger.info(
        "Deferring enrichment claim release for %s.%s row %s after transient SQLite lock: %s",
        table,
        claim_column,
        row_id,
        exc,
    )
