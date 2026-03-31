"""Epic Games Store library sync via legendary CLI.

Requires `legendary` to be installed and authenticated (`legendary auth`).
Set EPIC_LEGENDARY_PATH to the legendary config directory if non-default.
Playtime is not available from Epic.
"""

import json
import logging
import os
import asyncio
from datetime import datetime, timezone

from steam_mcp.data.db import find_game_by_name_fuzzy, load_fuzzy_candidates, upsert_game, upsert_game_platform

logger = logging.getLogger(__name__)

LEGENDARY_BIN = os.getenv("LEGENDARY_BIN", "legendary")


async def _run_legendary(*args: str) -> str:
    """Run a legendary CLI command and return stdout."""
    cmd = [LEGENDARY_BIN, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"legendary {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode()[:200]}"
        )
    return stdout.decode()


async def fetch_epic_library() -> list[dict]:
    """Return list of owned Epic games as dicts with at least 'title' and 'app_name'."""
    raw = await _run_legendary("list", "--json")
    data = json.loads(raw)
    # legendary --json returns a list of game objects
    if isinstance(data, list):
        return data
    # Some versions wrap in {"games": [...]}
    return data.get("games", [])


async def sync_epic() -> dict:
    """
    Sync Epic Games library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("EPIC_LEGENDARY_PATH") and not _legendary_available():
        logger.info("legendary not configured — skipping Epic sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    try:
        games = await fetch_epic_library()
    except Exception as exc:
        logger.warning("Epic sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    candidates = await load_fuzzy_candidates()

    for game in games:
        title = game.get("title") or game.get("app_title") or game.get("app_name")
        if not title:
            skipped += 1
            continue

        existing = await find_game_by_name_fuzzy(title, candidates=candidates)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=title)
            candidates[game_id] = title
            added += 1

        await upsert_game_platform(
            game_id=game_id,
            platform="epic",
            playtime_minutes=None,  # Epic doesn't expose playtime
            owned=1,
        )

    logger.info("Epic sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}


def _legendary_available() -> bool:
    """Check if legendary binary is on PATH."""
    import shutil
    return shutil.which(LEGENDARY_BIN) is not None
