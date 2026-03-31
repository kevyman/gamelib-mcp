"""GOG owned games sync via GOG OAuth2 API.

Set GOG_REFRESH_TOKEN in .env (obtained via python -m steam_mcp.setup_platform gog).
Playtime is not available from GOG's public API.
"""

import logging
import os
from datetime import datetime, timezone

import aiohttp

from steam_mcp.data.db import find_game_by_name_fuzzy, load_fuzzy_candidates, upsert_game, upsert_game_platform

logger = logging.getLogger(__name__)

_GOG_TOKEN_URL = "https://auth.gog.com/token"
_GOG_LIBRARY_URL = "https://embed.gog.com/user/data/games"
_GOG_GAME_DETAIL_URL = "https://api.gog.com/products/{game_id}?expand=downloads"

# GOG Galaxy public OAuth credentials — set env vars to override if GOG rotates them
_CLIENT_ID = os.getenv("GOG_CLIENT_ID", "46899977096215655")
_CLIENT_SECRET = os.getenv("GOG_CLIENT_SECRET", "9d85c43b1718a031d5b64228ecd1a9eb")


async def _get_access_token(session: aiohttp.ClientSession) -> str:
    """Exchange GOG_REFRESH_TOKEN for a short-lived access token."""
    refresh_token = os.environ["GOG_REFRESH_TOKEN"]
    async with session.post(
        _GOG_TOKEN_URL,
        params={
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data["access_token"]


async def _fetch_owned_game_ids(session: aiohttp.ClientSession, access_token: str) -> list[int]:
    """Return list of owned GOG product IDs."""
    async with session.get(
        _GOG_LIBRARY_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
        return data.get("owned", [])


async def _fetch_game_title(
    session: aiohttp.ClientSession,
    access_token: str,
    gog_id: int,
) -> str | None:
    """Fetch the title for a single GOG product ID."""
    url = _GOG_GAME_DETAIL_URL.format(game_id=gog_id)
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            return data.get("title")
    except Exception as exc:
        logger.debug("Could not fetch GOG title for id=%d: %s", gog_id, exc)
        return None


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("GOG_REFRESH_TOKEN"):
        logger.info("GOG_REFRESH_TOKEN not set — skipping GOG sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0

    try:
        async with aiohttp.ClientSession() as session:
            access_token = await _get_access_token(session)
            gog_ids = await _fetch_owned_game_ids(session, access_token)

            candidates = await load_fuzzy_candidates()
            for gog_id in gog_ids:
                title = await _fetch_game_title(session, access_token, gog_id)
                if not title:
                    skipped += 1
                    continue

                existing = await find_game_by_name_fuzzy(title, candidates=candidates)
                if existing:
                    game_id = existing["id"]
                    matched += 1
                else:
                    game_id = await upsert_game(appid=None, name=title)
                    added += 1

                await upsert_game_platform(
                    game_id=game_id,
                    platform="gog",
                    playtime_minutes=None,  # GOG public API doesn't expose playtime
                    owned=1,
                )

    except Exception as exc:
        logger.warning("GOG sync failed: %s", exc)
        return {"added": added, "matched": matched, "skipped": skipped}

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
