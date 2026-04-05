"""Lazy Steam Store API enrichment — genres, tags, review score, metacritic."""

import asyncio
import json
import re
from datetime import datetime, timezone

import httpx

from .db import get_db, get_steam_platform_row_by_appid, upsert_game_platform_enrichment, upsert_steam_platform_data

STORE_CACHE_DAYS = 7
STORE_API = "https://store.steampowered.com/api/appdetails"
REVIEWS_API = "https://store.steampowered.com/appreviews/{appid}"


async def enrich_game(appid: int) -> dict | None:
    """
    Fetch Steam Store data for appid and cache in DB.
    Returns the full games row dict, or None on failure.
    """
    row = await get_steam_platform_row_by_appid(appid)
    if row is None:
        return None
    if _is_fresh(row["store_cached_at"], STORE_CACHE_DAYS):
        return dict(row)

    store_data, review_summary = await _fetch_all(appid)
    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        if store_data is not None:
            steam_tags = _extract_tags(store_data)
            genres = json.dumps([g["description"] for g in store_data.get("genres", [])])
            short_desc = store_data.get("short_description", "")
            raw_date = (store_data.get("release_date") or {}).get("date", "")
            release_date = _parse_steam_date(raw_date)

            await db.execute(
                """UPDATE games SET
                    genres = ?,
                    tags = ?,
                    short_description = ?,
                    release_date = COALESCE(release_date, ?)
                WHERE id = ?""",
                (genres, steam_tags, short_desc, release_date, row["game_id"]),
            )
        await db.commit()

    steam_fields = {"store_cached_at": now}
    if "review_score" in review_summary:
        steam_fields["steam_review_score"] = review_summary["review_score"]
    if "review_score_desc" in review_summary:
        steam_fields["steam_review_desc"] = review_summary["review_score_desc"]
    await upsert_steam_platform_data(row["game_platform_id"], **steam_fields)

    # Write metacritic to game_platform_enrichment (Steam Store provides this for free)
    if store_data is not None:
        metacritic = store_data.get("metacritic") or {}
        metacritic_score = metacritic.get("score")
        metacritic_url = metacritic.get("url")
        if metacritic_score is not None:
            enrichment_fields: dict = {
                "metacritic_score": metacritic_score,
                "metacritic_cached_at": now,
            }
            if metacritic_url:
                enrichment_fields["metacritic_url"] = metacritic_url
            await upsert_game_platform_enrichment(row["game_platform_id"], **enrichment_fields)

    refreshed = await get_steam_platform_row_by_appid(appid)
    return dict(refreshed) if refreshed else None


async def _fetch_all(appid: int) -> tuple[dict | None, dict]:
    """Fetch appdetails and appreviews concurrently. Returns (store_data, review_summary)."""
    async def fetch_store():
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    STORE_API,
                    params={"appids": appid, "filters": "basic,genres,categories,short_description,metacritic,release_date"},
                )
                resp.raise_for_status()
                payload = resp.json()
            app_data = payload.get(str(appid), {})
            if not app_data.get("success"):
                return None
            return app_data.get("data", {})
        except Exception:
            return None

    async def fetch_reviews():
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    REVIEWS_API.format(appid=appid),
                    params={"json": 1, "language": "all", "purchase_type": "all"},
                )
                resp.raise_for_status()
                return resp.json().get("query_summary", {})
        except Exception:
            return {}

    store_data, review_summary = await asyncio.gather(fetch_store(), fetch_reviews())
    return store_data, review_summary


def _extract_tags(data: dict) -> str:
    """Build tag list from genres + categories, deduplicated, max 20."""
    tags = []
    for g in data.get("genres", []):
        tags.append(g["description"])
    for c in data.get("categories", []):
        tags.append(c["description"])
    seen = set()
    unique = []
    for t in tags:
        if t.lower() not in seen:
            seen.add(t.lower())
            unique.append(t)
    return json.dumps(unique[:20])


def _parse_steam_date(raw: str) -> str | None:
    """Parse Steam's release date string (e.g. '8 Nov, 2022') to ISO format, best-effort."""
    if not raw:
        return None
    # Try "D Mon, YYYY" or "D Mon YYYY"
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)[,\s]+(\d{4})", raw)
    if m:
        months = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        month = months.get(m.group(2).lower()[:3])
        if month:
            return f"{m.group(3)}-{month}-{int(m.group(1)):02d}"
    # Try bare year
    m = re.match(r"^(\d{4})$", raw.strip())
    if m:
        return f"{m.group(1)}-01-01"
    return None


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at or cached_at == "FAILED":
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        return (datetime.now(timezone.utc) - dt).total_seconds() < days * 86400
    except ValueError:
        return False
