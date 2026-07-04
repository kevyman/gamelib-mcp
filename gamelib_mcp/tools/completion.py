"""suggest_completion_status: read-only heuristic for un-classified games."""

from datetime import datetime, timezone

from ..data.db import get_db
from .common import PLAYTIME_SUM_SQL as _PLAYTIME_SUM_SQL, clamp_limit as _clamp_limit

# Abandoned candidates need enough playtime to rule out "hasn't started yet"
# and enough staleness to rule out "still actively playing, just slowly".
_ABANDONED_MIN_MINUTES = 120
_ABANDONED_MAX_HLTB_RATIO = 0.5
_ABANDONED_MIN_STALE_DAYS = 365

# One rollup per currently-unclassified, non-farmed primary-library game: total
# playtime plus the freshest last-played signal across both sources (Steam's
# own rtime_last_played and the generic game_platforms.last_played written by
# Nintendo/PSN). Both hltb_main and playtime are required for a signal at all —
# there is nothing to compare playtime against otherwise.
_SUGGESTION_SQL = f"""
WITH rollup AS (
    SELECT g.id AS game_id, g.name, g.hltb_main,
           {_PLAYTIME_SUM_SQL} AS playtime_minutes,
           MAX(gp.last_played) AS last_played,
           MAX(spd.rtime_last_played) AS rtime_last_played
    FROM games g
    JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    WHERE g.completion_status IS NULL
      AND g.is_primary_library_item = 1
      AND g.is_farmed = 0
    GROUP BY g.id
)
SELECT * FROM rollup
WHERE playtime_minutes IS NOT NULL AND playtime_minutes > 0 AND hltb_main IS NOT NULL
"""


def _freshest_activity(
    last_played: str | None, rtime_last_played: int | None
) -> tuple[datetime | None, str | None]:
    """Pick the more recent of the two last-played signals.

    Returns (moment, display_date) so callers can both age-check and render a
    human date string; (None, None) when neither signal is present.
    """
    candidates: list[tuple[datetime, str]] = []
    if last_played:
        try:
            moment = datetime.fromisoformat(last_played)
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            candidates.append((moment, last_played))
    if rtime_last_played:
        moment = datetime.fromtimestamp(rtime_last_played, tz=timezone.utc)
        candidates.append((moment, moment.date().isoformat()))
    if not candidates:
        return None, None
    return max(candidates, key=lambda c: c[0])


async def suggest_completion_status(limit: int = 25) -> dict:
    """
    Suggest completion statuses for games you haven't classified yet.

    Read-only heuristic — nothing is written. Confirm a suggestion with
    update_game(game_id=..., completion_status=...). Two signals:
    - completed: total playtime >= HowLongToBeat main-story hours
    - abandoned: at least 2h played, under half of HLTB main, and no activity
      for 12+ months (Steam rtime_last_played / game_platforms.last_played)
    Games already given a completion_status, farmed games, and non-primary
    library items (DLC/expansions/editions) are never suggested. Results are
    ordered by confidence: completed suggestions first (highest playtime/HLTB
    ratio first), then abandoned suggestions (staler first).
    """
    limit = _clamp_limit(limit)
    async with get_db() as db:
        rows = await db.execute_fetchall(_SUGGESTION_SQL)

    completed: list[tuple[float, dict]] = []
    abandoned: list[tuple[int, dict]] = []
    for row in rows:
        playtime_minutes = row["playtime_minutes"]
        hltb_main = row["hltb_main"]
        hltb_minutes = hltb_main * 60
        if hltb_minutes <= 0:
            continue
        playtime_hours = round(playtime_minutes / 60, 1)
        ratio = playtime_minutes / hltb_minutes

        if playtime_minutes >= hltb_minutes:
            completed.append(
                (
                    ratio,
                    {
                        "game_id": row["game_id"],
                        "name": row["name"],
                        "suggested_status": "completed",
                        "reason": (
                            f"Played {round(playtime_hours)}h of a "
                            f"{round(hltb_main)}h game"
                        ),
                        "playtime_hours": playtime_hours,
                        "hltb_main": hltb_main,
                        "last_played": _freshest_activity(
                            row["last_played"], row["rtime_last_played"]
                        )[1],
                    },
                )
            )
            continue

        if playtime_minutes < _ABANDONED_MIN_MINUTES:
            continue
        if ratio >= _ABANDONED_MAX_HLTB_RATIO:
            continue
        moment, display_date = _freshest_activity(
            row["last_played"], row["rtime_last_played"]
        )
        if moment is None:
            continue
        age_days = (datetime.now(timezone.utc) - moment).days
        if age_days < _ABANDONED_MIN_STALE_DAYS:
            continue
        abandoned.append(
            (
                age_days,
                {
                    "game_id": row["game_id"],
                    "name": row["name"],
                    "suggested_status": "abandoned",
                    "reason": (
                        f"Played {playtime_hours}h of {round(hltb_main)}h, "
                        f"last touched {display_date}"
                    ),
                    "playtime_hours": playtime_hours,
                    "hltb_main": hltb_main,
                    "last_played": display_date,
                },
            )
        )

    completed.sort(key=lambda entry: entry[0], reverse=True)
    abandoned.sort(key=lambda entry: entry[0], reverse=True)
    suggestions = [entry for _, entry in completed] + [entry for _, entry in abandoned]
    suggestions = suggestions[:limit]

    return {
        "suggestions": suggestions,
        "count": len(suggestions),
    }
