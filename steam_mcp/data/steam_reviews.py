"""Scrape Steam community reviews for the configured user."""

import logging
import os
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from .db import get_db

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
    """
    reviews = await _scrape_all_pages()
    synced = 0
    now = datetime.now(timezone.utc).isoformat()

    # Pre-fetch game ids and community review scores for all reviewed games
    game_info: dict[int, dict] = {}  # appid -> {id, steam_review_score}
    async with get_db() as db:
        for review in reviews:
            row = await db.execute_fetchone(
                "SELECT id, steam_review_score FROM games WHERE appid = ?",
                (review["appid"],),
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

    return {"synced": synced, "total_scraped": len(reviews)}


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


async def _scrape_all_pages() -> list[dict]:
    reviews = []
    page = 1
    async with httpx.AsyncClient(
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 (compatible; steam-mcp/1.0)"},
        follow_redirects=True,
    ) as client:
        while True:
            url = BASE_URL if page == 1 else f"{BASE_URL}?p={page}"
            try:
                resp = await client.get(url)
                resp.raise_for_status()
            except Exception as e:
                logger.warning("Steam reviews page %d fetch failed: %s", page, e)
                break

            page_reviews = _parse_page(resp.text)
            if not page_reviews:
                break

            reviews.extend(page_reviews)
            page += 1

            if page > 200:
                break

    return reviews


def _parse_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    reviews = []

    # Each review box on the Steam profile recommendations page
    for box in soup.select(".review_box, [class*='review_box']"):
        # Extract appid from the review link e.g. /recommended/12345/
        link = box.select_one("a[href*='/recommended/']")
        if link is None:
            continue

        m = re.search(r"/recommended/(\d+)/", link.get("href", ""))
        if not m:
            continue

        appid = int(m.group(1))

        # Determine thumb direction
        thumb_up = box.select_one(".thumb_up, .thumbsUp, [class*='thumbsUp'], [class*='thumb_up']")
        thumb_down = box.select_one(".thumb_down, .thumbsDown, [class*='thumbsDown'], [class*='thumb_down']")

        if thumb_up is not None:
            vote = 1
        elif thumb_down is not None:
            vote = -1
        else:
            # Try text "Recommended" / "Not Recommended"
            title_el = box.select_one(".title, [class*='ratingSummary']")
            if title_el:
                text = title_el.get_text(strip=True).lower()
                vote = 1 if "not" not in text and "recommend" in text else -1
            else:
                continue

        text_el = box.select_one(".content, [class*='review_content'] p, [class*='apphub_CardTextContent']")
        text = text_el.get_text(strip=True) if text_el else ""

        reviews.append({"appid": appid, "vote": vote, "text": text})

    return reviews
