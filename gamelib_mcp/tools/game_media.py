"""The neutral game representation: trailer, screenshots, similar-you-own, pedigree.

Two read paths render the same thing about a game — ``record_assessment``'s
evaluation package and ``get_game_detail(media=True)`` — so the blocks are
assembled once here instead of once per caller. Nothing in this module is
verdict-aware: it describes the GAME, not an opinion about it.

The block shapes are frozen (two widgets render them). ``media`` is whatever
``data.media.get_game_media`` returned for its source, untouched; ``pedigree``
is the developer's own previous games annotated with what the library owns,
plus the track record that reads out of it.

``similar`` is NOT a provider block at all. It used to be IGDB's
``similar_games`` field, which was unreliable enough that the row rarely
described the game it sat under; it is now ``similar_in_library`` — cosine
similarity over the same IDF-weighted community-tag vocabulary
``discover_games`` scores with, restricted to owned primary library items. That
matches what the row was always FOR ("games you already own that are like this
one"), needs no network at all, and can only name games he can actually go
play. Its callers attach it themselves, so a provider outage costs the trailer
and never the neighbours.

Imports stay one-way — data/* plus tools/common only — so tools/detail.py and
tools/assessment.py (which itself imports detail) can both depend on this
without an import cycle.
"""

import logging
import math
from typing import Any

from ..data.db import get_db
from ..data.media import get_game_media
from .common import (
    IGDB_COVER_URL,
    OWNED_SQL,
    PLAYTIME_SUM_SQL,
    STEAM_APPID_SQL,
    cover_url,
)

logger = logging.getLogger(__name__)

# A card shows a row of neighbours, not the whole ranked tail.
SIMILAR_ITEM_CAP = 8
# Only the first 20 prominence-ordered tags count, on BOTH sides. games.tags is
# vote-ranked (data/steamspy.py), and past the head of that list the tags stop
# describing the game and start describing the store page — the same reason
# discover_games' vibe filter has VIBE_TAG_PROMINENCE_CUTOFF.
SIMILAR_TAG_WINDOW = 20
# Below this the source has no tag evidence to reason from, and a "similar"
# row built on two tags is a guess with a confident face. None, not a short list.
SIMILAR_MIN_SOURCE_TAGS = 3
# A neighbour has to agree with the source on more than a genre and a mood.
SIMILAR_MIN_SHARED_TAGS = 3
# …and the shared tags have to be most of what either game IS. Measured on the
# live library: at cosine >= 0.40, Hollow Knight keeps 71 qualifying neighbours
# (weakest: Sundered, Shadow Complex, Yoku's Island Express) and Stardew Valley
# 23 (weakest: Unpacking), while 0.30 already admits Jedi: Fallen Order and The
# Escapists 2. Without a floor the shared-tag gate alone qualifies HALF the
# library on three generic tags — 1853 neighbours for Hollow Knight, 1562 for
# Stardew Valley — which makes the card's own count meaningless.
SIMILAR_MIN_SIMILARITY = 0.4
# The "why" under each cover: three tags fit on one line on a phone.
SIMILAR_WHY_CAP = 3
# Same floor and the same reason as discover._IDF_DF_FLOOR: a tag on one or two
# library games is not maximally informative, it is unmeasured, and without the
# floor those sampling accidents would dominate every neighbour list.
_SIMILAR_IDF_DF_FLOOR = 5
# Prominence decay p(rank) = 1 / (1 + rank / 8): the top tag counts double the
# 8th and roughly triple the 16th, so the head of the vote-ranked list drives
# the match without the tail being discarded outright.
_SIMILAR_PROMINENCE_HALF = 8.0

# Mirrors data/media.py's PREVIOUS_GAMES_CAP: the fetch already caps the
# studio's previous games, and this is the second gate on the same row.
PEDIGREE_ITEM_CAP = 6

# Ownership/playtime/rating for the IGDB-keyed entries of the PEDIGREE row.
# Narrower than tools/assessment.py's package annotation query (which also feeds
# anchors and comparisons, and needs names, covers and HLTB): these chips only
# render what is selected here, and the entries carry IGDB's own name, year and
# cover. Same rating priority as every other "my rating" rollup — full-weight
# sources first, then lowest id — and the same owned-only playtime.
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


# ── Similar in your library ──────────────────────────────────────────────────
#
# One statement, because every intermediate is a set the next one needs and
# round-tripping them through Python would move ~70k tag rows (3.5k owned
# primaries x the 20-tag window) across the aiosqlite boundary. The CTEs are
# MATERIALIZED for the same reason discover.py keeps its rollup in one: without
# it SQLite is free to re-run `owned` once per reference, and the tag scan is
# the expensive part.
#
# The cosine denominator is finished in PYTHON rather than SQL: sqrt() only
# exists in a build compiled with SQLITE_ENABLE_MATH_FUNCTIONS, which is the
# exact dependency gl_ln was written to avoid (data/db/__init__.py). Ranking
# still happens in SQL — for non-negative dot products ordering by
# dot^2 / (|s|^2 |c|^2) is ordering by the cosine itself — so the cap, the
# true count and the tie-break all stay where the rows are.
_SIMILAR_IDF_SQL = (
    f"gl_ln(1.0 + p.n * 1.0 / max(COALESCE(df.df, 0), {_SIMILAR_IDF_DF_FLOOR}))"
)

