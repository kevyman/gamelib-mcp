"""Background enrichment for Steam-derived metadata."""

import asyncio
import logging

from .db import STEAM_APP_ID, get_db, upsert_game_platform_enrichment
from .steam_store import enrich_game
from .hltb import get_hltb
from .protondb import get_protondb
from .steamspy import enrich_steamspy
from .opencritic import enrich_opencritic
from .metacritic import enrich_metacritic

logger = logging.getLogger(__name__)

# Concurrency / rate limits
_STORE_DELAY = 1.5      # seconds between Steam Store API calls (rate-limited)
_HLTB_DELAY = 1.0       # seconds between HLTB batches
_PROTON_DELAY = 0.5     # ProtonDB is generous
_STEAMSPY_DELAY = 1.0   # SteamSpy rate limit
_OPENCRITIC_DELAY = 1.0  # seconds; public API, no key required
_METACRITIC_DELAY = 2.0  # scraping — be polite
_BATCH_SIZE = 3


async def background_enrich() -> None:
    """Run all enrichment phases: store, HLTB, ProtonDB, SteamSpy, OpenCritic, Metacritic."""
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

    opencritic_count = await _enrich_opencritic()
    logger.info("Background enrichment — OpenCritic phase done: %d rows enriched", opencritic_count)

    metacritic_count = await _enrich_metacritic()
    logger.info("Background enrichment — Metacritic phase done: %d rows enriched", metacritic_count)

    logger.info(
        "Background enrichment complete — store=%d hltb=%d protondb=%d steamspy=%d opencritic=%d metacritic=%d",
        store_count, hltb_count, proton_count, steamspy_count, opencritic_count, metacritic_count,
    )


async def _enrich_store() -> int:
    """Enrich all games missing store data, respecting Steam's rate limits."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT CAST(gpi.identifier_value AS INTEGER) AS appid, g.name
                   FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                   JOIN game_platform_identifiers gpi
                     ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                   LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                   WHERE spd.store_cached_at IS NULL
                     AND g.is_farmed = 0
                   ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                   LIMIT 50"""
                ,
                (STEAM_APP_ID,),
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
                """SELECT g.id AS game_id, g.name FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                   LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                   WHERE spd.store_cached_at IS NOT NULL
                     AND g.hltb_cached_at IS NULL
                     AND g.is_farmed = 0
                   ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                   LIMIT 50"""
            )

        if not rows:
            break

        for i in range(0, len(rows), _BATCH_SIZE):
            batch = rows[i : i + _BATCH_SIZE]
            await asyncio.gather(
                *[get_hltb(r["game_id"], r["name"]) for r in batch],
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
                """SELECT CAST(gpi.identifier_value AS INTEGER) AS appid
                   FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                   JOIN game_platform_identifiers gpi
                     ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                   LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                   WHERE spd.store_cached_at IS NOT NULL
                     AND spd.protondb_cached_at IS NULL
                     AND g.is_farmed = 0
                   ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                   LIMIT 50"""
                ,
                (STEAM_APP_ID,),
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
                """SELECT CAST(gpi.identifier_value AS INTEGER) AS appid, g.name
                   FROM games g
                   JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                   JOIN game_platform_identifiers gpi
                     ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
                   LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                   WHERE spd.store_cached_at IS NOT NULL
                     AND spd.steamspy_cached_at IS NULL
                     AND g.is_farmed = 0
                   ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                   LIMIT 50"""
                ,
                (STEAM_APP_ID,),
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


async def _enrich_opencritic() -> int:
    """Fetch OpenCritic scores for all platform rows missing opencritic data."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT gp.id AS game_platform_id, g.name
                   FROM game_platforms gp
                   JOIN games g ON g.id = gp.game_id
                   LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                   WHERE (gpe.opencritic_cached_at IS NULL)
                     AND g.is_farmed = 0
                   ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                   LIMIT 50"""
            )

        if not rows:
            break

        for row in rows:
            try:
                await enrich_opencritic(row["game_platform_id"], row["name"])
                count += 1
            except Exception as e:
                logger.debug("OpenCritic enrich failed for %s: %s", row["name"], e)
                try:
                    await upsert_game_platform_enrichment(
                        row["game_platform_id"], opencritic_cached_at="FAILED"
                    )
                except Exception:
                    pass
            await asyncio.sleep(_OPENCRITIC_DELAY)

    return count


async def _enrich_metacritic() -> int:
    """Scrape Metacritic scores for all platform rows missing metacritic data."""
    count = 0
    while True:
        async with get_db() as db:
            rows = await db.execute_fetchall(
                """SELECT gp.id AS game_platform_id, gp.platform, g.name
                   FROM game_platforms gp
                   JOIN games g ON g.id = gp.game_id
                   LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                   WHERE (gpe.metacritic_cached_at IS NULL)
                     AND g.is_farmed = 0
                   ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                   LIMIT 50"""
            )

        if not rows:
            break

        for row in rows:
            try:
                await enrich_metacritic(
                    row["game_platform_id"],
                    row["name"],
                    row["platform"],
                )
                count += 1
            except Exception as e:
                logger.debug("Metacritic enrich failed for %s: %s", row["name"], e)
                try:
                    await upsert_game_platform_enrichment(
                        row["game_platform_id"], metacritic_cached_at="FAILED"
                    )
                except Exception:
                    pass
            await asyncio.sleep(_METACRITIC_DELAY)

    return count
