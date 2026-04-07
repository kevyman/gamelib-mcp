"""OpenCritic API client — cross-platform review scores cached in game_platform_enrichment."""

import logging
import os
import re
from datetime import datetime, timezone
import unicodedata

import httpx

from .db import get_db, upsert_game_platform_enrichment

logger = logging.getLogger(__name__)

OPENCRITIC_CACHE_DAYS = 30
_SEARCH_URL = "https://api.opencritic.com/api/game/search"
_GAME_URL = "https://api.opencritic.com/api/game/{id}"

_EDITION_PATTERNS = {
    "remake": re.compile(r"\bremake\b", re.IGNORECASE),
    "remaster": re.compile(r"\bremaster(?:ed)?\b", re.IGNORECASE),
    "definitive edition": re.compile(r"\bdefinitive edition\b", re.IGNORECASE),
    "director's cut": re.compile(r"\bdirector'?s cut\b", re.IGNORECASE),
    "complete edition": re.compile(r"\bcomplete edition\b", re.IGNORECASE),
    "game of the year edition": re.compile(r"\b(?:game of the year|goty) edition\b", re.IGNORECASE),
    "anniversary edition": re.compile(r"\banniversary edition\b", re.IGNORECASE),
    "hd": re.compile(r"\bhd\b", re.IGNORECASE),
    "dx": re.compile(r"\bdx\b", re.IGNORECASE),
}


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


def _normalize_match_title(value: str) -> str:
    cleaned = unicodedata.normalize("NFKD", value)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.casefold().replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _extract_edition_tokens(value: str) -> set[str]:
    return {
        token
        for token, pattern in _EDITION_PATTERNS.items()
        if pattern.search(value)
    }


def _choose_match(source_title: str, candidates: list[dict]) -> dict | None:
    source_norm = _normalize_match_title(source_title)
    source_tokens = _extract_edition_tokens(source_title)

    scored: list[tuple[int, dict]] = []
    for candidate in candidates:
        title = candidate["title"]
        cand_norm = _normalize_match_title(title)
        cand_tokens = _extract_edition_tokens(title)

        score = 0
        if cand_norm == source_norm:
            score += 100
        if cand_tokens == source_tokens:
            score += 20
        elif source_tokens and cand_tokens != source_tokens:
            continue
        if cand_norm.startswith(source_norm) or source_norm.startswith(cand_norm):
            score += 5
        scored.append((score, candidate))

    scored = [item for item in scored if item[0] > 0]
    if not scored:
        return None

    scored.sort(key=lambda item: item[0], reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


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