_SIMILAR_SQL = f"""
WITH src AS MATERIALIZED (
    SELECT g.id AS id, g.name AS name, g.name_normalized AS name_normalized,
           g.parent_game_id AS parent_game_id, g.tags AS tags
    FROM games g
    WHERE g.id = ?
),
src_tags AS MATERIALIZED (
    SELECT lower(je.value) AS tag, MIN(je.key) AS rank
    FROM src, json_each(COALESCE(src.tags, '[]')) je
    WHERE je.key < {SIMILAR_TAG_WINDOW}
    GROUP BY lower(je.value)
),
owned AS MATERIALIZED (
    SELECT g.id AS game_id, g.tags AS tags
    FROM games g
    WHERE g.is_primary_library_item = 1 AND {OWNED_SQL}
),
p AS MATERIALIZED (SELECT COUNT(*) AS n FROM owned),
cand_tags AS MATERIALIZED (
    SELECT owned.game_id AS game_id, lower(je.value) AS tag, MIN(je.key) AS rank
    FROM owned, json_each(COALESCE(owned.tags, '[]')) je
    WHERE je.key < {SIMILAR_TAG_WINDOW}
    GROUP BY owned.game_id, lower(je.value)
),
df AS MATERIALIZED (
    SELECT tag, COUNT(*) AS df FROM cand_tags GROUP BY tag
),
src_w AS MATERIALIZED (
    SELECT st.tag AS tag,
           {_SIMILAR_IDF_SQL} * (1.0 / (1.0 + st.rank / {_SIMILAR_PROMINENCE_HALF}))
               AS sw
    FROM src_tags st
    CROSS JOIN p
    LEFT JOIN df ON df.tag = st.tag
),
src_norm AS MATERIALIZED (SELECT SUM(sw * sw) AS n2 FROM src_w),
cand_w AS MATERIALIZED (
    SELECT ct.game_id AS game_id, ct.tag AS tag,
           {_SIMILAR_IDF_SQL} * (1.0 / (1.0 + ct.rank / {_SIMILAR_PROMINENCE_HALF}))
               AS cw
    FROM cand_tags ct
    CROSS JOIN p
    LEFT JOIN df ON df.tag = ct.tag
),
cand_norm AS MATERIALIZED (
    SELECT game_id, SUM(cw * cw) AS n2 FROM cand_w GROUP BY game_id
),
-- A duplicate row of the same game, the edition it hangs off, and the DLC
-- hanging off it are not neighbours: they are the same thing twice, and they
-- score near-perfectly because of it.
eligible AS MATERIALIZED (
    SELECT g.id AS game_id
    FROM games g
    JOIN owned o ON o.game_id = g.id
    CROSS JOIN src
    WHERE g.id <> src.id
      AND (src.parent_game_id IS NULL OR g.id <> src.parent_game_id)
      AND (g.parent_game_id IS NULL OR g.parent_game_id <> src.id)
      AND g.name <> src.name
      AND (src.name_normalized IS NULL
           OR g.name_normalized IS NULL
           OR g.name_normalized <> src.name_normalized)
),
scored AS MATERIALIZED (
    SELECT c.game_id AS game_id,
           SUM(s.sw * c.cw) AS dot,
           COUNT(*) AS shared
    FROM src_w s
    JOIN cand_w c ON c.tag = s.tag
    WHERE c.game_id IN (SELECT game_id FROM eligible)
      AND (SELECT COUNT(*) FROM src_tags) >= {SIMILAR_MIN_SOURCE_TAGS}
    GROUP BY c.game_id
    HAVING COUNT(*) >= {SIMILAR_MIN_SHARED_TAGS}
),
-- rank_key is the SQUARED cosine (see above), so the similarity floor is
-- applied as its square. It lands HERE, in its own CTE, so that COUNT(*) OVER
-- () below counts neighbours that cleared the floor — `count` is what the card
-- claims, and "8 of your 1853" is not a claim worth making.
qualified AS MATERIALIZED (
    SELECT sc.game_id AS game_id,
           sc.dot AS dot,
           cn.n2 AS cand_n2,
           (sc.dot * sc.dot) / (cn.n2 * (SELECT n2 FROM src_norm)) AS rank_key
    FROM scored sc
    JOIN cand_norm cn ON cn.game_id = sc.game_id
    WHERE (sc.dot * sc.dot) / (cn.n2 * (SELECT n2 FROM src_norm))
          >= {SIMILAR_MIN_SIMILARITY**2}
),
-- COUNT(*) OVER () is evaluated before this SELECT's own ORDER BY/LIMIT, so it
-- is the TRUE number of qualifying neighbours, not the number shown.
ranked AS (
    SELECT q.game_id AS game_id,
           q.dot AS dot,
           q.cand_n2 AS cand_n2,
           q.rank_key AS rank_key,
           COUNT(*) OVER () AS total
    FROM qualified q
    ORDER BY q.rank_key DESC, q.game_id ASC
    LIMIT {SIMILAR_ITEM_CAP}
)
SELECT r.game_id AS game_id,
       r.total AS total,
       r.rank_key AS rank_key,
       r.dot AS dot,
       r.cand_n2 AS cand_n2,
       (SELECT n2 FROM src_norm) AS src_n2,
       g.igdb_id AS igdb_id,
       g.name AS name,
       g.release_date AS release_date,
       g.cover_image_id AS cover_image_id,
       {STEAM_APPID_SQL} AS steam_appid,
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
       ) AS my_rating,
       s.tag AS shared_tag
FROM ranked r
JOIN games g ON g.id = r.game_id
JOIN cand_w c ON c.game_id = r.game_id
JOIN src_w s ON s.tag = c.tag
ORDER BY r.rank_key DESC, r.game_id ASC, s.sw * c.cw DESC, s.tag ASC
"""


