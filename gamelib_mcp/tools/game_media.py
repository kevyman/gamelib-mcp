"""The neutral game representation: trailer, screenshots, similar-you-own, pedigree.

Two read paths render the same thing about a game — ``record_assessment``'s
evaluation package and ``get_game_detail(media=True)`` — so the media payload
and its library-annotated similar-games block are assembled once here instead
of once per caller. Nothing in this module is verdict-aware: it describes the
GAME, not an opinion about it.

The block shapes are frozen (two widgets render them). ``media`` is whatever
``data.media.get_game_media`` returned for its source, untouched; ``similar``
is IGDB's similar games annotated with what the library owns, capped like
every other growing list; ``pedigree`` is the developer's own previous games
put through the same annotation, plus the track record that reads out of it.

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
# Mirrors data/media.py's PREVIOUS_GAMES_CAP: the fetch already caps the
# studio's previous games, and this is the second gate on the same row.
PEDIGREE_ITEM_CAP = 6

# Ownership/playtime/rating for IGDB-keyed neighbours — similar games and the
# developer's previous games alike. Narrower than tools/assessment.py's package
# annotation query (which also feeds anchors and comparisons, and needs names,
# covers and HLTB): these chips only render what is selected here, and the
# entries carry IGDB's own name, year and cover. Same rating priority as every
# other "my rating" rollup — full-weight sources first, then lowest id — and
# the same owned-only playtime.
_IGDB_ANNOTATION_SQL = f"""
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
            _IGDB_ANNOTATION_SQL.format(placeholders=placeholders), keys
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
                # NULL playtime is UNKNOWN (GOG, manual adds, mid-import rows),
                # never "unplayed" — the repo-wide three-state convention
                # (PLAY_STATE_SQL). Only an authoritative zero earns the tag.
                "unplayed": owned and playtime == 0,
                "my_rating": row["my_rating"] if row is not None else None,
                "playtime_hours": _hours(playtime),
            }
        )
    count = total if isinstance(total, int) else len(similar_raw)
    return {"items": items, "count": count, "truncated": count > len(items)}


def _track_record(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What HIS library says about the studio's previous games.

    The point of the strip: not "this studio is acclaimed" (critic scores say
    that, and every card already carries them) but "you played four of their
    last six and rated them 8.5". Null when there is nothing to count —
    the header line then stands alone rather than reporting three zeroes.
    """
    if not items:
        return None
    owned = [item for item in items if item["owned"]]
    # Every rated entry counts, owned or not: a rating is his judgement of the
    # studio's work, and a game he rated and later let go still is one.
    ratings = [item["my_rating"] for item in items if item["my_rating"] is not None]
    return {
        "owned_count": len(owned),
        # Owned-and-touched: an owned-but-never-launched game is evidence about
        # the backlog, not about the studio.
        "played_count": sum(1 for item in owned if item["playtime_hours"]),
        "avg_my_rating": round(sum(ratings) / len(ratings), 1) if ratings else None,
    }


async def annotate_pedigree(pedigree_raw: dict[str, Any]) -> dict[str, Any]:
    """The raw pedigree block with its previous games read against the library.

    Everything else passes through untouched — this layer only knows about
    ownership. Under the big-studio damper ``previous_games`` is already empty
    upstream, so the track record comes back null and the widgets render the
    header line alone.
    """
    raw_previous = [
        entry
        for entry in (pedigree_raw.get("previous_games") or [])
        if isinstance(entry, dict)
    ]
    library = await _annotate_by_igdb_id([entry.get("igdb_id") for entry in raw_previous])
    items: list[dict[str, Any]] = []
    for entry in raw_previous[:PEDIGREE_ITEM_CAP]:
        row = library.get(entry.get("igdb_id"))
        items.append(
            {
                "igdb_id": entry.get("igdb_id"),
                "name": entry.get("name"),
                "release_year": entry.get("release_year"),
                "critic_score": entry.get("critic_score"),
                "cover_url": (
                    IGDB_COVER_URL.format(image_id=entry["cover_image_id"])
                    if entry.get("cover_image_id")
                    else None
                ),
                "owned": bool(row["owned"]) if row is not None else False,
                "my_rating": row["my_rating"] if row is not None else None,
                "playtime_hours": (
                    _hours(row["playtime_minutes"]) if row is not None else None
                ),
            }
        )
    return {
        **{key: value for key, value in pedigree_raw.items() if key != "previous_games"},
        "previous_games": items,
        "library_track_record": _track_record(items),
    }


async def media_context(payload: dict | None) -> dict[str, Any]:
    """``{"media", "similar", "pedigree"}`` from one ``get_game_media`` payload.

    Split from the fetch so a caller that already ran (and time-boxed) its own
    ``get_game_media`` call — record_assessment's package path — shapes the
    result through exactly this code rather than a copy of it. Each member is
    None when the source had nothing of that kind.
    """
    if not payload:
        return {"media": None, "similar": None, "pedigree": None}
    similar_raw = payload.get("similar_raw")
    pedigree_raw = payload.get("pedigree_raw")
    return {
        "media": payload.get("media"),
        "similar": (
            await annotate_similar_games(similar_raw, payload.get("similar_count"))
            if similar_raw
            else None
        ),
        "pedigree": (
            await annotate_pedigree(pedigree_raw)
            if isinstance(pedigree_raw, dict)
            else None
        ),
    }


async def game_media_context(
    *,
    steam_appid: int | None,
    igdb_id: int | None,
    name: str | None,
) -> dict[str, Any]:
    """Fetch + shape: ``{"media", "similar", "pedigree"}`` (each …|None) for one game.

    Identity resolution is get_game_media's (Steam appid, then IGDB id, then an
    exact-name IGDB lookup), and so is the failure stance: a provider failure
    comes back as an empty context, never an exception. Anything else — a DB
    error annotating the similar games — propagates, and the caller decides
    what a missing trailer costs it.
    """
    payload = await get_game_media(steam_appid=steam_appid, igdb_id=igdb_id, name=name)
    return await media_context(payload)
