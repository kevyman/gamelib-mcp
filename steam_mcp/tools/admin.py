"""refresh_library and detect_farmed_games admin tools."""

import statistics
from collections import defaultdict

from ..data.steam_xml import fetch_library
from ..data.db import get_meta, get_db


async def refresh_library() -> dict:
    """Force re-sync Steam XML library feed."""
    result = await fetch_library()
    return result


async def detect_farmed_games(
    dry_run: bool = True,
    threshold_hours: float = 8.0,
    min_games_per_day: int = 8,
) -> dict:
    """
    Auto-detect ArchiSteamFarm card-farming sessions and mark games as is_farmed.

    Algorithm:
    1. Find all games with rtime_last_played set, playtime > 0, playtime <= threshold
    2. Group by date; days with >= min_games_per_day games are "farming days"
    3. All games last played on those days are candidates
    4. If dry_run=False, marks them is_farmed=1 in the DB

    dry_run=True (default) lets you preview before committing.
    """
    threshold_minutes = int(threshold_hours * 60)

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """SELECT g.appid, g.name, COALESCE(gp.playtime_minutes, 0) as playtime_forever,
                      g.rtime_last_played,
                      date(g.rtime_last_played, 'unixepoch') as last_played_date
               FROM games g
               LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
               WHERE g.rtime_last_played IS NOT NULL
                 AND COALESCE(gp.playtime_minutes, 0) > 0
                 AND COALESCE(gp.playtime_minutes, 0) <= ?""",
            (threshold_minutes,),
        )

    # Group by date
    by_date: dict[str, list] = defaultdict(list)
    for row in rows:
        by_date[row["last_played_date"]].append(row)

    # Identify farming days
    farming_days = []
    candidate_appids: set[int] = set()
    for date, games in sorted(by_date.items()):
        if len(games) >= min_games_per_day:
            playtimes = [g["playtime_forever"] / 60 for g in games]
            farming_days.append({
                "date": date,
                "game_count": len(games),
                "median_playtime_hours": round(statistics.median(playtimes), 2),
            })
            for g in games:
                candidate_appids.add(g["appid"])

    # Sample games for preview
    sample = []
    for row in rows:
        if row["appid"] in candidate_appids and len(sample) < 10:
            sample.append({
                "appid": row["appid"],
                "name": row["name"],
                "playtime_hours": round(row["playtime_forever"] / 60, 2),
                "last_played": row["last_played_date"],
            })

    if not dry_run and candidate_appids:
        placeholders = ",".join("?" * len(candidate_appids))
        async with get_db() as db:
            await db.execute(
                f"UPDATE games SET is_farmed = 1 WHERE appid IN ({placeholders})",
                list(candidate_appids),
            )
            await db.commit()

    return {
        "farming_days": farming_days,
        "candidates": len(candidate_appids),
        "threshold_hours": threshold_hours,
        "dry_run": dry_run,
        "sample_games": sample,
    }