def _similar_release_year(release_date: str | None) -> int | None:
    """Year component of a stored release_date ('2008-10-13'), or None."""
    if not release_date:
        return None
    head = str(release_date).strip()[:4]
    return int(head) if head.isdigit() else None


async def similar_in_library(game_id: int) -> dict[str, Any] | None:
    """The owned games most like ``game_id``, by shared community tags.

    Cosine similarity between two IDF-weighted, prominence-decayed tag vectors,
    over the first ``SIMILAR_TAG_WINDOW`` entries of each side's vote-ranked
    ``games.tags``. The candidate pool is owned primary library items only, and
    both the IDF and its document-frequency floor are discover_games' — the row
    is the same taste vocabulary, asked a different question ("what else here is
    like this?" rather than "what here would he like?").

    A neighbour has to clear two gates, not one: ``SIMILAR_MIN_SHARED_TAGS``
    shared windowed tags AND a cosine of ``SIMILAR_MIN_SIMILARITY``. The
    shared-tag gate alone qualifies half the library on three generic tags, so
    ``count`` — the number the card reads out — would be meaningless without
    the floor.

    None — never an empty block — when the source carries too little tag
    evidence to reason from, or when nothing clears both gates.
    """
    async with get_db() as db:
        rows = await db.execute_fetchall(_SIMILAR_SQL, (game_id,))
    if not rows:
        return None

    items: list[dict[str, Any]] = []
    why: dict[int, list[str]] = {}
    for row in rows:
        entry_tags = why.setdefault(row["game_id"], [])
        if len(entry_tags) < SIMILAR_WHY_CAP:
            entry_tags.append(row["shared_tag"])
        if len(items) and items[-1]["game_id"] == row["game_id"]:
            continue
        playtime = row["playtime_minutes"]
        # The cosine itself, finished here (see _SIMILAR_SQL on why sqrt is not
        # a SQL call). The denominator is strictly positive for any row that
        # reached this point — a qualifying candidate has tags, and so does the
        # source.
        score = row["dot"] / math.sqrt(row["src_n2"] * row["cand_n2"])
        items.append(
            {
                "game_id": row["game_id"],
                "igdb_id": row["igdb_id"],
                "name": row["name"],
                "release_year": _similar_release_year(row["release_date"]),
                "cover_url": cover_url(row["cover_image_id"], row["steam_appid"]),
                # Always true: the pool IS the owned library. Kept because the
                # two widgets read the key (ownership stickers) and the pedigree
                # row beside it genuinely varies.
                "owned": True,
                # NULL playtime is UNKNOWN (GOG, manual adds, mid-import rows),
                # never "unplayed" — the repo-wide three-state convention
                # (PLAY_STATE_SQL). Only an authoritative zero earns the tag.
                "unplayed": playtime == 0,
                "my_rating": row["my_rating"],
                "playtime_hours": _hours(playtime),
                "similarity": round(score, 2),
                "shared_tags": entry_tags,
            }
        )
    total = rows[0]["total"]
    return {"items": items, "count": total, "truncated": total > len(items)}


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
    """``{"media", "pedigree"}`` from one ``get_game_media`` payload.

    Split from the fetch so a caller that already ran (and time-boxed) its own
    ``get_game_media`` call — record_assessment's package path — shapes the
    result through exactly this code rather than a copy of it. Each member is
    None when the source had nothing of that kind.

    The ``similar`` row is deliberately NOT here: it is library-internal
    (``similar_in_library``), owes nothing to a provider, and callers attach it
    themselves so a provider outage cannot cost them the neighbours.
    """
    if not payload:
        return {"media": None, "pedigree": None}
    pedigree_raw = payload.get("pedigree_raw")
    return {
        "media": payload.get("media"),
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
    """Fetch + shape: ``{"media", "pedigree"}`` (each …|None) for one game.

    Identity resolution is get_game_media's (Steam appid, then IGDB id, then an
    exact-name IGDB lookup), and so is the failure stance: a provider failure
    comes back as an empty context, never an exception. Anything else — a DB
    error annotating the pedigree — propagates, and the caller decides what a
    missing trailer costs it.
    """
    payload = await get_game_media(steam_appid=steam_appid, igdb_id=igdb_id, name=name)
    return await media_context(payload)
