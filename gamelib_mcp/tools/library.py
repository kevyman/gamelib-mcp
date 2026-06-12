"""search_games and get_library_stats tools."""

from typing import Literal

from fastmcp.exceptions import ToolError

from ..data.db import get_db, load_platforms_for_games
from ..data.title_normalization import normalize_search_text
from .common import (
    STEAM_APPID_SQL as _STEAM_APPID_SQL,
    clamp_limit as _clamp_limit,
    resolve_platform as _resolve_platform,
)
from .search import build_name_match, fuzzy_fallback_game_ids

VALID_FILTERS = {"all", "unplayed", "played", "recent", "farmed"}

ResponseFormat = Literal["concise", "detailed"]

SORT_COLUMNS = {
    "playtime": "total_playtime_minutes",
    "name": "name",
    "metacritic": "metacritic_score",
    "opencritic": "opencritic_score",
    "hltb": "hltb_main",
}

# NOTE: this CTE is library-specific — it aggregates total_playtime_2weeks_minutes,
# which the discover/stats variants do not. Do not merge them without checking output.
_GAME_ROLLUP_CTE = f"""
WITH game_rollup AS (
    SELECT g.id AS game_id,
           g.name,
           COALESCE(g.name_normalized, lower(g.name)) AS name_normalized,
           {_STEAM_APPID_SQL} AS steam_appid,
           g.tags,
           g.genres,
           g.hltb_main,
           g.is_farmed,
           COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes,
           COALESCE(SUM(COALESCE(gp.playtime_2weeks_minutes, 0)), 0) AS total_playtime_2weeks_minutes,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.protondb_tier END) AS protondb_tier,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.steam_review_desc END) AS steam_review_desc,
           MAX(gpe.metacritic_score) AS metacritic_score,
           MAX(gpe.opencritic_score) AS opencritic_score
    FROM games g
    LEFT JOIN game_platforms gp ON gp.game_id = g.id
    LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
    LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
    GROUP BY g.id
)
"""


async def search_games(
    query: str,
    limit: int = 20,
    offset: int = 0,
    platform: str | None = None,
    response_format: ResponseFormat = "concise",
) -> dict:
    """Find games in the library by name, optionally filtered by platform.

    Matching is punctuation-insensitive and token-based ("sekiro shadow" finds
    "Sekiro: Shadows Die Twice"); when nothing matches, a fuzzy fallback
    catches misspellings and tags those results with match_type="fuzzy".
    """
    limit = _clamp_limit(limit)
    platform = _resolve_platform(platform)
    match = build_name_match(query)
    conditions = [match.where_sql]
    params: list = list(match.where_params)
    if platform:
        conditions.append(
            "game_id IN (SELECT game_id FROM game_platforms WHERE platform = ? AND owned = 1)"
        )
        params.append(platform)
    where = " AND ".join(conditions)
    async with get_db() as db:
        total = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT COUNT(*) AS c
            FROM game_rollup
            WHERE {where}
            """,
            tuple(params),
        )
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT *, {match.rank_sql} AS match_rank
            FROM game_rollup
            WHERE {where}
            ORDER BY match_rank ASC, total_playtime_minutes DESC, name ASC
            LIMIT ?
            OFFSET ?
            """,
            (*match.rank_params, *params, limit, offset),
        )

    if total["c"] == 0 and match.fuzzy_eligible:
        fuzzy_results = await _fuzzy_search(query, platform, limit, offset, response_format)
        if fuzzy_results is not None:
            return fuzzy_results

    return _envelope(
        await _format_rows(rows, response_format=response_format),
        total["c"],
        limit,
        offset,
    )


