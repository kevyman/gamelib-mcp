"""Lazy ProtonDB tier fetch."""

import logging
from datetime import UTC, datetime

import httpx

from . import provider_health
from .db import get_steam_platform_row_by_appid, upsert_steam_platform_data

CACHE_DAYS = 30
PROTONDB_API = "https://www.protondb.com/api/v1/reports/summaries/{appid}.json"
TIER_ORDER = ["native", "platinum", "gold", "silver", "bronze", "borked"]
logger = logging.getLogger(__name__)


async def get_protondb(appid: int) -> str | None:
    """Lazy-fetch ProtonDB tier. Returns tier string or None."""
    row = await get_steam_platform_row_by_appid(appid)

    if row and _is_fresh(row["protondb_cached_at"], CACHE_DAYS):
        return row["protondb_tier"]

    if row is None:
        return None

    return await _fetch_and_cache(appid, row["game_platform_id"])


async def _fetch_and_cache(appid: int, game_platform_id: int) -> str | None:
    now = datetime.now(UTC).isoformat()
    tier = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(PROTONDB_API.format(appid=appid))
            if resp.status_code != 200:
                if resp.status_code == 404:
                    # ProtonDB answered: it has no reports for this app. A
                    # miss, not an outage — an obscure library would otherwise
                    # trip the enrichment WARNING on every unreported game.
                    provider_health.record_success("protondb")
                else:
                    provider_health.record_failure(
                        "protondb", f"ProtonDB HTTP {resp.status_code} for appid {appid}"
                    )
                logger.warning("ProtonDB returned %s for appid %d", resp.status_code, appid)
                # Write backoff timestamp without touching tier so the background
                # worker doesn't immediately re-claim the row and hot-loop.
                await upsert_steam_platform_data(game_platform_id, protondb_cached_at=now)
                return None
            data = resp.json()
            tier = data.get("tier")
    except Exception as e:
        provider_health.record_failure("protondb", e)
        logger.warning("ProtonDB fetch failed for appid %d: %s", appid, e)
        await upsert_steam_platform_data(game_platform_id, protondb_cached_at=now)
        return None

    # A 200 with no tier is an answer ("nobody has reported on this app"),
    # so it counts as processed like any other successful fetch.
    provider_health.record_success("protondb")
    await upsert_steam_platform_data(
        game_platform_id,
        protondb_tier=tier,
        protondb_cached_at=now,
    )

    return tier


def tier_rank(tier: str | None) -> int:
    """Return numeric rank for tier comparison (lower = better)."""
    if not tier:
        return 999
    try:
        return TIER_ORDER.index(tier.lower())
    except ValueError:
        return 999


def meets_min_tier(tier: str | None, min_tier: str) -> bool:
    """Check if tier is at least as good as min_tier."""
    return tier_rank(tier) <= tier_rank(min_tier)


def _is_fresh(cached_at: str | None, days: int) -> bool:
    if not cached_at:
        return False
    try:
        dt = datetime.fromisoformat(cached_at)
        age = datetime.now(UTC) - dt
        return age.total_seconds() < days * 86400
    except ValueError:
        return False
