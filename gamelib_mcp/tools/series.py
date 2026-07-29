"""Series/franchise implementations: the "series" stats report and gap discovery.

get_series_breakdown ranks owned series by how many games are owned in each.
discover_series_gaps answers a different question — "which entries am I
missing in series I own and love?" — by combining the same game_series
tables with live IGDB series-member lookups (data/series_gaps.py).
"""

from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastmcp.exceptions import ToolError

from ..data.db import get_db
from .common import LIBRARY_PLATFORMS, clamp_limit, validate_platform

if TYPE_CHECKING:
    from ..data.igdb import SeriesMember

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

    Counts are owned-only: a game contributes only when it has an owned
    ``game_platforms`` row (on ``platform`` when set, anywhere otherwise).
    Wishlist-only games rows and owned=0 manual stubs — which carry series
    memberships via IGDB backfill — never count.
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
            "WHERE gp.game_id = g.id AND gp.platform = :platform AND gp.owned = 1)"
        )
        params["platform"] = resolved_platform
    else:
        # Owned-count semantics even without a platform filter: a wishlist-only
        # games row (games + game_wishlist, zero game_platforms rows) or an
        # owned=0 manual stub carries series memberships via IGDB backfill, and
        # without this guard it would inflate a series' owned-game counts.
        # (The platform branch's EXISTS above already implies owned-somewhere.)
        where_clauses.append(
            "EXISTS (SELECT 1 FROM game_platforms gp "
            "WHERE gp.game_id = g.id AND gp.owned = 1)"
        )
    if kind is not None:
        where_clauses.append("s.kind = :kind")
        params["kind"] = kind
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Per-game playtime (scoped to the platform when set, owned rows only)
    # summed over the series' member games. A correlated subquery avoids join
    # fan-out inflating the sum.
    platform_pt_clause = " AND gp.platform = :platform" if resolved_platform else ""
    playtime_subq = (
        "(SELECT COALESCE(SUM(gp.playtime_minutes), 0) FROM game_platforms gp "
        f"WHERE gp.game_id = g.id AND gp.owned = 1{platform_pt_clause})"
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
    if platform:
        platform_clause = (
            " AND EXISTS (SELECT 1 FROM game_platforms gp "
            "WHERE gp.game_id = g.id AND gp.platform = :platform AND gp.owned = 1)"
        )
        params["platform"] = platform
    else:
        # Mirror the ranking query's owned guard so included_games never lists
        # a wishlist-only or owned=0-stub member the counts just excluded.
        platform_clause = (
            " AND EXISTS (SELECT 1 FROM game_platforms gp "
            "WHERE gp.game_id = g.id AND gp.owned = 1)"
        )
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


def _release_year(iso_date: str | None) -> int | None:
    """Year component of an ISO YYYY-MM-DD date string, or None."""
    if not iso_date:
        return None
    try:
        return int(str(iso_date)[:4])
    except ValueError:
        return None


def _pick_name_suppression_target(
    row_year: int | None, candidates: "list[SeriesMember]"
) -> "SeriesMember":
    """The single member a name-matched library row suppresses.

    When several members of a series normalize to the same name (Doom 1993
    and DOOM 2016 both normalize to "doom"), one library row must not wipe
    them all out. Deterministic pick: the member whose release year is
    closest to the row's (library release dates are often the *store
    listing/port* date, so closeness — not equality — is the signal); ties
    and rows/members without a year fall back to the earliest-released
    member (undated members last), then lowest igdb_id.
    """

    def sort_key(member: "SeriesMember") -> tuple:
        member_year = _release_year(member.first_release_date)
        if row_year is not None and member_year is not None:
            return (0, abs(member_year - row_year), member.first_release_date or "", member.igdb_id)
        return (1, 0, member.first_release_date or "9999-99-99", member.igdb_id)

    return min(candidates, key=sort_key)


async def discover_series_gaps(
    kind: str | None = None,
    min_owned: int = 2,
    limit: int = 10,
    include_unreleased: bool = False,
    refresh_cache: bool = False,
) -> dict:
    """
    Unowned entries in series you own and rate highly.

    Ranks your series by taste (average personal rating of its games, then
    total playtime), takes the top `limit`, fetches each one's full member
    list from IGDB, and subtracts what you actually OWN (games with an owned
    game_platforms row). A wishlisted-but-unowned title is NOT subtracted —
    it still appears as a gap, annotated on_wishlist=true, so you can see
    "you already want this" instead of it silently disappearing. kind filters
    to collection|franchise; min_owned skips series where you own fewer games
    (ranking is owned-only — wishlist-only games never count toward it either);
    include_unreleased keeps unreleased/undated entries. Requires IGDB
    credentials (TWITCH_CLIENT_ID/SECRET).
    """
    from ..data.igdb import (
        IGDB_TO_PLATFORM,
        IGDBRequestFailure,
        igdb_credentials_configured,
    )
    from ..data.series_gaps import get_series_members_cached
    from ..data.title_normalization import normalize_series_gap_title

    if kind is not None and kind not in VALID_KINDS:
        raise ToolError(f"Unknown kind '{kind}'. Valid: {sorted(VALID_KINDS)}")

    if not igdb_credentials_configured():
        return {
            "results": [],
            "series_checked": 0,
            "errors": [],
            "status": "unconfigured",
            "error_summary": "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set",
        }

    min_owned = max(1, min_owned)
    limit = clamp_limit(limit)

    params: dict = {"min_owned": min_owned, "limit": limit}
    kind_clause = ""
    if kind is not None:
        kind_clause = "AND s.kind = :kind"
        params["kind"] = kind

    async with get_db() as db:
        # Only actually-owned games count toward a series' rank: wishlist sync
        # creates games rows with no owned game_platforms row (and IGDB backfill
        # adds their series memberships), so without the EXISTS guard a series
        # of wishlist-only entries could satisfy min_owned. Ratings are
        # aggregated per game before joining — a raw LEFT JOIN ratings would
        # fan out per rating source, inflating the summed playtime and skewing
        # avg_rating toward multi-source games.
        rows = await db.execute_fetchall(
            f"""
            SELECT s.id AS series_id,
                   s.igdb_id AS igdb_id,
                   s.kind AS kind,
                   s.name AS series_name,
                   COUNT(DISTINCT m.game_id) AS owned_count,
                   AVG(r.avg_rating) AS avg_rating,
                   COALESCE(SUM(
                       (SELECT COALESCE(SUM(gp.playtime_minutes), 0)
                        FROM game_platforms gp
                        WHERE gp.game_id = g.id AND gp.owned = 1)
                   ), 0) AS total_playtime_minutes
            FROM game_series s
            JOIN game_series_membership m ON m.series_id = s.id
            JOIN games g ON g.id = m.game_id
                 AND g.is_primary_library_item = 1
                 AND EXISTS (SELECT 1 FROM game_platforms gp
                             WHERE gp.game_id = g.id AND gp.owned = 1)
            LEFT JOIN (SELECT game_id, AVG(normalized_score) AS avg_rating
                       FROM ratings GROUP BY game_id) r ON r.game_id = g.id
            WHERE s.igdb_id IS NOT NULL {kind_clause}
            GROUP BY s.id
            HAVING owned_count >= :min_owned
            ORDER BY (avg_rating IS NULL) ASC, avg_rating DESC, total_playtime_minutes DESC
            LIMIT :limit
            """,
            params,
        )

        # "Have" (suppresses a member entirely) = owned on some platform
        # (gp.owned=1) ONLY. Wishlisted-but-unowned titles are deliberately NOT
        # "have" here — they must still surface as gaps (annotated on_wishlist
        # below), so a user sees "you already want this" rather than the entry
        # silently vanishing. Not the whole games table either: a games row can
        # exist owned=0/wishlist-less (an owned=0 manual stub, or an orphaned
        # row left behind by an unsynced wishlist removal), and suppressing
        # those titles would hide real gaps. Not filtered to igdb_id IS NOT
        # NULL either: an owned row can have no igdb_id at all (e.g. a
        # GOTY-edition title IGDB backfill hasn't resolved yet), in which case
        # only the normalized-name fallback below can recognize it as "have".
        have_rows = await db.execute_fetchall(
            """
            SELECT igdb_id, name, release_date FROM games
            WHERE EXISTS (SELECT 1 FROM game_platforms gp
                          WHERE gp.game_id = games.id AND gp.owned = 1)
            ORDER BY games.id
            """
        )

        # Wishlisted (any platform, owned or not — though an owned+wishlisted
        # game is already excluded above via have_rows) games, for the
        # on_wishlist annotation only. This set never suppresses a member.
        wishlist_rows = await db.execute_fetchall(
            """
            SELECT igdb_id, name FROM games
            WHERE EXISTS (SELECT 1 FROM game_wishlist w
                          WHERE w.game_id = games.id)
            """
        )

    have_igdb_ids = {row["igdb_id"] for row in have_rows if row["igdb_id"] is not None}
    # Library rows as (igdb_id, normalized name, release year), in stable
    # games.id order so name-based suppression below is deterministic.
    # release_date is NOT trusted as the original release year — for many
    # rows it's the store listing/port date (prod: "PAYDAY 2" carries
    # 2018-03-15 against the 2013 game) — so it's only ever used to *rank*
    # same-named member candidates, never to veto a match.
    library_rows: list[tuple[int | None, str | None, int | None]] = [
        (
            row["igdb_id"],
            normalize_series_gap_title(row["name"]) if row["name"] else None,
            _release_year(row["release_date"]),
        )
        for row in have_rows
    ]
    wishlist_igdb_ids = {row["igdb_id"] for row in wishlist_rows if row["igdb_id"] is not None}
    wishlist_norm_names = {
        normalize_series_gap_title(row["name"]) for row in wishlist_rows if row["name"]
    }
    today = datetime.now(UTC).date().isoformat()

    with_gaps: list[dict] = []
    without_gaps: list[dict] = []
    errors: list[dict] = []

    for row in rows:
        series_igdb_id = row["igdb_id"]
        series_kind = row["kind"]
        try:
            series_result = await get_series_members_cached(
                series_kind, series_igdb_id, refresh=refresh_cache
            )
        except IGDBRequestFailure as exc:
            errors.append({"series": row["series_name"], "error": str(exc)})
            continue

        members = series_result.members
        member_ids = {m.igdb_id for m in members}
        aliases = series_result.aliases

        # Layer A: a member is excluded when an OWNED igdb_id is the member
        # itself or an edition/re-release alias of it (e.g. "The Witcher:
        # Enhanced Edition" -> the canonical "The Witcher" member).
        excluded: set[int] = have_igdb_ids & member_ids
        excluded |= {
            aliases[hid]
            for hid in have_igdb_ids
            if hid in aliases and aliases[hid] in member_ids
        }

        # on_wishlist annotation (never suppresses): a member is flagged when a
        # wishlisted-but-unowned library game resolves to it via the same
        # identity layers used for owned suppression above — direct igdb_id,
        # edition/re-release alias, or normalized-name match. Unlike Layer B's
        # owned-row suppression, this is purely additive: several members (or
        # none) can be flagged and nothing here removes a gap from the list.
        wishlisted_member_ids: set[int] = wishlist_igdb_ids & member_ids
        wishlisted_member_ids |= {
            aliases[hid]
            for hid in wishlist_igdb_ids
            if hid in aliases and aliases[hid] in member_ids
        }
        for m in members:
            if normalize_series_gap_title(m.name) in wishlist_norm_names:
                wishlisted_member_ids.add(m.igdb_id)

        # Layer B: normalized-name fallback with row-consumption semantics. A
        # library row that matched a member by id/alias is CONSUMED — it
        # explained itself and must not additionally suppress a same-named
        # sibling (an owned DOOM-2016 row with a proper igdb_id must not hide
        # the Doom-1993 member) — but ONLY when the consumed member's
        # normalized name matches the row's own. When the id contradicts the
        # name (prod: a row named "Tales from the Borderlands" whose enriched
        # igdb_id is actually "New Tales from the Borderlands", or "PAYDAY 2"
        # enriched as "Payday 2 VR"), the enrichment is suspect and the row
        # keeps its one-member name-suppression right. Each such row
        # suppresses at most ONE member: the best same-named candidate per
        # _pick_name_suppression_target. Deterministic exact match on
        # edition-stripped normalized names only — no fuzzy scoring.
        members_by_norm: dict[str, list] = defaultdict(list)
        member_by_id: dict[int, SeriesMember] = {}
        for m in members:
            members_by_norm[normalize_series_gap_title(m.name)].append(m)
            member_by_id[m.igdb_id] = m

        for row_igdb_id, row_norm, row_year in library_rows:
            consumed_member = None
            if row_igdb_id is not None:
                consumed_member = member_by_id.get(row_igdb_id)
                if consumed_member is None:
                    alias_target = aliases.get(row_igdb_id)
                    if alias_target is not None:
                        consumed_member = member_by_id.get(alias_target)
            if consumed_member is not None and (
                not row_norm
                or normalize_series_gap_title(consumed_member.name) == row_norm
            ):
                continue  # id/alias agrees with the row's name: row is bound
            if not row_norm:
                continue
            candidates = [
                m for m in members_by_norm.get(row_norm, []) if m.igdb_id not in excluded
            ]
            if candidates:
                excluded.add(_pick_name_suppression_target(row_year, candidates).igdb_id)

        gaps = []
        for member in members:
            if member.igdb_id in excluded:
                continue
            if not include_unreleased and (
                member.first_release_date is None or member.first_release_date > today
            ):
                continue
            available_on = sorted(
                {IGDB_TO_PLATFORM[p] for p in member.platforms if p in IGDB_TO_PLATFORM}
            )
            gaps.append(
                {
                    "igdb_id": member.igdb_id,
                    "name": member.name,
                    "release_date": member.first_release_date,
                    "game_type": member.game_type,
                    "available_on": available_on,
                    "on_wishlist": member.igdb_id in wishlisted_member_ids,
                }
            )

        entry = {
            "series_id": row["series_id"],
            "series_name": row["series_name"],
            "kind": series_kind,
            "owned_count": row["owned_count"],
            "avg_rating": (
                round(row["avg_rating"], 2) if row["avg_rating"] is not None else None
            ),
            "total_playtime_hours": round((row["total_playtime_minutes"] or 0) / 60, 1),
            "gaps": gaps,
        }
        (with_gaps if gaps else without_gaps).append(entry)

    return {
        "results": with_gaps + without_gaps,
        "series_checked": len(rows),
        "errors": errors,
    }