async def _fuzzy_search(
    query: str,
    platform: str | None,
    limit: int,
    offset: int,
    response_format: ResponseFormat,
) -> dict | None:
    """LIKE tiers found nothing — retry with the fuzzy matcher. None = no match."""
    game_ids = await fuzzy_fallback_game_ids(query)
    if not game_ids:
        return None

    placeholders = ",".join("?" * len(game_ids))
    conditions = [f"game_id IN ({placeholders})"]
    params: list = list(game_ids)
    if platform:
        conditions.append(
            "game_id IN (SELECT game_id FROM game_platforms WHERE platform = ? AND owned = 1)"
        )
        params.append(platform)
    async with get_db() as db:
        total = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT COUNT(*) AS c
            FROM game_rollup
            WHERE {' AND '.join(conditions)}
            """,
            tuple(params),
        )
        if total["c"] == 0:
            return None

        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT *
            FROM game_rollup
            WHERE {' AND '.join(conditions)}
            ORDER BY total_playtime_minutes DESC, name ASC
            LIMIT ?
            OFFSET ?
            """,
            (*params, limit, offset),
        )

    results = await _format_rows(rows, response_format=response_format)
    for game in results:
        game["match_type"] = "fuzzy"
    return _envelope(results, total["c"], limit, offset)


async def search_games_batch(
    queries: list[str],
    limit_per_query: int = 5,
) -> dict[str, list[dict]]:
    """Look up multiple game names in one call. Returns dict keyed by query."""
    limit_per_query = _clamp_limit(limit_per_query)
    results = {}
    async with get_db() as db:
        for query in queries:
            match = build_name_match(query)
            rows = await db.execute_fetchall(
                _GAME_ROLLUP_CTE
                + f"""
                SELECT *, {match.rank_sql} AS match_rank
                FROM game_rollup
                WHERE {match.where_sql}
                ORDER BY match_rank ASC, total_playtime_minutes DESC, name ASC
                LIMIT ?
                """,
                (*match.rank_params, *match.where_params, limit_per_query),
            )
            results[query] = await _format_rows(rows)

    for query, games in results.items():
        if games or not normalize_search_text(query):
            continue
        fuzzy = await _fuzzy_search(query, None, limit_per_query, 0, "detailed")
        if fuzzy is not None:
            results[query] = fuzzy["results"]
    return results


