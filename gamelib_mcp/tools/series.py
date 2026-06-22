"""get_series_breakdown tool: rank game series/franchises by owned-game count."""

from collections import defaultdict

from fastmcp.exceptions import ToolError

from ..data.db import get_db
from .common import LIBRARY_PLATFORMS, clamp_limit, validate_platform

VALID_COUNTING_MODES = {"entries", "distinct_games", "base_games_only"}
VALID_KINDS = {"collection", "franchise"}

# Each counting mode maps to one aggregate column; that column is also surfaced
# as the row's `count` and drives HAVING/ORDER BY. The three counts narrow from
# every membership row -> primary library items -> base games only.
_COUNT_COLUMNS = {
    "entries": "count_entries",
    "distinct_games": "count_distinct_games",
    "base_games_only": "count_base_games_only",
}

_COUNT_EXPRESSIONS = {
    "count_entries": "COUNT(DISTINCT m.game_id)",
    "count_distinct_games": (
        "COUNT(DISTINCT CASE WHEN g.is_primary_library_item = 1 THEN m.game_id END)"
    ),
    "count_base_games_only": (
        "COUNT(DISTINCT CASE WHEN g.content_type = 'base_game' THEN m.game_id END)"
    ),
}


async def get_series_breakdown(
    counting_mode: str = "distinct_games",
    kind: str | None = None,
    min_games: int = 1,
    platform: str | None = None,
    include_games: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> dict:
    """Rank IGDB collections/franchises in the library by owned-game count.

    Each result is one series (a ``game_series`` row), labeled with its ``kind``.
    A game may count toward both its collection and its broader franchise, so
    the same game can appear in two rows; use ``kind`` and ``min_games`` to prune.
    """
    if counting_mode not in VALID_COUNTING_MODES:
        raise ToolError(
            f"Unknown counting_mode '{counting_mode}'. Valid: {sorted(VALID_COUNTING_MODES)}"
        )
    if kind is not None and kind not in VALID_KINDS:
        raise ToolError(f"Unknown kind '{kind}'. Valid: {sorted(VALID_KINDS)}")
    resolved_platform = validate_platform(platform, LIBRARY_PLATFORMS) if platform else None
    limit = clamp_limit(limit)
    offset = max(0, offset)
    min_games = max(1, min_games)

    count_col = _COUNT_COLUMNS[counting_mode]
    count_expr = _COUNT_EXPRESSIONS[count_col]

    params: dict = {"min_games": min_games}
    where_clauses: list[str] = []
    if resolved_platform:
        where_clauses.append(
            "EXISTS (SELECT 1 FROM game_platforms gp "
            "WHERE gp.game_id = g.id AND gp.platform = :platform)"
        )
        params["platform"] = resolved_platform
    if kind is not None:
        where_clauses.append("s.kind = :kind")
        params["kind"] = kind
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Per-game playtime (scoped to the platform when set) summed over the series'
    # member games. A correlated subquery avoids join fan-out inflating the sum.
    platform_pt_clause = " AND gp.platform = :platform" if resolved_platform else ""
    playtime_subq = (
        "(SELECT COALESCE(SUM(gp.playtime_minutes), 0) FROM game_platforms gp "
        f"WHERE gp.game_id = g.id{platform_pt_clause})"
    )

    base_sql = f"""
        FROM game_series s
        JOIN game_series_membership m ON m.series_id = s.id
        JOIN games g ON g.id = m.game_id
        {where_sql}
        GROUP BY s.id
        HAVING {count_expr} >= :min_games
    """

    async with get_db() as db:
        total_row = await db.execute_fetchone(
            f"SELECT COUNT(*) AS n FROM (SELECT s.id {base_sql})", params
        )
        total_matches = total_row["n"] if total_row else 0

        rows = await db.execute_fetchall(
            f"""
            SELECT s.id AS series_id,
                   s.name AS series_name,
                   s.kind AS kind,
                   {_COUNT_EXPRESSIONS['count_entries']} AS count_entries,
                   {_COUNT_EXPRESSIONS['count_distinct_games']} AS count_distinct_games,
                   {_COUNT_EXPRESSIONS['count_base_games_only']} AS count_base_games_only,
                   COALESCE(SUM({playtime_subq}), 0) AS total_playtime_minutes
            {base_sql}
            ORDER BY {count_col} DESC, s.name ASC
            LIMIT :limit OFFSET :offset
            """,
            dict(params, limit=limit, offset=offset),
        )

        members_by_series: dict = {}
        if include_games and rows:
            members_by_series = await _load_members(
                db, [row["series_id"] for row in rows], resolved_platform
            )

    results = []
    for row in rows:
        entry = {
            "series_id": row["series_id"],
            "series_name": row["series_name"],
            "kind": row["kind"],
            "count": row[count_col],
            "count_entries": row["count_entries"],
            "count_distinct_games": row["count_distinct_games"],
            "count_base_games_only": row["count_base_games_only"],
            "total_playtime_hours": round((row["total_playtime_minutes"] or 0) / 60, 1),
        }
        if include_games:
            included, collapsed = members_by_series.get(row["series_id"], ([], []))
            entry["included_games"] = included
            entry["collapsed_entries"] = collapsed
        results.append(entry)

    return {
        "results": results,
        "counting_mode": counting_mode,
        "total_matches": total_matches,
        "has_more": offset + len(results) < total_matches,
    }


async def _load_members(
    db, series_ids: list[int], platform: str | None
) -> dict[int, tuple[list[str], list[dict]]]:
    """For the page's series, split member games into primary vs collapsed entries."""
    placeholders = ",".join(f":sid{i}" for i in range(len(series_ids)))
    params: dict = {f"sid{i}": sid for i, sid in enumerate(series_ids)}
    platform_clause = ""
    if platform:
        platform_clause = (
            " AND EXISTS (SELECT 1 FROM game_platforms gp "
            "WHERE gp.game_id = g.id AND gp.platform = :platform)"
        )
        params["platform"] = platform
    rows = await db.execute_fetchall(
        f"""SELECT m.series_id AS series_id, g.name AS name,
                   g.content_type AS content_type,
                   g.is_primary_library_item AS is_primary_library_item
            FROM game_series_membership m
            JOIN games g ON g.id = m.game_id
            WHERE m.series_id IN ({placeholders}){platform_clause}
            ORDER BY g.is_primary_library_item DESC, g.name ASC""",
        params,
    )
    included: dict[int, list[str]] = defaultdict(list)
    collapsed: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        if row["is_primary_library_item"]:
            included[row["series_id"]].append(row["name"])
        else:
            collapsed[row["series_id"]].append(
                {"name": row["name"], "reason": row["content_type"]}
            )
    return {sid: (included.get(sid, []), collapsed.get(sid, [])) for sid in series_ids}
