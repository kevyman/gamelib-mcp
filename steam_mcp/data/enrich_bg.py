"""Background enrichment — slowly populate Steam Store, HLTB, and ProtonDB for all games."""

import asyncio
import logging

from .db import get_db, set_meta, get_meta
from .steam_store import enrich_game
from .hltb import get_hltb
from .protondb import get_protondb
from .steamspy import enrich_steamspy

logger = logging.getLogger(__name__)

# Concurrency / rate limits
_STORE_DELAY = 1.5      # seconds between Steam Store API calls (rate-limited)
_HLTB_DELAY = 1.0       # seconds between HLTB batches
_PROTON_DELAY = 0.5     # ProtonDB is generous
_STEAMSPY_DELAY = 1.0   # SteamSpy rate limit
_BATCH_SIZE = 3


async def background_enrich() -> None:
    """Enrich all games that are missing store data, then HLTB + ProtonDB.

    Runs quietly in the background after startup. Prioritises played games
    (more likely to appear in recommendations / searches) then unplayed.
    Stores a checkpoint so restarts resume where they left off.
    """
    logger.info("Background enrichment started")

    # Phase 1: Steam Store (tags, genres, metacritic, review score)
    store_count = await _enrich_store()
    logger.info("Background enrichment — store phase done: %d games enriched", store_count)

    # Phase 2: HLTB for games that now have store data but no HLTB
    hltb_count = await _enrich_hltb()
    logger.info("Background enrichment — HLTB phase done: %d games enriched", hltb_count)

    # Phase 3: ProtonDB for games that still have no tier
    proton_count = await _enrich_protondb()
    logger.info("Background enrichment — ProtonDB phase done: %d games enriched", proton_count)

    # Phase 4: SteamSpy user-curated tags
    steamspy_count = await _enrich_steamspy()
    logger.info("Background enrichment — SteamSpy phase done: %d games enriched", steamspy_count)

    logger.info("Background enrichment complete — store=%d hltb=%d protondb=%d steamspy=%d",
                store_count, hltb_count, proton_count, steamspy_count)


async def _enrich_store() -> int:
    """Enrich all games missing store data, respecting Steam's rate limits."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT appid, name FROM games
                   WHERE store_cached_at IS NULL
                     AND is_farmed = 0
                   ORDER BY playtime_forever DESC
                   LIMIT 50"""
            )

        if not rows:
            break

        for row in rows:
            try:
                await enrich_game(row["appid"])
                count += 1
            except Exception as e:
                logger.debug("Store enrich failed for %s: %s", row["name"], e)
            await asyncio.sleep(_STORE_DELAY)

    return count


async def _enrich_hltb() -> int:
    """Backfill HLTB for games that have store data but no HLTB yet."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT appid, name FROM games
                   WHERE store_cached_at IS NOT NULL
                     AND hltb_cached_at IS NULL
                     AND is_farmed = 0
                   ORDER BY playtime_forever DESC
                   LIMIT 50"""
            )

        if not rows:
            break

        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i : i + _BATCH_SIZE]
            await asyncio.gather(
                *[get_hltb(r["appid"], r["name"]) for r in batch],
                return_exceptions=True,
            )
            count += len(batch)
            await asyncio.sleep(_HLTB_DELAY)

    return count


async def _enrich_protondb() -> int:
    """Backfill ProtonDB tiers."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT appid FROM games
                   WHERE store_cached_at IS NOT NULL
                     AND protondb_cached_at IS NULL
                     AND is_farmed = 0
                   ORDER BY playtime_forever DESC
                   LIMIT 50"""
            )

        if not rows:
            break

        for row in rows:
            try:
                await get_protondb(row["appid"])
                count += 1
            except Exception as e:
                logger.debug("ProtonDB enrich failed for appid %d: %s", row["appid"], e)
            await asyncio.sleep(_PROTON_DELAY)

    return count


async def _enrich_steamspy() -> int:
    """Backfill SteamSpy user-curated tags."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT appid, name FROM games
                   WHERE store_cached_at IS NOT NULL
                     AND steamspy_cached_at IS NULL
                     AND is_farmed = 0
                   ORDER BY playtime_forever DESC
                   LIMIT 50"""
            )
        if not rows:
            break
        for row in rows:
            try:
                await enrich_steamspy(row["appid"])
                count += 1
            except Exception as e:
                logger.debug("SteamSpy enrich failed for %s: %s", row["name"], e)
            await asyncio.sleep(_STEAMSPY_DELAY)
    return count
