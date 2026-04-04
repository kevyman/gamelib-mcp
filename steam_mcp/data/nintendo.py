"""Nintendo Switch play history sync via nxapi CLI.

Auth: set NINTENDO_SESSION_TOKEN in .env.
Obtain by running: nxapi nso auth
Follow the prompts; copy the session token into .env.

Caveat: nxapi only exposes play history (titles that have been launched).
Unplayed digital purchases and uninserted physical cartridges will not appear.
This is a Nintendo platform limitation — no workaround exists.

Playtime: reported in minutes from Nintendo's play history API.
"""

import asyncio
import json
import logging
import os
import shutil

from steam_mcp.data.db import (
    find_game_by_name_fuzzy,
    load_fuzzy_candidates,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
)

logger = logging.getLogger(__name__)

NXAPI_BIN = os.getenv("NXAPI_BIN", "nxapi")
NINTENDO_TITLE_ID = "nintendo_title_id"


def _nxapi_available() -> bool:
    return shutil.which(NXAPI_BIN) is not None


async def _run_nxapi(*args: str) -> str:
    """Run an nxapi CLI command and return stdout as a string."""
    token = os.environ.get("NINTENDO_SESSION_TOKEN")
    env = {**os.environ}
    if token:
        env["NXAPI_SESSION_TOKEN"] = token

    proc = await asyncio.create_subprocess_exec(
        NXAPI_BIN, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"nxapi {' '.join(args)} failed (rc={proc.returncode}): {stderr.decode()[:300]}"
        )
    return stdout.decode()


async def fetch_nintendo_play_history() -> list[dict]:
    """
    Return play history as a list of dicts with keys:
      - name (str): game title
      - playtime_minutes (int | None): total play time in minutes
      - title_id (str | None): Nintendo title ID if available
    """
    # Primary: `nxapi nso play-history --json`; fallback: `nxapi nso titles --json`
    try:
        raw = await _run_nxapi("nso", "play-history", "--json")
    except RuntimeError:
        raw = await _run_nxapi("nso", "titles", "--json")

    data = json.loads(raw)

    # nxapi returns {"items": [...]} or {"titles": [...]} or a bare list
    items = data if isinstance(data, list) else data.get("items", data.get("titles", []))

    results = []
    for item in items:
        name = item.get("name") or item.get("title") or item.get("gameName")
        if not name:
            continue

        # Playtime may be in minutes or seconds depending on nxapi version
        minutes = (
            item.get("playingMinutes")
            or item.get("totalPlayedMinutes")
            or item.get("totalPlayTime")
        )
        # Heuristic: values >10000 are likely seconds; convert
        if minutes and minutes > 10_000:
            minutes = minutes // 60

        title_id = item.get("titleId") or item.get("id")

        results.append({
            "name": str(name),
            "playtime_minutes": int(minutes) if minutes else None,
            "title_id": str(title_id) if title_id else None,
        })

    return results


async def sync_nintendo() -> dict:
    """
    Sync Nintendo Switch play history into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not os.getenv("NINTENDO_SESSION_TOKEN"):
        logger.info("NINTENDO_SESSION_TOKEN not set — skipping Nintendo sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    if not _nxapi_available():
        logger.warning("nxapi binary not found — skipping Nintendo sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0

    try:
        history = await fetch_nintendo_play_history()
    except Exception as exc:
        logger.warning("Nintendo sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    candidates = await load_fuzzy_candidates()

    for entry in history:
        name = entry["name"]
        if not name:
            skipped += 1
            continue

        existing = await find_game_by_name_fuzzy(name, candidates=candidates)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=name)
            candidates[game_id] = name
            added += 1

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="switch",
            playtime_minutes=entry["playtime_minutes"],
            owned=1,
        )

        if entry["title_id"]:
            await upsert_game_platform_identifier(
                platform_id, NINTENDO_TITLE_ID, entry["title_id"]
            )

    logger.info("Nintendo sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
