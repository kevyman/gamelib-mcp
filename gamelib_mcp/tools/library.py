"""search_games and get_library_stats tools."""

from typing import Literal

from fastmcp.exceptions import ToolError

from ..data.db import fts_ready, get_db, load_platforms_for_games
from ..data.tag_synonyms import canonical_tag
from ..data.title_normalization import normalize_search_text
from ..utils import _parse_json
from .common import (
    OWNED_SQL as _OWNED_SQL,
)
from .common import (
    PLAY_STATE_SQL as _PLAY_STATE_SQL,
)
from .common import (
    PLAYTIME_SUM_SQL as _PLAYTIME_SUM_SQL,
)
from .common import (
    SERIES_NAMES_SQL as _SERIES_NAMES_SQL,
)
from .common import (
    STEAM_APPID_SQL as _STEAM_APPID_SQL,
)
from .common import (
    WISHLISTED_SQL as _WISHLISTED_SQL,
)
from .common import (
    clamp_limit as _clamp_limit,
)
from .common import (
    resolve_platform as _resolve_platform,
)
from .search import build_name_match, fuzzy_fallback_game_ids

VALID_FILTERS = {
    "all", "unplayed", "played", "recent", "farmed", "unknown",
    "playing", "completed", "abandoned", "evergreen",
}

VALID_CONTENT = {"games", "addons", "all"}

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
           {_SERIES_NAMES_SQL} AS series,
           g.tags,
           g.genres,
           g.hltb_main,
           g.is_farmed,
           g.completion_status,
           g.content_type,
           g.parent_game_id,
           g.is_primary_library_item,
           {_OWNED_SQL} AS owned,
           {_WISHLISTED_SQL} AS wishlisted,
           {_PLAYTIME_SUM_SQL} AS total_playtime_minutes,
           {_PLAY_STATE_SQL} AS play_state,
           COALESCE(SUM(COALESCE(gp.playtime_2weeks_minutes, 0)), 0) AS total_playtime_2weeks_minutes,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.protondb_tier END) AS protondb_tier,
           MAX(CASE WHEN gp.platform = 'steam' THEN spd.steam_review_desc END) AS steam_review_desc,
           MAX(gpe.metacritic_score) AS metacritic_score,
           MAX(gpe.opencritic_score) AS opencritic_score
    FROM games g
    -- owned = 1: unowned rows (wishlist-only games have none; owned=0 manual
    -- stubs do exist) must not feed the aggregates — a stub's playtime isn't
    -- real playtime anywhere, so play_state/playtime/enrichment derive from
    -- owned rows only. Unowned games still appear in search results (LEFT
    -- JOIN keeps the games row; the owned/wishlisted flags tell them apart).
    LEFT JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1
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
    series: str | None = None,
    response_format: ResponseFormat = "concise",
) -> dict:
    """Find games in the library by name, optionally filtered by platform/series.

    Matching is punctuation-insensitive and token-based ("sekiro shadow" finds
    "Sekiro: Shadows Die Twice"); when nothing matches, a fuzzy fallback
    catches misspellings and tags those results with match_type="fuzzy".

    series: restrict to games in a series (IGDB collection/franchise) by exact,
    case-insensitive name. Pass an empty query to browse a whole series, e.g.
    search_games("", series="The Legend of Zelda").

    Results can include wishlist-only titles (a games row with a wishlist
    entry but no owned platform) — check owned/wishlisted on each result, not
    is_primary_library_item, which is a content-type flag (real game vs
    DLC/soundtrack/edition) and says nothing about ownership.
    """
    limit = _clamp_limit(limit)
    platform = _resolve_platform(platform)
    match = build_name_match(query, use_fts=fts_ready(), id_column="game_id")
    conditions = [match.where_sql]
    params: list = list(match.where_params)
    # An exact normalized match must never be hidden by the primary filter: a
    # nested row (real edition, or a misclassified base game) whose name IS the
    # query would otherwise be invisible whenever any primary row also matches,
    # because the nested-content fallback only fires on zero primary matches —
    # and an invisible exact match tempts create_missing callers into minting a
    # duplicate. Exact matches rank 0, so they surface first.
    if match.fuzzy_eligible:
        conditions.append("(is_primary_library_item = 1 OR name_normalized = ?)")
        params.append(normalize_search_text(query))
    else:
        conditions.append("is_primary_library_item = 1")
    if platform:
        conditions.append(
            "game_id IN (SELECT game_id FROM game_platforms WHERE platform = ? AND owned = 1)"
        )
        params.append(platform)
    if series:
        conditions.append(
            """EXISTS (
                SELECT 1 FROM json_each(COALESCE(series, '[]'))
                WHERE lower(value) = ?
            )"""
        )
        params.append(series.lower())
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

    if total["c"] == 0 and match.fuzzy_eligible and not series:
        alias_results = await _alias_search(query, platform, limit, offset, response_format)
        if alias_results is not None:
            return alias_results
        fuzzy_results = await _fuzzy_search(query, platform, limit, offset, response_format)
        if fuzzy_results is not None:
            return fuzzy_results
        nested_results = await _nested_content_fallback(
            query, platform, limit, offset, response_format
        )
        if nested_results is not None:
            return nested_results

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
    conditions = [f"game_id IN ({placeholders})", "is_primary_library_item = 1"]
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


