"""refresh_library, detect_farmed_games, and set_nintendo_session admin tools."""

import asyncio
import json
import logging
import statistics
from collections import defaultdict

from ..data.db import STEAM_APP_ID, get_db
from ..data.epic import sync_epic
from ..data.gog import sync_gog
from ..data.nintendo import sync_nintendo
from ..data.psn import sync_psn
from ..data.steam_xml import fetch_library

logger = logging.getLogger(__name__)


async def refresh_library(platforms: list[str] | None = None) -> dict:
    """
    Re-sync game library. Defaults to all configured platforms.
    platforms: optional subset, e.g. ["steam", "epic"]. If omitted, syncs all.
    """
    _ALL = {"steam", "epic", "gog", "nintendo", "ps5"}
    targets = set(platforms) if platforms else _ALL

    platform_syncs = {
        "steam":    fetch_library,
        "epic":     sync_epic,
        "gog":      sync_gog,
        "nintendo": sync_nintendo,
        "ps5":      sync_psn,
    }
    selected = [(name, fn) for name, fn in platform_syncs.items() if name in targets]
    outcomes = await asyncio.gather(*(fn() for _, fn in selected), return_exceptions=True)

    results: dict = {}
    for (name, _), outcome in zip(selected, outcomes, strict=True):
        if isinstance(outcome, Exception):
            results[name] = {"error": str(outcome)}
        else:
            results[name] = outcome
    return results


async def set_nintendo_session(cookies: str) -> dict:
    """
    Store Nintendo Account session cookies for VGCS fallback sync.

    Accepts either:
    - A JSON object: {"cookie_name": "value", ...}
    - A JSON array (Cookie Editor / EditThisCookie format):
      [{"name": "...", "value": "..."}, ...]

    How to get your cookies:
    1. Open https://accounts.nintendo.com/portal/vgcs/ in your browser
    2. Install the "Cookie Editor" browser extension
    3. Click the extension icon → Export → copy the JSON
    4. Pass that JSON string to this tool

    Cookies are saved to the path in NINTENDO_COOKIES_FILE
    (default: data/nintendo_cookies.json).
    """
    try:
        raw = json.loads(cookies)
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"Invalid JSON: {exc}"}

    if isinstance(raw, list):
        normalized = {c["name"]: c["value"] for c in raw if "name" in c and "value" in c}
    elif isinstance(raw, dict):
        normalized = raw
    else:
        return {"success": False, "error": "Expected a JSON object or array"}

    if not normalized:
        return {"success": False, "error": "No valid cookies found in input"}

    path = os.getenv("NINTENDO_COOKIES_FILE", "data/nintendo_cookies.json")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)

    logger.info("Nintendo session cookies saved to %s (%d cookies)", path, len(normalized))
    return {"success": True, "cookie_count": len(normalized), "path": path}


async def detect_farmed_games(
    dry_run: bool = True,
    threshold_hours: float = 8.0,
    min_games_per_day: int = 8,
) -> dict:
    """
    Auto-detect ArchiSteamFarm card-farming sessions and mark games as is_farmed.

    Algorithm:
    1. Find Steam games with rtime_last_played set and low playtime.
    2. Group by date; days with >= min_games_per_day games are "farming days".
    3. All Steam games last played on those days are candidates.
    4. If dry_run=False, marks their canonical game rows is_farmed=1.
    """
    threshold_minutes = int(threshold_hours * 60)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.id AS game_id,
                      g.name,
                      CAST(gpi.identifier_value AS INTEGER) AS appid,
                      COALESCE(gp.playtime_minutes, 0) AS playtime_forever,
                      spd.rtime_last_played,
                      date(spd.rtime_last_played, 'unixepoch') AS last_played_date
               FROM games g
               JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               JOIN game_platform_identifiers gpi
                 ON gpi.game_platform_id = gp.id AND gpi.identifier_type = ?
               LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
               WHERE spd.rtime_last_played IS NOT NULL
                 AND COALESCE(gp.playtime_minutes, 0) > 0
                 AND COALESCE(gp.playtime_minutes, 0) <= ?""",
            (STEAM_APP_ID, threshold_minutes),
        )

    by_date: dict[str, list] = defaultdict(list)
    for row in rows:
        by_date[row["last_played_date"]].append(row)

    farming_days = []
    candidate_game_ids: set[int] = set()
    candidate_appids: set[int] = set()
    for date, games in sorted(by_date.items()):
        if len(games) >= min_games_per_day:
            playtimes = [game["playtime_forever"] / 60 for game in games]
            farming_days.append(
                {
                    "date": date,
                    "game_count": len(games),
                    "median_playtime_hours": round(statistics.median(playtimes), 2),
                }
            )
            for game in games:
                candidate_game_ids.add(game["game_id"])
                candidate_appids.add(game["appid"])

    sample = []
    for row in rows:
        if row["game_id"] in candidate_game_ids and len(sample) < 10:
            sample.append(
                {
                    "game_id": row["game_id"],
                    "appid": row["appid"],
                    "name": row["name"],
                    "playtime_hours": round(row["playtime_forever"] / 60, 2),
                    "last_played": row["last_played_date"],
                }
            )

    if not dry_run and candidate_game_ids:
        placeholders = ",".join("?" * len(candidate_game_ids))
        async with get_db() as db:
            await db.execute(
                f"UPDATE games SET is_farmed = 1 WHERE id IN ({placeholders})",
                list(candidate_game_ids),
            )
            await db.commit()

    return {
        "farming_days": farming_days,
        "candidates": len(candidate_game_ids),
        "steam_appids": sorted(candidate_appids),
        "threshold_hours": threshold_hours,
        "dry_run": dry_run,
        "sample_games": sample,
    }
