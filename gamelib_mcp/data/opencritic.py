"""OpenCritic API client — cross-platform review scores cached in game_platform_enrichment."""

import asyncio
import logging
import json
import os
import re
from datetime import datetime, timezone
import random
import unicodedata
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .db import get_db, upsert_game_platform_enrichment

logger = logging.getLogger(__name__)

OPENCRITIC_CACHE_DAYS = 30
_RECENT_RELEASE_WINDOW_DAYS = 180
_RECENT_SUCCESS_TTL_DAYS = 7
_NO_MATCH_COOLDOWN_DAYS = 7
_TRANSIENT_FAILURE_COOLDOWN_DAYS = 1
_BASE_DELAY_SECONDS = 2.0
_BASE_JITTER_SECONDS = 1.0
_RETRY_DELAYS = (4.0, 8.0, 16.0)
_SEARCH_URL = "https://api.opencritic.com/api/game/search"
_GAME_URL = "https://api.opencritic.com/api/game/{id}"
_OPENCRITIC_BASE_URL = "https://opencritic.com"
_SEARCH_PAGE_URL = f"{_OPENCRITIC_BASE_URL}/search"
_SEARCH_FALLBACK_URL = "https://html.duckduckgo.com/html/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
    )
}

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


def _is_opencritic_fresh(cached_at: str | None, release_date: str | None, now: datetime) -> bool:
    if not cached_at:
        return False
    if not release_date:
        return False
    try:
        fetched_at = datetime.fromisoformat(cached_at)
        released = datetime.fromisoformat(release_date).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if (now - released).days > _RECENT_RELEASE_WINDOW_DAYS:
        return True
    return (now - fetched_at).total_seconds() < _RECENT_SUCCESS_TTL_DAYS * 86400


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


async def discover_candidates(title: str) -> list[dict]:
    primary = await _discover_from_opencritic(title)
    if primary:
        return primary
    return await _discover_from_search_fallback(title)


def _candidate_to_export_url(candidate: dict) -> str:
    return _normalize_opencritic_url(candidate["url"]).rstrip("/") + "/export"


def _parse_opencritic_record(html: str, source_url: str) -> dict | None:
    state_match = re.search(r"window\.__STATE__\s*=\s*(\{.*?\})\s*;", html, re.S)
    if state_match is None:
        return None

    try:
        state = json.loads(state_match.group(1))
    except json.JSONDecodeError:
        return None

    required = (
        state.get("id"),
        state.get("topCriticScore"),
        state.get("tier"),
        state.get("percentRecommended"),
        state.get("numReviews"),
    )
    if any(value is None for value in required):
        return None

    canonical = state.get("url")
    return {
        "opencritic_id": int(state["id"]),
        "opencritic_url": canonical if canonical else source_url.removesuffix("/export"),
        "opencritic_score": int(round(float(state["topCriticScore"]))),
        "opencritic_tier": state["tier"],
        "opencritic_percent_rec": float(state["percentRecommended"]),
        "opencritic_num_reviews": int(state["numReviews"]),
    }


async def _sleep_with_jitter(base_seconds: float) -> None:
    await asyncio.sleep(base_seconds + random.uniform(0, _BASE_JITTER_SECONDS))


async def _fetch_opencritic_record(client: httpx.AsyncClient, url: str) -> dict:
    await _sleep_with_jitter(_BASE_DELAY_SECONDS)
    for retry_delay in (*_RETRY_DELAYS, None):
        try:
            response = await client.get(url)
            if response.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError("retryable", request=response.request, response=response)
            response.raise_for_status()
            parsed = _parse_opencritic_record(response.text, url)
            if parsed is None:
                return {"status": "parse_failed"}
            return {"status": "matched", "fields": parsed}
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code in (429, 500, 502, 503, 504) and retry_delay is not None:
                await _sleep_with_jitter(retry_delay)
                continue
            return {"status": "http_error"}
        except httpx.RequestError:
            if retry_delay is not None:
                await _sleep_with_jitter(retry_delay)
                continue
            return {"status": "http_error"}
    return {"status": "http_error"}


async def _discover_from_opencritic(title: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            response = await client.get(_SEARCH_PAGE_URL, params={"q": title})
            response.raise_for_status()
    except Exception as exc:
        logger.debug("OpenCritic primary discovery failed for %r: %s", title, exc)
        return []

    return _parse_discovery_candidates(response.text)


async def _discover_from_search_fallback(title: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            response = await client.get(
                _SEARCH_FALLBACK_URL,
                params={"q": f"site:opencritic.com/game {title}"},
            )
            response.raise_for_status()
    except Exception as exc:
        logger.debug("OpenCritic search fallback failed for %r: %s", title, exc)
        return []

    return _parse_discovery_candidates(response.text)


def _normalize_opencritic_url(value: str) -> str:
    return urljoin(f"{_OPENCRITIC_BASE_URL}/", value)


def _parse_discovery_candidates(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "/game/" not in href:
            continue

        url = _normalize_opencritic_url(href)
        if not url.startswith(f"{_OPENCRITIC_BASE_URL}/game/"):
            continue
        match = re.search(r"/game/(\d+)/", url)
        if match is None or url in seen_urls:
            continue

        title = anchor.get_text(" ", strip=True)
        if not title:
            continue

        candidates.append(
            {
                "title": title,
                "url": url,
                "opencritic_id": int(match.group(1)),
            }
        )
        seen_urls.add(url)

    return candidates


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