async def _alias_search(
    query: str,
    platform: str | None,
    limit: int,
    offset: int,
    response_format: ResponseFormat,
) -> dict | None:
    """Retry against package/edition aliases. None = no alias match."""
    match = build_name_match(query, column="ga.alias_normalized")
    if not match.fuzzy_eligible:
        return None

    conditions = [match.where_sql, "gr.is_primary_library_item = 1"]
    params: list = list(match.where_params)
    if platform:
        conditions.append(
            "gr.game_id IN (SELECT game_id FROM game_platforms WHERE platform = ? AND owned = 1)"
        )
        params.append(platform)
    where = " AND ".join(conditions)

    async with get_db() as db:
        total = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT COUNT(DISTINCT gr.game_id) AS c
            FROM game_rollup gr
            JOIN game_aliases ga ON ga.game_id = gr.game_id
            WHERE {where}
            """,
            tuple(params),
        )
        if total["c"] == 0:
            return None

        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT gr.*, ga.alias AS matched_alias, 'alias' AS match_type,
                   {match.rank_sql} AS match_rank
            FROM game_rollup gr
            JOIN game_aliases ga ON ga.game_id = gr.game_id
            WHERE {where}
            GROUP BY gr.game_id
            ORDER BY match_rank ASC, gr.total_playtime_minutes DESC, gr.name ASC
            LIMIT ?
            OFFSET ?
            """,
            (*match.rank_params, *params, limit, offset),
        )

    return _envelope(
        await _format_rows(rows, response_format=response_format),
        total["c"],
        limit,
        offset,
    )


async def _nested_content_fallback(
    query: str,
    platform: str | None,
    limit: int,
    offset: int,
    response_format: ResponseFormat,
) -> dict | None:
    """Primary-item tiers + alias + fuzzy all found nothing — retry the tiered
    name match restricted to nested content (DLC/expansions/editions,
    is_primary_library_item=0). Primary rows were already covered by the first
    pass, so this only searches the rows that pass excluded. None = no match.
    """
    match = build_name_match(
        query, column="gr.name_normalized", use_fts=fts_ready(), id_column="gr.game_id"
    )
    if not match.fuzzy_eligible:
        return None

    conditions = [match.where_sql, "gr.is_primary_library_item = 0"]
    params: list = list(match.where_params)
    if platform:
        conditions.append(
            "gr.game_id IN (SELECT game_id FROM game_platforms WHERE platform = ? AND owned = 1)"
        )
        params.append(platform)
    where = " AND ".join(conditions)

    async with get_db() as db:
        total = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT COUNT(*) AS c
            FROM game_rollup gr
            WHERE {where}
            """,
            tuple(params),
        )
        if total["c"] == 0:
            return None

        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT gr.*, parent.name AS parent_name,
                   'nested_content' AS match_type,
                   {match.rank_sql} AS match_rank
            FROM game_rollup gr
            LEFT JOIN games parent ON parent.id = gr.parent_game_id
            WHERE {where}
            ORDER BY match_rank ASC, gr.total_playtime_minutes DESC, gr.name ASC
            LIMIT ?
            OFFSET ?
            """,
            (*match.rank_params, *params, limit, offset),
        )

    return _envelope(
        await _format_rows(rows, response_format=response_format),
        total["c"],
        limit,
        offset,
    )


