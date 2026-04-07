"""Concurrent background enrichment with claim-aware worker families."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import httpx

from . import igdb
from .db import (
    STEAM_APP_ID,
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
    upsert_game_platform_enrichment,
)
from .hltb import get_hltb
from .metacritic import enrich_metacritic
from .opencritic import enrich_opencritic, is_configured as opencritic_is_configured
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


async def background_enrich() -> None:
    """Run enrichment families concurrently until all queues go quiescent."""
    logger.info("Background enrichment started")
    token = _SUPERVISOR_PROGRESS.set(_ProgressTracker())
    try:
        jobs = [
            ("store", _run_store_workers()),
            ("hltb", _run_hltb_workers()),
            ("protondb", _run_protondb_workers()),
            ("steamspy", _run_steamspy_workers()),
            ("metacritic", _run_metacritic_workers()),
            ("igdb", _run_igdb_workers()),
        ]
        if opencritic_is_configured():
            jobs.append(("opencritic", _run_opencritic_workers()))
        else:
            logger.info("Background enrichment skipping OpenCritic workers: OPENCRITIC_API_KEY is not configured")

        results = await asyncio.gather(*(job for _, job in jobs), return_exceptions=True)
        for (family, _), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception):
                logger.error("Background enrichment family failed: %s: %s", family, result)
        logger.info("Background enrichment complete: %r", results)
    finally:
        _SUPERVISOR_PROGRESS.reset(token)


async def _run_until_quiescent(run_batch: Callable[[], Awaitable[int]]) -> int:
    idle_polls = 0
    total = 0
    tracker = _SUPERVISOR_PROGRESS.get()
    observed_epoch = tracker.epoch if tracker is not None else 0
    while idle_polls < _IDLE_POLLS:
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
    return await _run_until_quiescent(_run_store_batch)


async def _run_hltb_workers() -> int:
    total = await _run_until_quiescent(_run_hltb_batch)
    logger.info("HLTB worker complete: processed %d rows", total)
    return total


async def _run_protondb_workers() -> int:
    return await _run_until_quiescent(_run_protondb_batch)


async def _run_steamspy_workers() -> int:
    return await _run_until_quiescent(_run_steamspy_batch)


async def _run_opencritic_workers() -> int:
    if not opencritic_is_configured():
        return 0
    return await _run_until_quiescent(_run_opencritic_batch)


async def _run_metacritic_workers() -> int:
    return await _run_until_quiescent(_run_metacritic_batch)


async def _run_igdb_workers() -> int:
    return await _run_until_quiescent(_run_igdb_batch)


async def _run_store_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_store(limit=50, stale_before=_claim_cutoff_iso())
    rows = await load_store_batch_rows(claimed_ids)
    if not rows:
        return 0

    semaphore = asyncio.Semaphore(_STORE_CONCURRENCY)
    start_gate = _RequestStartGate(_STORE_START_INTERVAL)

    async with httpx.AsyncClient() as client:
        async def enrich_one(row) -> int:
            async with semaphore:
                try:
                    await start_gate.wait_turn()
                    await enrich_game(row["appid"], client=client)
                except Exception as exc:
                    logger.debug("Store enrich failed for %s: %s", row["name"], exc)
                finally:
                    await _finalize_store_claim(row["game_platform_id"])
                return 1

        return sum(await asyncio.gather(*(enrich_one(row) for row in rows)))


async def _run_hltb_batch() -> int:
    claimed_ids = await claim_game_ids_for_hltb(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_hltb_batch_rows(claimed_ids)
    if not rows:
        return 0

    logger.info("HLTB worker claimed %d rows", len(rows))

    total = 0
    for index in range(0, len(rows), _BATCH_SIZE):
        batch = rows[index : index + _BATCH_SIZE]

        async def run_one(row) -> int:
            try:
                await get_hltb(row["game_id"], row["name"])
            except Exception as exc:
                logger.debug("HLTB enrich failed for %s: %s", row["name"], exc)
            finally:
                await clear_claim("games", "hltb_claimed_at", row["game_id"])
            return 1

        total += sum(await asyncio.gather(*(run_one(row) for row in batch)))
        await asyncio.sleep(_HLTB_DELAY)
    return total


async def _run_protondb_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_protondb(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_steam_platform_batch_rows(claimed_ids)
    if not rows:
        return 0

    processed = 0
    for row in rows:
        try:
            await get_protondb(row["appid"])
        except Exception as exc:
            logger.debug("ProtonDB enrich failed for %s: %s", row["name"], exc)
        finally:
            await _finalize_steam_claim(row["game_platform_id"], "protondb_claimed_at")
        processed += 1
        await asyncio.sleep(_PROTON_DELAY)
    return processed


async def _run_steamspy_batch() -> int:
    claimed_ids = await claim_steam_platform_ids_for_steamspy(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_steam_platform_batch_rows(claimed_ids)
    if not rows:
        return 0

    processed = 0
    for row in rows:
        try:
            await enrich_steamspy(row["appid"])
        except Exception as exc:
            logger.debug("SteamSpy enrich failed for %s: %s", row["name"], exc)
        finally:
            await _finalize_steam_claim(row["game_platform_id"], "steamspy_claimed_at")
        processed += 1
        await asyncio.sleep(_STEAMSPY_DELAY)
    return processed


async def _run_opencritic_batch() -> int:
    claimed_ids = await claim_game_platform_ids_for_opencritic(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_opencritic_batch_rows(claimed_ids)
    if not rows:
        return 0

    processed = 0
    for row in rows:
        success = True
        try:
            await enrich_opencritic(row["game_platform_id"], row["name"])
        except Exception as exc:
            success = False
            logger.debug("OpenCritic enrich failed for %s: %s", row["name"], exc)
        finally:
            await _finalize_platform_enrichment_claim(
                row["game_platform_id"],
                "opencritic_claimed_at",
                "opencritic_cached_at",
                success,
            )
        processed += 1
        await asyncio.sleep(_OPENCRITIC_DELAY)
    return processed


async def _run_metacritic_batch() -> int:
    claimed_ids = await claim_game_platform_ids_for_metacritic(limit=25, stale_before=_claim_cutoff_iso())
    rows = await load_metacritic_batch_rows(claimed_ids)
    if not rows:
        return 0

    processed = 0
    for row in rows:
        success = True
        try:
            await enrich_metacritic(row["game_platform_id"], row["name"], row["platform"])
        except Exception as exc:
            success = False
            logger.debug("Metacritic enrich failed for %s: %s", row["name"], exc)
        finally:
            await _finalize_platform_enrichment_claim(
                row["game_platform_id"],
                "metacritic_claimed_at",
                "metacritic_cached_at",
                success,
            )
        processed += 1
        await asyncio.sleep(_METACRITIC_DELAY)
    return processed


async def _run_igdb_batch() -> int:
    total = 0
    for _ in range(_IGDB_WORKER_CONCURRENCY):
        total += await igdb.backfill_missing_games(limit=10)
    return total


async def _finalize_store_claim(platform_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE steam_platform_data SET store_claimed_at = NULL WHERE game_platform_id = ?",
            (platform_id,),
        )
        await db.commit()


async def _finalize_steam_claim(
    platform_id: int,
    claim_column: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            f"UPDATE steam_platform_data SET {claim_column} = NULL WHERE game_platform_id = ?",
            (platform_id,),
        )
        await db.commit()


async def _finalize_platform_enrichment_claim(
    platform_id: int,
    claim_column: str,
    cached_column: str,
    success: bool,
) -> None:
    if success:
        await clear_claim("game_platform_enrichment", claim_column, platform_id, id_column="game_platform_id")
        return

    await upsert_game_platform_enrichment(
        platform_id,
        **{claim_column: None, cached_column: "FAILED"},
    )
