"""PlayStation Network library sync via PSNAWP.

Auth: set PSN_NPSSO in .env.
Obtain the NPSSO cookie by visiting https://ca.account.sony.com/api/v1/ssocookie
while logged in to your PSN account in a browser. The page renders an error message,
but the `npsso` cookie is set — open DevTools (F12) → Application → Cookies →
find `npsso` under the Sony domain and copy the 64-character value.

Library source: client.title_stats() — returns all titles the user has played,
with name, play_count, and play_duration (datetime.timedelta). Only played titles
appear; unplayed purchases will not show up (PSN platform limitation).
"""

import asyncio
import logging
import os

from psnawp_api.models.title_stats import PlatformCategory

from gamelib_mcp.data.db import (
    load_fuzzy_candidates,
    upsert_game_platform,
    upsert_game_platform_enrichment,
)
from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB

logger = logging.getLogger(__name__)

# Media/streaming apps to exclude from library sync.
# The primary filter catches PPSA-prefixed titles with UNKNOWN category (PS5-era apps).
# This blocklist catches PS4-era apps (CUSA IDs) that share the same UNKNOWN category
# but wouldn't be caught by the prefix check alone.
_MEDIA_APP_NAMES = {
    "Disney+", "Spotify", "Netflix", "YouTube", "Prime Video",
    "Plex", "Crunchyroll", "Apple TV", "Twitch", "SONY PICTURES CORE",
}


def _get_psnawp():
    """Return an authenticated PSNAWP instance, or raise if not configured."""
    from psnawp_api import PSNAWP  # lazy import — optional dependency
    npsso = os.environ.get("PSN_NPSSO")
    if not npsso:
        raise EnvironmentError("PSN_NPSSO not set")
    return PSNAWP(npsso)


async def fetch_psn_library() -> list[dict]:
    """
    Return a list of dicts with 'name' and 'playtime_minutes' for each played PS5 title.

    Uses client.title_stats() which returns name, play_count, and play_duration
    (a datetime.timedelta). Runs PSNAWP synchronously in an executor.
    """
    def _fetch():
        psnawp = _get_psnawp()
        client = psnawp.me()
        results = []
        for entry in client.title_stats():
            name = entry.name
            if not name:
                continue
            if entry.category is PlatformCategory.UNKNOWN and (entry.title_id or "").startswith("PPSA"):
                continue
            if name in _MEDIA_APP_NAMES:
                continue
            minutes = int(entry.play_duration.total_seconds() // 60)
            results.append({"name": name, "playtime_minutes": minutes})
        return results

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch)


async def sync_psn() -> dict:
    """
    Sync PSN library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("PSN_NPSSO"):
        logger.info("PSN_NPSSO not set — skipping PSN sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0

    try:
        entries = await fetch_psn_library()
    except Exception as exc:
        logger.warning("PSN sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    candidates = await load_fuzzy_candidates()

    for entry in entries:
        name = entry["name"]
        if not name:
            skipped += 1
            continue

        igdb_platform_id = PLATFORM_TO_IGDB.get("ps5")
        game_id, igdb_game = await resolve_and_link_game(name, igdb_platform_id, candidates)
        if game_id in candidates:
            matched += 1
        else:
            candidates[game_id] = name
            added += 1

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="ps5",
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    logger.info("PSN sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