async def get_library_stats(
    filter: str = "all",
    max_hltb_hours: float | None = None,
    min_metacritic: int | None = None,
    protondb_tier: str | None = None,
    sort_by: str = "playtime",
    limit: int = 50,
    offset: int = 0,
    platform: str | None = None,
    response_format: ResponseFormat = "concise",
    min_opencritic: int | None = None,
    tags: list[str] | None = None,
    genres: list[str] | None = None,
) -> dict:
    """
    Return filtered/sorted game list plus aggregate stats.

    filter: all | unplayed | played | recent | farmed
    sort_by: playtime | name | metacritic | opencritic | hltb
    platform: steam | epic | gog | ps5 | nintendo | switch2 (optional — filter to games owned on that platform)
    tags / genres: case-insensitive; a game must carry EVERY listed entry.

    Note: min_metacritic, min_opencritic, and max_hltb_hours exclude games with
    no score / no HLTB data (NULL), so even min_metacritic=0 drops unscored games.
    """
    limit = _clamp_limit(limit)
    if filter not in VALID_FILTERS:
        raise ToolError(f"Unknown filter '{filter}'. Valid: {sorted(VALID_FILTERS)}")
    if sort_by not in SORT_COLUMNS:
        raise ToolError(f"Unknown sort_by '{sort_by}'. Valid: {sorted(SORT_COLUMNS)}")
    if protondb_tier is not None:
        from ..data.protondb import TIER_ORDER

        if protondb_tier.lower() not in TIER_ORDER:
            raise ToolError(
                f"Unknown protondb_tier '{protondb_tier}'. Valid: {list(TIER_ORDER)}"
            )

    conditions = []
    params: list = []

    if filter == "unplayed":
        conditions.append("(total_playtime_minutes = 0 OR is_farmed = 1)")
    elif filter == "played":
        conditions.append("(total_playtime_minutes > 0 AND is_farmed = 0)")
    elif filter == "recent":
        conditions.append("total_playtime_2weeks_minutes > 0")
    elif filter == "farmed":
        conditions.append("is_farmed = 1")

    if max_hltb_hours is not None:
        conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    if min_metacritic is not None:
        conditions.append("metacritic_score >= ?")
        params.append(min_metacritic)

    if min_opencritic is not None:
        conditions.append("opencritic_score >= ?")
        params.append(min_opencritic)

    for column, wanted in (("tags", tags), ("genres", genres)):
        for entry in wanted or []:
            conditions.append(
                f"""EXISTS (
                    SELECT 1 FROM json_each(COALESCE({column}, '[]'))
                    WHERE lower(value) = ?
                )"""
            )
            params.append(entry.lower())

    if protondb_tier is not None:
        from ..data.protondb import TIER_ORDER

        tier_lower = protondb_tier.lower()
        min_rank = TIER_ORDER.index(tier_lower) if tier_lower in TIER_ORDER else 999
        allowed = [tier for index, tier in enumerate(TIER_ORDER) if index <= min_rank]
        placeholders = ",".join("?" * len(allowed))
        conditions.append(f"lower(COALESCE(protondb_tier, '')) IN ({placeholders})")
        params.extend(allowed)

    platform = _resolve_platform(platform)
    if platform:
        conditions.append(
            "game_id IN (SELECT game_id FROM game_platforms WHERE platform = ? AND owned = 1)"
        )
        params.append(platform)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sort_col = SORT_COLUMNS.get(sort_by, "total_playtime_minutes")
    sort_dir = "ASC" if sort_by == "name" else "DESC"

    async with get_db() as db:
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT *
            FROM game_rollup
            {where}
            ORDER BY {sort_col} {sort_dir} NULLS LAST, name ASC
            LIMIT ?
            OFFSET ?
            """,
            (*params, limit, offset),
        )
        summary = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT COUNT(*) AS total_games,
                   SUM(CASE WHEN total_playtime_minutes > 0 AND is_farmed = 0 THEN 1 ELSE 0 END) AS played,
                   SUM(CASE WHEN total_playtime_minutes = 0 OR is_farmed = 1 THEN 1 ELSE 0 END) AS unplayed,
                   SUM(CASE WHEN is_farmed = 1 THEN 1 ELSE 0 END) AS farmed_games,
                   SUM(total_playtime_minutes) AS total_minutes
            FROM game_rollup
            {where}
            """,
            tuple(params),
        )

    return {
        "total_games": summary["total_games"],
        "played": summary["played"] or 0,
        "unplayed": summary["unplayed"] or 0,
        "farmed_games": summary["farmed_games"] or 0,
        "total_playtime_hours": round((summary["total_minutes"] or 0) / 60, 1),
        "filter": filter,
        "sort_by": sort_by,
        "results": await _format_rows(rows, response_format=response_format),
        "total_matches": summary["total_games"],
        "has_more": offset + len(rows) < summary["total_games"],
    }


async def _format_rows(rows, response_format: ResponseFormat = "detailed") -> list[dict]:
    platforms_by_game = (
        await load_platforms_for_games(row["game_id"] for row in rows)
        if response_format == "detailed"
        else {}
    )
    return [
        _format_game(row, platforms_by_game.get(row["game_id"], []), response_format)
        for row in rows
    ]


def _format_game(row, platforms: list[dict], response_format: ResponseFormat) -> dict:
    game = {
        "game_id": row["game_id"],
        "appid": row["steam_appid"],
        "steam_appid": row["steam_appid"],
        "name": row["name"],
        "playtime_hours": round((row["total_playtime_minutes"] or 0) / 60, 1),
        "playtime_2weeks_hours": round((row["total_playtime_2weeks_minutes"] or 0) / 60, 1),
        "hltb_main": row["hltb_main"],
        "metacritic_score": row["metacritic_score"],
        "opencritic_score": row["opencritic_score"],
        "protondb_tier": row["protondb_tier"],
        "steam_review_desc": row["steam_review_desc"],
        "is_farmed": bool(row["is_farmed"]),
    }
    if response_format == "detailed":
        game["platforms"] = platforms
    return game


def _envelope(results: list[dict], total_matches: int, limit: int, offset: int) -> dict:
    return {
        "results": results,
        "total_matches": total_matches,
        "has_more": offset + len(results) < total_matches,
    }
