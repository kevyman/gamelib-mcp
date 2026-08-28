"""The neutral game representation: trailer, screenshots, similar-you-own.

Two read paths render the same thing about a game — ``record_assessment``'s
evaluation package and ``get_game_detail(media=True)`` — so the media payload
and its library-annotated similar-games block are assembled once here instead
of once per caller. Nothing in this module is verdict-aware: it describes the
GAME, not an opinion about it.

The block shapes are frozen (two widgets render them). ``media`` is whatever
``data.media.get_game_media`` returned for its source, untouched; ``similar``
is IGDB's similar games annotated with what the library owns, capped like
every other growing list.

Imports stay one-way — data/* plus tools/common only — so tools/detail.py and
tools/assessment.py (which itself imports detail) can both depend on this
without an import cycle.
"""

import logging
from typing import Any

from ..data.db import get_db
from ..data.media import get_game_media
from .common import IGDB_COVER_URL, OWNED_SQL, PLAYTIME_SUM_SQL

logger = logging.getLogger(__name__)

# A card shows a row of neighbours, not IGDB's whole similarity list.
SIMILAR_ITEM_CAP = 8

# Ownership/playtime/rating for the similar games, keyed by IGDB id. Narrower
# than tools/assessment.py's package annotation query (which also feeds anchors
# and comparisons, and needs names, covers and HLTB): a similar-games chip only
# renders what is selected here, and the entries carry IGDB's own name, year
# and cover. Same rating priority as every other "my rating" rollup —
# full-weight sources first, then lowest id — and the same owned-only playtime.
_SIMILAR_ANNOTATION_SQL = f"""
SELECT g.igdb_id AS igdb_id,
       {OWNED_SQL} AS owned,
       (
           SELECT {PLAYTIME_SUM_SQL} FROM game_platforms gp
           WHERE gp.game_id = g.id AND gp.owned = 1
       ) AS playtime_minutes,
       (
           SELECT rt.normalized_score FROM ratings rt
           WHERE rt.game_id = g.id AND rt.normalized_score IS NOT NULL
           ORDER BY CASE rt.source WHEN 'manual' THEN 0 WHEN 'backloggd' THEN 1
                    ELSE 2 END, rt.id
           LIMIT 1
       ) AS my_rating
FROM games g
WHERE g.igdb_id IN ({{placeholders}})
"""


def _hours(minutes: float | None) -> float | None:
    return round(minutes / 60, 1) if minutes is not None else None


async def _annotate_by_igdb_id(igdb_ids: list) -> dict[Any, Any]:
    """{igdb_id: row} for the library games among ``igdb_ids``."""
    keys = [value for value in dict.fromkeys(igdb_ids) if value is not None]
    if not keys:
        return {}
    placeholders = ", ".join("?" * len(keys))
    async with get_db() as db:
        rows = await db.execute_fetchall(
            _SIMILAR_ANNOTATION_SQL.format(placeholders=placeholders), keys
        )
    return {row["igdb_id"]: row for row in rows}


async def annotate_similar_games(
    similar_raw: list[dict[str, Any]], total: int | None
) -> dict[str, Any]:
    """IGDB's similar games, annotated with ownership — the play-what-you-own view."""
    library = await _annotate_by_igdb_id([entry.get("igdb_id") for entry in similar_raw])
    items: list[dict[str, Any]] = []
    for entry in similar_raw[:SIMILAR_ITEM_CAP]:
        row = library.get(entry.get("igdb_id"))
        owned = bool(row["owned"]) if row is not None else False
        playtime = row["playtime_minutes"] if row is not None else None
        items.append(
            {
                "igdb_id": entry.get("igdb_id"),
                "name": entry.get("name"),
                "release_year": entry.get("release_year"),
                "cover_url": (
                    IGDB_COVER_URL.format(image_id=entry["cover_image_id"])
                    if entry.get("cover_image_id")
                    else None
                ),
                "owned": owned,
                "unplayed": owned and not playtime,
                "my_rating": row["my_rating"] if row is not None else None,
                "playtime_hours": _hours(playtime),
            }
        )
    count = total if isinstance(total, int) else len(similar_raw)
    return {"items": items, "count": count, "truncated": count > len(items)}


async def media_context(payload: dict | None) -> dict[str, Any]:
    """``{"media", "similar"}`` from one ``get_game_media`` payload.

    Split from the fetch so a caller that already ran (and time-boxed) its own
    ``get_game_media`` call — record_assessment's package path — shapes the
    result through exactly this code rather than a copy of it. Either member is
    None when the source had nothing of that kind.
    """
    if not payload:
        return {"media": None, "similar": None}
    similar_raw = payload.get("similar_raw")
    return {
        "media": payload.get("media"),
        "similar": (
            await annotate_similar_games(similar_raw, payload.get("similar_count"))
            if similar_raw
            else None
        ),
    }


async def game_media_context(
    *,
    steam_appid: int | None,
    igdb_id: int | None,
    name: str | None,
) -> dict[str, Any]:
    """Fetch + shape: ``{"media": …|None, "similar": …|None}`` for one game.

    Identity resolution is get_game_media's (Steam appid, then IGDB id, then an
    exact-name IGDB lookup), and so is the failure stance: a provider failure
    comes back as an empty context, never an exception. Anything else — a DB
    error annotating the similar games — propagates, and the caller decides
    what a missing trailer costs it.
    """
    payload = await get_game_media(steam_appid=steam_appid, igdb_id=igdb_id, name=name)
    return await media_context(payload)
