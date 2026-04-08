"""OpenCritic scraping helpers cached in game_platform_enrichment."""

import asyncio
import html
import logging
import json
import re
from datetime import datetime, timezone
import random
import unicodedata
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .db import get_db, upsert_game_platform_enrichment

logger = logging.getLogger(__name__)

_RECENT_RELEASE_WINDOW_DAYS = 180
_RECENT_SUCCESS_TTL_DAYS = 7
_NO_MATCH_COOLDOWN_DAYS = 7
_TRANSIENT_FAILURE_COOLDOWN_DAYS = 1
_BASE_DELAY_SECONDS = 2.0
_BASE_JITTER_SECONDS = 1.0
_RETRY_DELAYS = (4.0, 8.0, 16.0)
_OPENCRITIC_BASE_URL = "https://opencritic.com"
_SEARCH_PAGE_URL = f"{_OPENCRITIC_BASE_URL}/search"
_SEARCH_API_URL = "https://api.opencritic.com/api/meta/search"
_SEARCH_FALLBACK_URL = "https://html.duckduckgo.com/html/"
_OPENCRITIC_API_BEARER = "Bearer R2tBRkdvUU9WSHpoUXpaSXVYa2g5cGU5NEFsWUgyeXQ="
_DDG_DELAY_SECONDS = 10.0
_DDG_403_COOLDOWN_SECONDS = 60.0
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
    return True


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
    if state_match is not None:
        try:
            state = json.loads(state_match.group(1))
        except json.JSONDecodeError:
            state = None
        else:
            record = _state_to_opencritic_record(state, source_url)
            if record is not None:
                return record

    script_match = re.search(
        r'<script id="serverApp-state" type="application/json">\s*(.*?)\s*</script>',
        html,
        re.S,
    )
    if script_match is None:
        return None

    payload_text = html_unescape_quotes(script_match.group(1))
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return None

    source_id_match = re.search(r"/game/(\d+)/", source_url)
    source_id = int(source_id_match.group(1)) if source_id_match is not None else None

    state: dict | None = None
    if source_id is not None:
        state = payload.get(f"game/{source_id}")
    if state is None:
        for key, value in payload.items():
            if key.startswith("game/") and isinstance(value, dict):
                state = value
                break
    if state is None:
        return None

    if source_id is not None and "id" not in state:
        state = {"id": source_id, **state}
    return _state_to_opencritic_record(state, source_url)


def html_unescape_quotes(value: str) -> str:
    return html.unescape(value).replace("&q;", '"')


def _state_to_opencritic_record(state: dict, source_url: str) -> dict | None:
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
            response = await client.get(
                _SEARCH_API_URL,
                params={"criteria": title},
                headers={
                    **_HEADERS,
                    "Accept": "application/json, text/plain, */*",
                    "Authorization": _OPENCRITIC_API_BEARER,
                    "Origin": _OPENCRITIC_BASE_URL,
                    "Referer": f"{_SEARCH_PAGE_URL}?q={quote_plus(title)}",
                },
            )
            response.raise_for_status()
    except Exception as exc:
        logger.debug("OpenCritic primary discovery failed for %r: %s", title, exc)
        return []

    return _parse_discovery_candidates(response.text)


async def _discover_from_search_fallback(title: str) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True, headers=_HEADERS) as client:
            await _sleep_with_jitter(_DDG_DELAY_SECONDS)
            response = await client.get(
                _SEARCH_FALLBACK_URL,
                params={"q": f"site:opencritic.com/game {title}"},
            )
            if response.status_code == 403:
                await _sleep_with_jitter(_DDG_403_COOLDOWN_SECONDS)
            response.raise_for_status()
    except Exception as exc:
        logger.debug("OpenCritic search fallback failed for %r: %s", title, exc)
        return []

    return _parse_discovery_candidates(response.text)


def _normalize_opencritic_url(value: str) -> str:
    return urljoin(f"{_OPENCRITIC_BASE_URL}/", value)


def _slugify_opencritic_title(value: str) -> str:
    cleaned = unicodedata.normalize("NFKD", value)
    cleaned = "".join(ch for ch in cleaned if not unicodedata.combining(ch))
    cleaned = cleaned.casefold().replace("&", " and ")
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)
    return cleaned.strip("-")


def _extract_duckduckgo_target(href: str) -> str:
    parsed = urlparse(urljoin("https://duckduckgo.com", href))
    query = parse_qs(parsed.query)
    target = query.get("uddg", [href])[0]
    return _normalize_opencritic_url(target)


def _parse_discovery_candidates(html: str) -> list[dict]:
    try:
        payload = json.loads(html)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, list):
        return [
            {
                "title": item["name"],
                "url": f"{_OPENCRITIC_BASE_URL}/game/{int(item['id'])}/{_slugify_opencritic_title(item['name'])}",
                "opencritic_id": int(item["id"]),
            }
            for item in payload
            if item.get("relation") == "game" and item.get("id") is not None and item.get("name")
        ]

    soup = BeautifulSoup(html, "html.parser")
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        url = _extract_duckduckgo_target(href)
        if "/game/" not in url:
            continue

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


async def _load_opencritic_context(game_platform_id: int) -> dict:
    async with get_db() as db:
        row = await db.execute_fetchone(
            """SELECT g.release_date, gpe.opencritic_cached_at
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
               WHERE gp.id = ?""",
            (game_platform_id,),
        )
    return dict(row) if row else {"release_date": None, "opencritic_cached_at": None}


async def _fetch_via_client(client: httpx.AsyncClient, match: dict) -> dict:
    return await _fetch_opencritic_record(client, _candidate_to_export_url(match))


async def enrich_opencritic(game_platform_id: int, game_name: str) -> dict:
    context = await _load_opencritic_context(game_platform_id)
    now = datetime.now(timezone.utc)
    if _is_opencritic_fresh(context["opencritic_cached_at"], context["release_date"], now):
        return {"status": "cached"}

    candidates = await discover_candidates(game_name)
    if not candidates:
        return {"status": "no_match"}

    match = _choose_match(game_name, candidates)
    if match is None:
        return {"status": "ambiguous"}

    async with httpx.AsyncClient(timeout=15, follow_redirects=True, headers=_HEADERS) as client:
        fetched = await _fetch_via_client(client, match)

    if fetched["status"] != "matched":
        return fetched

    fields = {
        **fetched["fields"],
        "opencritic_cached_at": now.isoformat(),
    }
    await upsert_game_platform_enrichment(game_platform_id, **fields)
    return {"status": "matched", "fields": fields}
