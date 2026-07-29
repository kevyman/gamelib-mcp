"""Platform-aware Metacritic scraper — writes to game_platform_enrichment.

The declarative surface (URL templates, the platform→slug map, the CSS
fallback selectors, cache TTL) comes from ``scrape_config.load_scrape_config
("metacritic")``. The JSON-LD ``bestRating == 100`` disambiguation guard stays
in code — it is what keeps user scores (bestRating=10) from being stored as
Metascores, and must not be healable away.
"""

import json
import logging
import re
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from .db import upsert_game_platform_enrichment
from .scrape_config import MetacriticScrapeConfig, fetch_allowlisted, load_scrape_config

logger = logging.getLogger(__name__)

METACRITIC_CACHE_DAYS = 30

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


def _is_fresh(cached_at: str | None, cache_days: int = METACRITIC_CACHE_DAYS) -> bool:
    if not cached_at:
        return False
    if cached_at == "FAILED":
        return True  # don't retry; background job skips FAILED entries
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(UTC) - dt).total_seconds() < cache_days * 86400
    except ValueError:
        return False


def _to_slug(name: str) -> str:
    """Convert game name to Metacritic URL slug."""
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"\s+", "-", slug.strip())
    slug = re.sub(r"-+", "-", slug)
    return slug


def _candidate_urls(slug: str, platform: str, config: MetacriticScrapeConfig | None = None) -> list[str]:
    if config is None:
        config = MetacriticScrapeConfig()

    query_value = config.platform_query_values.get(platform)
    base_url = config.game_url_template.format(slug=slug)
    urls: list[str] = []

    if query_value:
        urls.append(f"{base_url}?platform={query_value}")
        urls.append(config.platform_game_url_template.format(platform_slug=query_value, slug=slug))

    urls.append(base_url)
    return urls


def _extract_score(html: str, config: MetacriticScrapeConfig | None = None) -> int | None:
    """Extract the critic Metascore from a Metacritic game page. Pure."""
    if config is None:
        config = MetacriticScrapeConfig()

    soup = BeautifulSoup(html, "html.parser")

    # Try JSON-LD structured data first (more reliable than HTML scraping).
    # Metacritic exposes both the critic Metascore (0-100) and the user score
    # (0-10) as aggregateRating blocks; only the critic block has bestRating=100.
    # Without this guard we'd silently store user scores like 8 as a "Metascore".
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError as exc:
            # A page can carry several ld+json blocks; one unparseable block is
            # not a reason to abandon the others.
            logger.debug("Metacritic JSON-LD block unparseable: %s", exc)
            continue
        for entry in data if isinstance(data, list) else [data]:
            if not isinstance(entry, dict):
                continue
            rating = entry.get("aggregateRating") or {}
            value = rating.get("ratingValue")
            best = rating.get("bestRating")
            if value is None or best is None:
                continue
            try:
                if int(float(best)) != 100:
                    continue  # user score (bestRating=10), not the Metascore
                return int(float(value))
            except (TypeError, ValueError):
                continue

    # Fallback: critic-score-only CSS selectors. (.c-siteReviewScore is shared by
    # the user-score widget, so it is intentionally excluded here.)
    for selector in config.critic_score_selectors:
        el = soup.select_one(selector)
        if el:
            text = el.get_text(strip=True)
            m = re.search(r"\d+", text)
            if m:
                score = int(m.group())
                if 0 < score <= 100:
                    return score

    return None


async def _fetch_score_from_url(
    url: str, config: MetacriticScrapeConfig | None = None
) -> tuple[int | None, str]:
    """
    Fetch a Metacritic game page and extract the Metascore.
    Returns (score, final_url). Score is None if not found or page 404s.
    """
    try:
        async with httpx.AsyncClient(
            timeout=15,
            headers=_HEADERS,
        ) as client:
            resp = await fetch_allowlisted(client, url, provider="metacritic")
            if resp.status_code == 404:
                return None, url
            resp.raise_for_status()
            html = resp.text
            final_url = str(resp.url)
    except Exception as exc:
        logger.debug("Metacritic fetch failed for %s: %s", url, exc)
        return None, url

    return _extract_score(html, config), final_url


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

    config = await load_scrape_config("metacritic")

    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT metacritic_cached_at FROM game_platform_enrichment WHERE game_platform_id = ?",
            (game_platform_id,),
        )
    cached_at = row["metacritic_cached_at"] if row else None
    if _is_fresh(cached_at, config.cache_days):
        return None

    now = datetime.now(UTC).isoformat()
    slug = _to_slug(game_name)
    score = None
    final_url = config.game_url_template.format(slug=slug)
    for url in _candidate_urls(slug, platform, config):
        score, final_url = await _fetch_score_from_url(url, config)
        if score is not None:
            break

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
