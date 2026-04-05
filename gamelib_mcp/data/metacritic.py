"""Platform-aware Metacritic scraper — writes to game_platform_enrichment."""

import json
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .db import upsert_game_platform_enrichment

logger = logging.getLogger(__name__)

METACRITIC_CACHE_DAYS = 30

_GAME_URL = "https://www.metacritic.com/game/{slug}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_fresh(cached_at: str | None) -> bool:
    if not cached_at:
        return False
    if cached_at == "FAILED":
        return True  # don't retry; background job skips FAILED entries
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() < METACRITIC_CACHE_DAYS * 86400
    except ValueError:
        return False


def _to_slug(name: str) -> str:
    """Convert game name to Metacritic URL slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug


async def _fetch_score_from_url(url: str) -> tuple[int | None, str]:
    """
    Fetch a Metacritic game page and extract the Metascore.
    Returns (score, final_url). Score is None if not found or page 404s.
    """
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            headers=_HEADERS,
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                return None, url
            resp.raise_for_status()
            html = resp.text
            final_url = str(resp.url)
    except Exception as exc:
        logger.debug("Metacritic fetch failed for %s: %s", url, exc)
        return None, url

    soup = BeautifulSoup(html, "html.parser")

    # Try JSON-LD structured data first (more reliable than HTML scraping)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            rating = (data.get("aggregateRating") or {}).get("ratingValue")
            if rating is not None:
                return int(float(rating)), final_url
        except Exception:
            continue

    # Fallback: look for score in common Metacritic CSS selectors
    for selector in [
        '[data-testid="score-meta-critic"]',
        ".c-siteReviewScore",
        ".metascore_w",
    ]:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            m = re.search(r"\d+", text)
            if m:
                score = int(m.group())
                if 0 < score <= 100:
                    return score, final_url

    return None, final_url


async def enrich_metacritic(
    game_platform_id: int,
    game_name: str,
    platform: str,
) -> dict | None:
    """
    Scrape Metacritic score for game_name and cache in game_platform_enrichment.
    Returns enrichment dict or None.
    """
    from .db import get_db

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT metacritic_cached_at FROM game_platform_enrichment WHERE game_platform_id = ?",
            (game_platform_id,),
        )
    cached_at = row["metacritic_cached_at"] if row else None
    if _is_fresh(cached_at):
        return None

    now = datetime.now(timezone.utc).isoformat()
    slug = _to_slug(game_name)

    url = _GAME_URL.format(slug=slug)
    score, final_url = await _fetch_score_from_url(url)

    if score is None:
        await upsert_game_platform_enrichment(
            game_platform_id, metacritic_cached_at="FAILED"
        )
        return None

    fields = {
        "metacritic_score": score,
        "metacritic_url": final_url,
        "metacritic_cached_at": now,
    }
    await upsert_game_platform_enrichment(game_platform_id, **fields)
    return fields
