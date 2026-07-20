"""get_play_history: what got played in a given window, per game.

Non-Nintendo platforms derive deltas from cumulative play_history snapshots
(one row per game+platform+day, written by record_play_history_snapshots
after each sync — see data/db/history.py). switch2 is served instead from
nintendo_play_summary's real per-day Parental Controls data, which is
strictly more accurate than a snapshot delta would be.
"""

from datetime import date, datetime, timedelta, timezone

from fastmcp.exceptions import ToolError

from ..data.db import get_db
from ..data.nintendo import NINTENDO_TITLE_ID
from .common import LIBRARY_PLATFORMS, clamp_limit, validate_platform

_SWITCH2 = "switch2"

# Deltas per (game, platform) from cumulative play_history snapshots. The
# baseline is the last snapshot strictly before the window, or — if a game's
# very first snapshot falls inside the window — that first snapshot itself
# (its growth *before* that point is unattributable, so it's excluded rather
# than misreported). MAX(0, ...) absorbs any upstream total correction
# (e.g. a re-sync that legitimately lowers a stored total).
_GENERIC_DELTA_SQL = """
WITH bounds AS (
    SELECT ph.game_id, ph.platform,
           (SELECT ph2.playtime_minutes FROM play_history ph2
            WHERE ph2.game_id = ph.game_id AND ph2.platform = ph.platform
              AND ph2.snapshot_date <= :end
            ORDER BY ph2.snapshot_date DESC LIMIT 1) AS end_total,
           COALESCE(
               (SELECT ph3.playtime_minutes FROM play_history ph3
                WHERE ph3.game_id = ph.game_id AND ph3.platform = ph.platform
                  AND ph3.snapshot_date < :start
                ORDER BY ph3.snapshot_date DESC LIMIT 1),
               (SELECT ph4.playtime_minutes FROM play_history ph4
                WHERE ph4.game_id = ph.game_id AND ph4.platform = ph.platform
                  AND ph4.snapshot_date >= :start AND ph4.snapshot_date <= :end
                ORDER BY ph4.snapshot_date ASC LIMIT 1)
           ) AS start_total
    FROM play_history ph
    WHERE ph.snapshot_date >= :start AND ph.snapshot_date <= :end
      AND ph.platform != 'switch2'
      {platform_clause}
    GROUP BY ph.game_id, ph.platform
)
SELECT b.game_id AS game_id, b.platform AS platform, g.name AS name,
       MAX(0, b.end_total - b.start_total) AS minutes_played
FROM bounds b JOIN games g ON g.id = b.game_id
WHERE b.end_total - b.start_total > 0
"""

# switch2 deltas come from real daily data instead of snapshot subtraction.
# nintendo_play_summary.application_id is bridged to a game via the
# nintendo_title_id identifier recorded on that game's switch2 platform row.
# Plain equality: both sides are normalized to uppercase at ingest (see
# data/db/__init__.py::normalize_identifier_value, applied by
# upsert_game_platform_identifier and upsert_nintendo_play_summary) instead of
# comparing case-insensitively at read time.
_SWITCH2_DELTA_SQL = """
SELECT gp.game_id AS game_id, 'switch2' AS platform, g.name AS name,
       SUM(nps.playtime_minutes) AS minutes_played
FROM nintendo_play_summary nps
JOIN game_platform_identifiers gpi
  ON gpi.identifier_type = :identifier_type
 AND gpi.identifier_value = nps.application_id
JOIN game_platforms gp ON gp.id = gpi.game_platform_id
JOIN games g ON g.id = gp.game_id
WHERE nps.period_type = 'day'
  AND nps.period_key >= :start AND nps.period_key <= :end
GROUP BY gp.game_id
HAVING minutes_played > 0
"""

# Playtime for switch2 titles played (e.g. under another VGCS account on a
# shared console) that never resolved to a game in the library.
_SWITCH2_UNMATCHED_SQL = """
SELECT COALESCE(SUM(nps.playtime_minutes), 0) AS unmatched
FROM nintendo_play_summary nps
WHERE nps.period_type = 'day'
  AND nps.period_key >= :start AND nps.period_key <= :end
  AND NOT EXISTS (
      SELECT 1 FROM game_platform_identifiers gpi
      WHERE gpi.identifier_type = :identifier_type
        AND gpi.identifier_value = nps.application_id
  )
"""


