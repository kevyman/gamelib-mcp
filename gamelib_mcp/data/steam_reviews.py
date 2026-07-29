"""Scrape Steam community reviews for the configured user.

The declarative surface (URL templates, box/link/thumb selectors, the appid
regex, pagination cap) comes from ``scrape_config.load_scrape_config
("steam_reviews")``; the thumbs+community score fusion in ``_compute_score``
and the appid→game_id DB join stay in code.
"""

import logging
import os
import re
from datetime import UTC, datetime

import httpx
from bs4 import BeautifulSoup

from .db import STEAM_APP_ID, get_db
from .scrape_config import (
    SteamReviewsScrapeConfig,
    fetch_allowlisted,
    load_scrape_config,
)

_STEAM_PROFILE_ID = os.getenv("STEAM_PROFILE_ID", "")
BASE_URL = f"https://steamcommunity.com/id/{_STEAM_PROFILE_ID}/recommended/"
logger = logging.getLogger(__name__)


async def sync_steam_reviews() -> dict:
    """
    Scrape paginated Steam reviews, upsert into ratings.

    Normalized score combines the user's thumbs-up/down with the game's
    community review score (1–9 enum from Steam Store API) to produce a
    1–10 rating:
      - Thumbs up  → 6–10, scaled by community score (higher = better)
      - Thumbs down → 1–4, scaled by community score (lower  = worse)
      - No community score → fallback 7.5 (up) / 2.5 (down)

    Returns scrape/upsert *volume* alongside the resulting distinct-game count,
    which can differ: several scraped review rows (paginated re-appearances, or
    distinct appids that reconcile to one game) collapse onto a single
    UNIQUE(game_id, source) rating. ``scraped_rows``/``rows_upserted`` are the
    raw volume; ``distinct_games_after`` is the authoritative count of games
    carrying a steam_review rating post-sync — the number get_ratings and
    get_taste_profile report — so a 52-vs-27 gap reads as dedup, not lost writes.
    """
    config = await load_scrape_config("steam_reviews")
    reviews = await _scrape_all_pages(config)
    synced = 0
    now = datetime.now(UTC).isoformat()

    # Pre-fetch game ids and community review scores for all reviewed games
    game_info: dict[int, dict] = {}  # appid -> {id, steam_review_score}
    async with get_db() as db:
        for review in reviews:
            row = await db.execute_fetchone(
                """SELECT gp.game_id AS id, spd.steam_review_score
                   FROM game_platform_identifiers gpi
                   JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                   LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                   WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
                   LIMIT 1""",
                (STEAM_APP_ID, str(review["appid"])),
            )
            if row:
                game_info[review["appid"]] = {
                    "id": row["id"],
                    "steam_review_score": row["steam_review_score"],
                }

    async with get_db() as db:
        for review in reviews:
            appid = review["appid"]
            info = game_info.get(appid)
            if info is None:
                continue
            vote = review["vote"]  # 1 (up) or -1 (down)
            community = info["steam_review_score"]
            normalized = _compute_score(vote, community)

            await db.execute(
                """INSERT INTO ratings (game_id, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, 'steam_review', ?, ?, ?, ?)
                   ON CONFLICT(game_id, source) DO UPDATE SET
                       raw_score = excluded.raw_score,
                       normalized_score = excluded.normalized_score,
                       review_text = excluded.review_text,
                       synced_at = excluded.synced_at""",
                (info["id"], float(vote), normalized, review.get("text", ""), now),
            )
            synced += 1

        await db.commit()

        distinct_row = await db.execute_fetchone(
            "SELECT COUNT(DISTINCT game_id) AS c FROM ratings WHERE source = 'steam_review'"
        )

    return {
        "scraped_rows": len(reviews),
        "rows_upserted": synced,
        "distinct_games_after": distinct_row["c"] if distinct_row else 0,
    }


def _compute_score(vote: int, community_score: int | None) -> float:
    """Combine thumbs-up/down with community review score (1–9) into a 1–10 rating.

    Thumbs up  → 6 + (community - 1) * 0.5  → range 6–10
    Thumbs down → 1 + (community - 1) * 0.375 → range 1–4
    """
    if vote == 1:
        if community_score and 1 <= community_score <= 9:
            return round(6 + (community_score - 1) * 0.5, 1)
        return 7.5  # fallback: midpoint of 6–10
    else:
        if community_score and 1 <= community_score <= 9:
            return round(1 + (community_score - 1) * 0.375, 1)
        return 2.5  # fallback: midpoint of 1–4


def _page_url(page: int, config: SteamReviewsScrapeConfig) -> str:
    user = os.getenv("STEAM_PROFILE_ID", _STEAM_PROFILE_ID)
    if page == 1:
        return config.url_template.format(user=user)
    return config.page_url_template.format(user=user, page=page)


async def _scrape_all_pages(config: SteamReviewsScrapeConfig | None = None) -> list[dict]:
    if config is None:
        config = SteamReviewsScrapeConfig()

    reviews = []
    page = 1
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; gamelib-mcp/1.0)"},
    ) as client:
        while True:
            try:
                resp = await fetch_allowlisted(
                    client, _page_url(page, config), provider="steam_reviews"
                )
                resp.raise_for_status()
            except Exception as e:
                logger.warning("Steam reviews page %d fetch failed: %s", page, e)
                break

            page_reviews = _parse_page(resp.text, config)
            if not page_reviews:
                break

            reviews.extend(page_reviews)
            page += 1

            if page > config.pagination_cap:
                break

    return reviews


def _parse_page(html: str, config: SteamReviewsScrapeConfig | None = None) -> list[dict]:
    """Parse one Steam recommendations page into {appid, vote, text} rows. Pure."""
    if config is None:
        config = SteamReviewsScrapeConfig()

    soup = BeautifulSoup(html, "lxml")
    reviews = []

    # Each review box on the Steam profile recommendations page
    for box in soup.select(config.review_box_selector):
        # Extract appid from the review link e.g. /recommended/12345/
        link = box.select_one(config.review_link_selector)
        if link is None:
            continue

        href = link.get("href", "")
        m = re.search(config.appid_regex, href) if isinstance(href, str) else None
        if not m:
            continue

        try:
            appid = int(m.group(1))
        except ValueError:
            continue

        # Determine thumb direction
        thumb_up = box.select_one(config.thumb_up_selector)
        thumb_down = box.select_one(config.thumb_down_selector)

        if thumb_up is not None:
            vote = 1
        elif thumb_down is not None:
            vote = -1
        else:
            # Try text "Recommended" / "Not Recommended"
            title_el = box.select_one(config.rating_summary_selector)
            if title_el:
                text = title_el.get_text(strip=True).lower()
                vote = 1 if "not" not in text and "recommend" in text else -1
            else:
                continue

        text_el = box.select_one(config.review_text_selector)
        text = text_el.get_text(strip=True) if text_el else ""

        reviews.append({"appid": appid, "vote": vote, "text": text})

    return reviews