async def search_games_batch(
    queries: list[str],
    limit_per_query: int = 5,
) -> dict[str, list[dict]]:
    """Look up multiple game names in one call. Returns dict keyed by query."""
    limit_per_query = _clamp_limit(limit_per_query)
    results = {}
    async with get_db() as db:
        for query in queries:
            match = build_name_match(query, use_fts=fts_ready(), id_column="game_id")
            # Same exact-match escape as search_games: a nested row whose name
            # IS the query must surface (rank 0) instead of hiding behind the
            # primary filter while broader primary matches exist.
            if match.fuzzy_eligible:
                primary_sql = "(is_primary_library_item = 1 OR name_normalized = ?)"
                primary_params: tuple = (normalize_search_text(query),)
            else:
                primary_sql = "is_primary_library_item = 1"
                primary_params = ()
            rows = await db.execute_fetchall(
                _GAME_ROLLUP_CTE
                + f"""
                SELECT *, {match.rank_sql} AS match_rank
                FROM game_rollup
                WHERE {match.where_sql} AND {primary_sql}
                ORDER BY match_rank ASC, total_playtime_minutes DESC, name ASC
                LIMIT ?
                """,
                (*match.rank_params, *match.where_params, *primary_params, limit_per_query),
            )
            results[query] = await _format_rows(rows)

    # Same fallback chain as single-query search_games (alias > fuzzy >
    # nested). The alias tier matters most here: batch mode backs ownership
    # screens over storefront/abbreviated titles ("TMNT: Shredder's Revenge"
    # vs the stored full name), and skipping it returned a wrong "not owned".
    for query, games in results.items():
        if games or not normalize_search_text(query):
            continue
        alias = await _alias_search(query, None, limit_per_query, 0, "detailed")
        if alias is not None:
            results[query] = alias["results"]
            continue
        fuzzy = await _fuzzy_search(query, None, limit_per_query, 0, "detailed")
        if fuzzy is not None:
            results[query] = fuzzy["results"]
            continue
        nested = await _nested_content_fallback(query, None, limit_per_query, 0, "detailed")
        if nested is not None:
            results[query] = nested["results"]
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
    series: list[str] | None = None,
    content: str = "games",
) -> dict:
    """
    Return filtered/sorted game list plus aggregate stats.

    filter: all | unplayed | played | recent | farmed | unknown | playing |
    completed | abandoned | evergreen (the last four read games.completion_status,
    set via update_game)
    sort_by: playtime | name | metacritic | opencritic | hltb
    platform: steam | epic | gog | ps5 | nintendo | switch2 (optional — filter to games owned on that platform)
    tags / genres / series: case-insensitive; a game must carry EVERY listed entry.
    series matches IGDB collections/franchises (e.g. "The Legend of Zelda").
    content: games (default — is_primary_library_item=1, today's behavior) |
    addons (only DLC/expansion/edition rows, is_primary_library_item=0) | all
    (both). Only affects the listed/aggregated rows themselves — the always-
    present addons block below is computed independently of this param.

    Note: min_metacritic, min_opencritic, and max_hltb_hours exclude games with
    no score / no HLTB data (NULL), so even min_metacritic=0 drops unscored games.

    This is the OWNED library view: results and aggregate counts are always
    scoped to games with an owned platform row, so a wishlist-only title never
    inflates total_games/backlog totals here (use search_games or get_wishlist
    to look up wishlist entries). is_primary_library_item is a content-type
    flag (real game vs DLC/soundtrack/edition), not an ownership signal.

    Regardless of content/filter, the response always carries an additive
    addons block — {count, spend: {currency: total_price_paid}, top_parents:
    [{game_id, name, addon_count}] up to 5} — summarizing owned nested content
    library-wide, the same way spending summarizes acquisition cost.
    """
    limit = _clamp_limit(limit)
    if filter not in VALID_FILTERS:
        raise ToolError(f"Unknown filter '{filter}'. Valid: {sorted(VALID_FILTERS)}")
    if sort_by not in SORT_COLUMNS:
        raise ToolError(f"Unknown sort_by '{sort_by}'. Valid: {sorted(SORT_COLUMNS)}")
    if content not in VALID_CONTENT:
        raise ToolError(f"Unknown content '{content}'. Valid: {sorted(VALID_CONTENT)}")
    if protondb_tier is not None:
        from ..data.protondb import TIER_ORDER

        if protondb_tier.lower() not in TIER_ORDER:
            raise ToolError(
                f"Unknown protondb_tier '{protondb_tier}'. Valid: {list(TIER_ORDER)}"
            )

    # get_library_stats is the OWNED library view (unlike search_games, which
    # also surfaces wishlist-only rows so they can be looked up by name) — a
    # wishlist sync creates a games row with no owned game_platforms row at
    # all, and such a row must not count toward library totals/backlog here.
    if content == "games":
        conditions = ["is_primary_library_item = 1", "owned = 1"]
    elif content == "addons":
        conditions = ["is_primary_library_item = 0", "owned = 1"]
    else:  # "all"
        conditions = ["owned = 1"]
    params: list = []

    if filter == "unplayed":
        conditions.append("play_state = 'unplayed'")
    elif filter == "played":
        conditions.append("play_state = 'played'")
    elif filter == "unknown":
        conditions.append("play_state = 'unknown'")
    elif filter == "recent":
        conditions.append("total_playtime_2weeks_minutes > 0")
    elif filter == "farmed":
        conditions.append("is_farmed = 1")
    elif filter == "playing":
        conditions.append("completion_status = 'playing'")
    elif filter == "completed":
        conditions.append("completion_status = 'completed'")
    elif filter == "abandoned":
        conditions.append("completion_status = 'abandoned'")
    elif filter == "evergreen":
        conditions.append("completion_status = 'evergreen'")

    if max_hltb_hours is not None:
        conditions.append("hltb_main <= ?")
        params.append(max_hltb_hours)

    if min_metacritic is not None:
        conditions.append("metacritic_score >= ?")
        params.append(min_metacritic)

    if min_opencritic is not None:
        conditions.append("opencritic_score >= ?")
        params.append(min_opencritic)

    for column, wanted in (("tags", tags), ("genres", genres), ("series", series)):
        for entry in wanted or []:
            conditions.append(
                f"""EXISTS (
                    SELECT 1 FROM json_each(COALESCE({column}, '[]'))
                    WHERE lower(value) = ?
                )"""
            )
            # Tags carry a canonical/synonym vocabulary; genres and series do not.
            params.append(canonical_tag(entry) if column == "tags" else entry.lower())

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
        # parent.name rides along so addon listings (content="addons"/"all")
        # can say which base game each nested row belongs to; primary rows have
        # no parent, so the default games view is unaffected. The page is
        # filtered/ordered in the subquery (bare column names — a direct join
        # would make them ambiguous against games'), then the parent joined on.
        rows = await db.execute_fetchall(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT filtered.*, parent.name AS parent_name
            FROM (
                SELECT *
                FROM game_rollup
                {where}
                ORDER BY {sort_col} {sort_dir} NULLS LAST, name ASC
                LIMIT ?
                OFFSET ?
            ) AS filtered
            LEFT JOIN games parent ON parent.id = filtered.parent_game_id
            ORDER BY filtered.{sort_col} {sort_dir} NULLS LAST, filtered.name ASC
            """,
            (*params, limit, offset),
        )
        summary = await db.execute_fetchone(
            _GAME_ROLLUP_CTE
            + f"""
            SELECT COUNT(*) AS total_games,
                   SUM(CASE WHEN play_state = 'played' THEN 1 ELSE 0 END) AS played,
                   SUM(CASE WHEN play_state = 'unplayed' THEN 1 ELSE 0 END) AS unplayed,
                   SUM(CASE WHEN play_state = 'unknown' THEN 1 ELSE 0 END) AS unknown,
                   SUM(CASE WHEN is_farmed = 1 THEN 1 ELSE 0 END) AS farmed_games,
                   SUM(total_playtime_minutes) AS total_minutes
            FROM game_rollup
            {where}
            """,
            tuple(params),
        )

        # Library-wide acquisition spend, same scoping as get_spending_stats
        # totals: every owned game_platforms row (DLC/editions included —
        # money spent is money spent), independent of the filter params above.
        # Monetary totals group per currency and are never summed across.
        spend_summary = await db.execute_fetchone(
            """SELECT COUNT(*) AS owned_rows,
                      SUM(CASE WHEN price_paid IS NOT NULL THEN 1 ELSE 0 END) AS priced_rows
               FROM game_platforms
               WHERE owned = 1"""
        )
        spend_totals = await db.execute_fetchall(
            """SELECT price_currency AS currency,
                      ROUND(SUM(price_paid), 2) AS total_spent,
                      COUNT(*) AS priced_rows
               FROM game_platforms
               WHERE owned = 1 AND price_paid IS NOT NULL
               GROUP BY price_currency
               ORDER BY total_spent DESC"""
        )

        # addons block: always present, independent of the `content` param
        # (same pattern as `spending` above) — a library-wide summary of owned
        # nested content (DLC/expansions/editions). "Owned" here follows the
        # OWNED_SQL notion in tools/common.py: a nested games row counts once
        # it has at least one owned game_platforms row.
        addons_summary = await db.execute_fetchone(
            f"""SELECT COUNT(*) AS count
                FROM games g
                WHERE g.is_primary_library_item = 0 AND {_OWNED_SQL}"""
        )
        addons_spend_rows = await db.execute_fetchall(
            """SELECT gp.price_currency AS currency,
                      ROUND(SUM(gp.price_paid), 2) AS total_spent
               FROM game_platforms gp
               JOIN games g ON g.id = gp.game_id
               WHERE gp.owned = 1 AND gp.price_paid IS NOT NULL
                     AND g.is_primary_library_item = 0
               GROUP BY gp.price_currency"""
        )
        addons_top_parents = await db.execute_fetchall(
            f"""SELECT parent.id AS game_id, parent.name AS name,
                       COUNT(*) AS addon_count
                FROM games g
                JOIN games parent ON parent.id = g.parent_game_id
                WHERE g.is_primary_library_item = 0 AND {_OWNED_SQL}
                GROUP BY parent.id, parent.name
                ORDER BY addon_count DESC, parent.name ASC
                LIMIT 5"""
        )

    spend_owned_rows = spend_summary["owned_rows"] or 0
    spend_priced_rows = spend_summary["priced_rows"] or 0
    addons_block = {
        "count": addons_summary["count"] or 0,
        "spend": {row["currency"]: row["total_spent"] for row in addons_spend_rows},
        "top_parents": [dict(r) for r in addons_top_parents],
    }
    return {
        "total_games": summary["total_games"],
        "played": summary["played"] or 0,
        "unplayed": summary["unplayed"] or 0,
        "unknown": summary["unknown"] or 0,
        "farmed_games": summary["farmed_games"] or 0,
        "total_playtime_hours": round((summary["total_minutes"] or 0) / 60, 1),
        "filter": filter,
        "sort_by": sort_by,
        "spending": {
            "totals": [dict(r) for r in spend_totals],
            "owned_rows": spend_owned_rows,
            "priced_rows": spend_priced_rows,
            "coverage_pct": (
                round(spend_priced_rows / spend_owned_rows * 100, 1)
                if spend_owned_rows
                else 0.0
            ),
        },
        "addons": addons_block,
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
    row_keys = set(row.keys())
    play_state = row["play_state"] if "play_state" in row_keys else None
    game = {
        "game_id": row["game_id"],
        "appid": row["steam_appid"],
        "steam_appid": row["steam_appid"],
        "name": row["name"],
        "series": _parse_json(row["series"]),
        "playtime_hours": (
            None
            if play_state == "unknown"
            else round((row["total_playtime_minutes"] or 0) / 60, 1)
        ),
        "playtime_2weeks_hours": round((row["total_playtime_2weeks_minutes"] or 0) / 60, 1),
        "hltb_main": row["hltb_main"],
        "metacritic_score": row["metacritic_score"],
        "opencritic_score": row["opencritic_score"],
        "protondb_tier": row["protondb_tier"],
        "steam_review_desc": row["steam_review_desc"],
        "is_farmed": bool(row["is_farmed"]),
        "completion_status": row["completion_status"],
        "content_type": row["content_type"],
        "parent_game_id": row["parent_game_id"],
        "is_primary_library_item": bool(row["is_primary_library_item"]),
        "play_state": play_state,
        "owned": bool(row["owned"]),
        "wishlisted": bool(row["wishlisted"]),
    }
    if "match_type" in row_keys and row["match_type"]:
        game["match_type"] = row["match_type"]
    if "matched_alias" in row_keys and row["matched_alias"]:
        game["matched_alias"] = row["matched_alias"]
    if "parent_name" in row_keys and row["parent_name"]:
        game["parent_name"] = row["parent_name"]
    if response_format == "detailed":
        game["platforms"] = platforms
    return game


def _envelope(results: list[dict], total_matches: int, limit: int, offset: int) -> dict:
    return {
        "results": results,
        "total_matches": total_matches,
        "has_more": offset + len(results) < total_matches,
    }