def _parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ToolError(
            f"Invalid {label} '{value}': expected ISO format YYYY-MM-DD"
        ) from None


async def get_play_history(
    days: int = 30,
    start_date: str | None = None,
    end_date: str | None = None,
    platform: str | None = None,
    limit: int = 20,
) -> dict:
    """
    What you actually played in a time window, per game, most-played first.

    Defaults to the last `days` days; or pass explicit ISO start_date/end_date
    (inclusive). Non-Nintendo platforms are computed from cumulative sync
    snapshots, so granularity is per-sync-day and history only exists from the
    day this feature was deployed — a game's very first snapshot inside the
    window only counts growth after that snapshot, since its prior total is
    unattributable. switch2 uses real per-day Parental Controls data
    (nintendo_play_summary) instead, which is forward-only for the same
    reason (see set_nintendo_pctl_session). Returns per-game minutes,
    per-platform totals, and the window used.
    """
    end = _parse_iso_date(end_date, "end_date") if end_date else datetime.now(timezone.utc).date()
    start = _parse_iso_date(start_date, "start_date") if start_date else end - timedelta(days=days)
    if start > end:
        raise ToolError(
            f"start_date {start.isoformat()} is after end_date {end.isoformat()}"
        )

    resolved_platform = validate_platform(platform, LIBRARY_PLATFORMS) if platform else None
    start_str = start.isoformat()
    end_str = end.isoformat()

    rows: list[dict] = []
    switch2_unmatched_minutes = 0

    async with get_db() as db:
        if resolved_platform != _SWITCH2:
            platform_clause = ""
            params: dict = {"start": start_str, "end": end_str}
            if resolved_platform is not None:
                platform_clause = "AND ph.platform = :platform"
                params["platform"] = resolved_platform
            sql = _GENERIC_DELTA_SQL.format(platform_clause=platform_clause)
            generic_rows = await db.execute_fetchall(sql, params)
            rows.extend(
                {
                    "game_id": r["game_id"],
                    "platform": r["platform"],
                    "name": r["name"],
                    "minutes_played": r["minutes_played"],
                }
                for r in generic_rows
            )

        if resolved_platform is None or resolved_platform == _SWITCH2:
            switch_params = {
                "start": start_str,
                "end": end_str,
                "identifier_type": NINTENDO_TITLE_ID,
            }
            switch_rows = await db.execute_fetchall(_SWITCH2_DELTA_SQL, switch_params)
            rows.extend(
                {
                    "game_id": r["game_id"],
                    "platform": r["platform"],
                    "name": r["name"],
                    "minutes_played": r["minutes_played"],
                }
                for r in switch_rows
            )
            unmatched_row = await db.execute_fetchone(
                _SWITCH2_UNMATCHED_SQL, switch_params
            )
            switch2_unmatched_minutes = (unmatched_row["unmatched"] if unmatched_row else 0) or 0

    rows.sort(key=lambda r: r["minutes_played"], reverse=True)

    total_minutes = sum(r["minutes_played"] for r in rows)
    by_platform: dict[str, int] = {}
    for r in rows:
        by_platform[r["platform"]] = by_platform.get(r["platform"], 0) + r["minutes_played"]

    limited = rows[: clamp_limit(limit)]
    games = [
        {
            "game_id": r["game_id"],
            "name": r["name"],
            "platform": r["platform"],
            "minutes_played": r["minutes_played"],
            "hours_played": round(r["minutes_played"] / 60, 1),
        }
        for r in limited
    ]

    return {
        "window": {"start": start_str, "end": end_str},
        "total_minutes": total_minutes,
        "total_hours": round(total_minutes / 60, 1),
        "by_platform": by_platform,
        "games": games,
        "switch2_unmatched_minutes": switch2_unmatched_minutes,
    }
