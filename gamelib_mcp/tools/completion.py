"""Read-only completion heuristic behind check_library's completion.unclassified."""

from datetime import UTC, datetime

from ..data.db import get_db
from .common import PLAYTIME_SUM_SQL as _PLAYTIME_SUM_SQL
from .common import clamp_limit as _clamp_limit

# Abandoned candidates need enough playtime to rule out "hasn't started yet"
# and enough staleness to rule out "still actively playing, just slowly".
_ABANDONED_MIN_MINUTES = 120
_ABANDONED_MAX_HLTB_RATIO = 0.5
_ABANDONED_MIN_STALE_DAYS = 365

# Evergreen candidates: playtime far beyond any finite completion signal —
# the shape of an endless/sandbox game (Rocket League, Tabletop Simulator,
# MMOs) rather than a story you'd ever mark "completed". Two conservative,
# deterministic branches:
#  - hltb_main present and not near-zero: playtime is a large multiple of it.
#  - hltb_main absent or near-zero (no useful story-length signal at all):
#    playtime alone must be substantial on its own.
_EVERGREEN_MIN_HLTB_RATIO = 3.0
_EVERGREEN_NEAR_ZERO_HLTB_HOURS = 1.0
_EVERGREEN_MIN_MINUTES_NO_HLTB = 40 * 60

# One rollup per currently-unclassified, non-farmed primary-library game: total
# playtime plus the freshest last-played signal across both sources (Steam's
# own rtime_last_played and the generic game_platforms.last_played written by
# Nintendo/PSN). hltb_main is intentionally NOT required here (unlike the old
# completed/abandoned-only version) — the no-HLTB evergreen branch needs rows
# where it's NULL to be visible at all; playtime is still required since
# there's nothing to suggest from with none.
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
WHERE playtime_minutes IS NOT NULL AND playtime_minutes > 0
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
                moment = moment.replace(tzinfo=UTC)
            candidates.append((moment, last_played))
    if rtime_last_played:
        moment = datetime.fromtimestamp(rtime_last_played, tz=UTC)
        candidates.append((moment, moment.date().isoformat()))
    if not candidates:
        return None, None
    return max(candidates, key=lambda c: c[0])


async def suggest_completion_status(limit: int = 25) -> dict:
    """
    Suggest completion statuses for games you haven't classified yet.

    Read-only heuristic — nothing is written. Confirm a suggestion with
    update_game(game_id=..., completion_status=...). Three signals:
    - evergreen: playtime is >= 3x HowLongToBeat main-story hours, or (when
      HLTB main is missing/near-zero and so gives no usable signal) total
      playtime alone is substantial (40h+). Endless/sandbox games (Rocket
      League, Tabletop Simulator, MMOs) fit this shape, not a "completed" run.
    - completed: total playtime >= HowLongToBeat main-story hours
    - abandoned: at least 2h played, under half of HLTB main, and no activity
      for 12+ months (Steam rtime_last_played / game_platforms.last_played)
    Games already given a completion_status (including evergreen), farmed
    games, and non-primary library items (DLC/expansions/editions) are never
    suggested. Results are ordered by confidence within each signal: completed
    first (highest playtime/HLTB ratio), then evergreen (highest playtime),
    then abandoned (staler first).
    """
    limit = _clamp_limit(limit)
    async with get_db() as db:
        rows = await db.execute_fetchall(_SUGGESTION_SQL)

    completed: list[tuple[float, dict]] = []
    evergreen: list[tuple[float, dict]] = []
    abandoned: list[tuple[int, dict]] = []
    for row in rows:
        playtime_minutes = row["playtime_minutes"]
        hltb_main = row["hltb_main"]
        playtime_hours = round(playtime_minutes / 60, 1)

        if hltb_main is None or hltb_main <= _EVERGREEN_NEAR_ZERO_HLTB_HOURS:
            # No reliable story-length signal to compare against; require a
            # large playtime on its own before suggesting anything.
            if playtime_minutes >= _EVERGREEN_MIN_MINUTES_NO_HLTB:
                evergreen.append(
                    (
                        playtime_hours,
                        {
                            "game_id": row["game_id"],
                            "name": row["name"],
                            "suggested_status": "evergreen",
                            "reason": (
                                f"Played {round(playtime_hours)}h with no useful "
                                "HowLongToBeat signal — likely an endless/sandbox game"
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

        hltb_minutes = hltb_main * 60
        ratio = playtime_minutes / hltb_minutes

        if ratio >= _EVERGREEN_MIN_HLTB_RATIO:
            evergreen.append(
                (
                    playtime_hours,
                    {
                        "game_id": row["game_id"],
                        "name": row["name"],
                        "suggested_status": "evergreen",
                        "reason": (
                            f"Played {round(playtime_hours)}h — {round(ratio, 1)}x "
                            f"the {round(hltb_main)}h main story; looks like a "
                            "game with no fixed ending"
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
        age_days = (datetime.now(UTC) - moment).days
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
    evergreen.sort(key=lambda entry: entry[0], reverse=True)
    abandoned.sort(key=lambda entry: entry[0], reverse=True)
    suggestions = (
        [entry for _, entry in completed]
        + [entry for _, entry in evergreen]
        + [entry for _, entry in abandoned]
    )
    suggestions = suggestions[:limit]

    return {
        "suggestions": suggestions,
        "count": len(suggestions),
    }
