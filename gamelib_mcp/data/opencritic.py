"""OpenCritic API client — cross-platform review scores cached in game_platform_enrichment."""

import logging
import os
from datetime import datetime, timezone

import httpx

from .db import get_db, upsert_game_platform_enrichment

logger = logging.getLogger(__name__)

OPENCRITIC_CACHE_DAYS = 30
_SEARCH_URL = "https://api.opencritic.com/api/game/search"
_GAME_URL = "https://api.opencritic.com/api/game/{id}"


def is_configured() -> bool:
    return bool(os.getenv("OPENCRITIC_API_KEY"))


def _is_fresh(cached_at: str | None) -> bool:
    if not cached_at:
        return False
    if cached_at == "FAILED":
        return True  # don't retry; background job will skip FAILED entries
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() < OPENCRITIC_CACHE_DAYS * 86400
    except ValueError:
        return False


async def enrich_opencritic(game_platform_id: int, game_name: str) -> dict | None:
    """
    Fetch OpenCritic score for game_name and cache in game_platform_enrichment.
    Returns enrichment dict or None on failure.
    """
    if not is_configured():
        logger.info("OpenCritic enrich skipped for %r: OPENCRITIC_API_KEY is not configured", game_name)
        return None

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT opencritic_cached_at FROM game_platform_enrichment WHERE game_platform_id = ?",
            (game_platform_id,),
        )
    cached_at = row["opencritic_cached_at"] if row else None
    if _is_fresh(cached_at):
        return None

    now = datetime.now(timezone.utc).isoformat()

    oc_id = await _search_game(game_name)
    if oc_id is None:
        await upsert_game_platform_enrichment(game_platform_id, opencritic_cached_at="FAILED")
        return None

    data = await _fetch_game(oc_id)
    if data is None:
        await upsert_game_platform_enrichment(game_platform_id, opencritic_cached_at="FAILED")
        return None

    score = data.get("topCriticScore")
    tier = data.get("tier")
    percent_rec = data.get("percentRecommended")

    # topCriticScore can be -1 when no reviews yet
    if score is not None and score < 0:
        score = None

    fields = {
        "opencritic_id": oc_id,
        "opencritic_score": score,
        "opencritic_tier": tier,
        "opencritic_percent_rec": percent_rec,
        "opencritic_cached_at": now,
    }
    await upsert_game_platform_enrichment(game_platform_id, **fields)
    return fields


async def _search_game(name: str) -> int | None:
    api_key = os.getenv("OPENCRITIC_API_KEY")
    if not api_key:
        logger.info("OpenCritic search skipped for %r: OPENCRITIC_API_KEY is not configured", name)
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _SEARCH_URL,
                params={"criteria": name},
                headers={"x-access-token": api_key},
            )
            resp.raise_for_status()
            results = resp.json()
    except Exception as exc:
        logger.debug("OpenCritic search failed for %r: %s", name, exc)
        return None

    if not results:
        return None

    # Pick best name match
    from .db import extract_best_fuzzy_key
    choices = {item["id"]: item["name"] for item in results if "id" in item and "name" in item}
    best_id = extract_best_fuzzy_key(name, choices, cutoff=70)
    return best_id if best_id is not None else results[0].get("id")


async def _fetch_game(oc_id: int) -> dict | None:
    api_key = os.getenv("OPENCRITIC_API_KEY")
    if not api_key:
        return None

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _GAME_URL.format(id=oc_id),
                headers={"x-access-token": api_key},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        logger.debug("OpenCritic game fetch failed for id %d: %s", oc_id, exc)
        return None
