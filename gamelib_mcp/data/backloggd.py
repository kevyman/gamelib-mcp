"""Scrape Backloggd reviews for the configured user and upsert into ratings table.

The declarative surface (URL templates, selectors, the star-width regex,
pagination cap, fuzzy cutoff) comes from ``scrape_config.load_scrape_config
("backloggd")`` so it can be healed at the config level; the DOM sibling-walk
in ``_find_preceding_title`` is deliberately code — it is structural traversal,
not a single selector, and stays out of the healable vocabulary.
"""

import logging
import os
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup, Tag

from .db import extract_best_fuzzy_key, get_db
from .scrape_config import BackloggdScrapeConfig, fetch_allowlisted, load_scrape_config

_BACKLOGGD_USER = os.getenv("BACKLOGGD_USER", "")
BASE_URL = f"https://backloggd.com/u/{_BACKLOGGD_USER}/reviews"
logger = logging.getLogger(__name__)


async def sync_backloggd() -> dict:
    """
    Scrape all pages of Backloggd reviews, fuzzy-match to DB games,
    upsert into ratings. Returns stats dict.
    """
    config = await load_scrape_config("backloggd")

    async with get_db() as db:
        game_rows = await db.execute_fetchall("SELECT id, name FROM games")

    name_to_id = {r["name"].lower(): r["id"] for r in game_rows}
    all_names = list(name_to_id.keys())
    candidate_names = {name: name for name in all_names}

    reviews = await _scrape_all_pages(config)
    synced = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        for review in reviews:
            game_id = _match_game_id(
                review["title"], candidate_names, name_to_id, cutoff=config.fuzzy_cutoff
            )
            if game_id is None:
                logger.debug("No match for Backloggd game: %s", review["title"])
                skipped += 1
                continue

            raw = review["score"]  # 0.5–5
            normalized = raw * 2   # → 1–10

            await db.execute(
                """INSERT INTO ratings (game_id, source, raw_score, normalized_score, review_text, synced_at)
                   VALUES (?, 'backloggd', ?, ?, ?, ?)
                   ON CONFLICT(game_id, source) DO UPDATE SET
                       raw_score = excluded.raw_score,
                       normalized_score = excluded.normalized_score,
                       review_text = excluded.review_text,
                       synced_at = excluded.synced_at""",
                (game_id, raw, normalized, review.get("text", ""), now),
            )
            synced += 1

        await db.commit()

    return {"synced": synced, "skipped": skipped, "total_scraped": len(reviews)}


def _page_url(page: int, config: BackloggdScrapeConfig) -> str:
    user = os.getenv("BACKLOGGD_USER", _BACKLOGGD_USER)
    if page == 1:
        return config.url_template.format(user=user)
    return config.page_url_template.format(user=user, page=page)


async def _scrape_all_pages(config: BackloggdScrapeConfig | None = None) -> list[dict]:
    """Paginate through Backloggd reviews and return list of {title, score, text}."""
    if config is None:
        config = BackloggdScrapeConfig()

    reviews = []
    page = 1
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "gamelib-mcp/1.0"}) as client:
        while True:
            try:
                resp = await fetch_allowlisted(
                    client, _page_url(page, config), provider="backloggd"
                )
                resp.raise_for_status()
            except Exception as e:
                logger.warning("Backloggd page %d fetch failed: %s", page, e)
                break

            page_reviews = _parse_page(resp.text, config)
            if not page_reviews:
                break

            reviews.extend(page_reviews)
            page += 1

            # Safety limit
            if page > config.pagination_cap:
                break

    return reviews


def _parse_page(html: str, config: BackloggdScrapeConfig | None = None) -> list[dict]:
    """Parse one Backloggd reviews page. Pure: no network, no DB.

    The page structure places game titles in `.game-name h3` elements
    as siblings *before* each `.review-card` div. We iterate over the
    review cards and look backwards to find the preceding game name.
    """
    if config is None:
        config = BackloggdScrapeConfig()

    soup = BeautifulSoup(html, "lxml")
    reviews = []

    # The main reviews container: div.user-reviews containing the review cards
    cards = soup.select(config.review_card_selector)

    for card in cards:
        # Game title is in a .game-name row that precedes the review-card
        title = _find_preceding_title(card, config)
        if not title:
            continue

        # Star rating: .stars-top has style="width:XX%" where 100% = 5 stars
        score = _extract_score(card, config)
        if score is None:
            continue

        # Review text is in .review-body .card-text
        text_el = card.select_one(config.review_text_selector)
        text = text_el.get_text(strip=True) if text_el else ""

        reviews.append({"title": title, "score": score, "text": text})

    return reviews


def _find_preceding_title(card: Tag, config: BackloggdScrapeConfig) -> str | None:
    """Walk backwards from a .review-card to find the preceding .game-name h3."""
    # Walk up — the card is inside a col > row structure, so we may need
    # to look at previous siblings of the card's parent containers too.
    node: Tag | None = card
    for _ in range(5):  # limit depth
        if node is None:
            break
        prev = node.find_previous_sibling()
        if prev:
            # Check if this sibling (or something inside it) has .game-name
            if isinstance(prev, Tag):
                if config.title_container_class in (prev.get("class") or []):
                    h3 = prev.select_one(config.title_inner_selector)
                    if h3:
                        return h3.get_text(strip=True)
                gn = prev.select_one(config.title_selector)
                if gn:
                    return gn.get_text(strip=True)
        # Move up to parent and try again
        node = node.parent

    return None


def _extract_score(card: Tag, config: BackloggdScrapeConfig) -> float | None:
    """Extract star rating from the .stars-top width percentage.

    Backloggd renders ratings as two overlapping rows of star spans:
      .stars-bottom: empty stars (always 5)
      .stars-top: filled stars, clipped via style="width:XX%"
    So width:100% = 5.0, width:50% = 2.5, width:10% = 0.5, etc.
    """
    stars_top = card.select_one(config.score_selector)
    if stars_top:
        style = stars_top.get("style", "")
        match = re.search(config.score_style_regex, style) if isinstance(style, str) else None
        if match:
            try:
                pct = float(match.group(1))
            except ValueError:
                return None
            score = round(pct / 20, 1)  # 100% / 20 = 5.0
            if 0.5 <= score <= 5:
                return score

    return None


def _match_game_id(
    title: str, candidate_names: dict[str, str], name_to_id: dict, cutoff: int = 85
) -> int | None:
    """Fuzzy-match a Backloggd title to a game in the DB, returns games.id."""
    title_lower = title.lower()

    # Exact match first
    if title_lower in name_to_id:
        return name_to_id[title_lower]

    # Fuzzy match
    match = extract_best_fuzzy_key(title_lower, candidate_names, cutoff=cutoff)
    if match:
        return name_to_id[match]

    return None
