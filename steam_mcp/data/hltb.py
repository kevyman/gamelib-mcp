"""Lazy HowLongToBeat fetch + semaphore-limited background pre-warm."""

import asyncio
import logging
from datetime import datetime, timezone

from howlongtobeatpy import HowLongToBeat

from .db import get_db

HLTB_CACHE_DAYS = 30
_semaphore = asyncio.Semaphore(3)
logger = logging.getLogger(__name__)


async def get_hltb(appid: int, name: str) -> dict | None:
    """
    Lazy-fetch HLTB data for a game. Caches in DB.
    Returns dict with hltb_main, hltb_extra, hltb_complete or None.
    """
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT hltb_main, hltb_extra, hltb_complete, hltb_cached_at FROM games WHERE appid = ?",
            (appid,),
        )

    if row:
        cached_at = row["hltb_cached_at"]
        if cached_at == "FAILED" or _is_fresh(cached_at, HLTB_CACHE_DAYS):
            if cached_at == "FAILED":
                return None
            return {
                "hltb_main": row["hltb_main"],
                "hltb_extra": row["hltb_extra"],
                "hltb_complete": row["hltb_complete"],
            }

    return await _fetch_and_cache(appid, name)


async def _fetch_and_cache(appid: int, name: str) -> dict | None:
    async with _semaphore:
        try:
            results = await HowLongToBeat().async_search(name)
            now = datetime.now(timezone.utc).isoformat()

            if not results:
                await _cache_result(appid, None, None, None, "FAILED")
                return None

            # Pick closest match by similarity score
            best = max(results, key=lambda e: e.similarity)
            main = best.main_story
            extra = best.main_extra
            comp = best.completionist

            await _cache_result(appid, main, extra, comp, now)
            return {"hltb_main": main, "hltb_extra": extra, "hltb_complete": comp}
        except Exception as e:
            logger.warning("HLTB fetch failed for %s (%d): %s", name, appid, e)
            await _cache_result(appid, None, None, None, "FAILED")
            return None


async def _cache_result(
    appid: int,
    main: float | None,
    extra: float | None,
    comp: float | None,
    cached_at: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            """UPDATE games SET hltb_main = ?, hltb_extra = ?, hltb_complete = ?, hltb_cached_at = ?
               WHERE appid = ?""",
            (main, extra, comp, cached_at, appid),
        )
        await db.commit()


async def prewarm_hltb() -> None:
    """
    Background task: pre-warm HLTB for unplayed games with store data,
    ordered by steam_review_score DESC. Rate-limited by semaphore + 1s delay.
    """
    logger.info("HLTB pre-warm started")
    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.appid, g.name FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE COALESCE(gp.playtime_minutes, 0) = 0
                 AND g.tags IS NOT NULL
                 AND (g.hltb_cached_at IS NULL)
               ORDER BY g.steam_review_score DESC NULLS LAST
               LIMIT 500"""
        )

    batch_size = 3
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        tasks = [get_hltb(r["appid"], r["name"]) for r in batch]
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(1)

    logger.info("HLTB pre-warm complete: processed %d games", len(rows))


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at or cached_at == "FAILED":
        return False
    try:
        from datetime import timedelta
        dt = datetime.fromisoformat(cached_at)
        age = datetime.now(timezone.utc) - dt
        return age.total_seconds() < days * 86400
    except ValueError:
        return False
