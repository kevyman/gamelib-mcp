"""query_library: free-form read-only SQL, plus the schema it is written against.

query_library runs a single SELECT/WITH/EXPLAIN statement against a dedicated
read-only connection (data/db/readonly.py) — never the RW connection every
other tool uses. get_db_schema merges live sqlite_master/PRAGMA introspection
with the curated annotations in this module (TABLE_ANNOTATIONS/EXAMPLE_QUERIES/
GUIDANCE below), so the model always sees the *actual* current schema plus the
semantic traps that aren't visible from column names alone (switch2 playtime,
per-currency spend, is_primary_library_item, ...).

These two tools are an escape hatch, not the primary interface — CLAUDE.md's
dedicated tools (discover_games, get_spending_stats, get_backlog_stats,
get_play_history, ...) encode the same semantic traps *and* return
pre-shaped, cheaper responses. main.py's docstring for query_library says so
explicitly.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time

from ..data.db import get_db
from ..data.db.readonly import DEFAULT_QUERY_TIMEOUT_SECONDS, execute_readonly_query

logger = logging.getLogger(__name__)

MAX_ROW_LIMIT = 200
DEFAULT_ROW_LIMIT = 200
CELL_TRUNCATE_LENGTH = 300

# ── Curated schema annotations ───────────────────────────────────────────────
# Sourced from CLAUDE.md's Database section and docs/adr/0001-single-user.md,
# docs/adr/0002-dlc-first-class.md. Merged onto the live introspection in
# get_db_schema(); the annotation-sync test (tests/test_query_tool.py) is a
# strict two-way drift guard — every live table/view needs an entry here, and
# every entry needs to still exist in the live schema.
TABLE_ANNOTATIONS: dict[str, dict] = {
    "games": {
        "description": (
            "Canonical game rows + shared (cross-platform) enrichment. One row "
            "per distinct title OR nested content item (DLC/expansion/edition/…) "
            "— see content_type/is_primary_library_item below."
        ),
        "columns": {
            "is_primary_library_item": (
                "Derived from content_type, never set independently. A 'how many "
                "games do I own' query MUST filter is_primary_library_item = 1 "
                "(or use v_owned_games) — otherwise DLC/editions/soundtracks "
                "inflate the count."
            ),
            "content_type": (
                "base_game/standalone_expansion/remake/remaster/expanded_game/port "
                "are primary (is_primary_library_item=1); dlc/expansion/bundle/"
                "edition/unknown_addon are nested content hanging off parent_game_id. "
                "See docs/adr/0002-dlc-first-class.md."
            ),
            "parent_game_id": "For nested content, the base game it belongs to (games.id).",
            "completion_status": (
                "User-set only (via update_game) — no sync/enrichment writer ever "
                "touches it. NULL means unset (not necessarily unplayed — see "
                "v_owned_games.playtime_minutes / PLAY_STATE derivation in tools). "
                "Values: playing/completed/abandoned/evergreen."
            ),
            "tags": (
                "JSON array of strings (SteamSpy community tags UNIONed with IGDB "
                "themes/keywords), lowercase canonical vocabulary. Query with "
                "json_each: SELECT g.name, je.value FROM games g, json_each(g.tags) je."
            ),
            "manual_overrides": (
                "JSON array of column names a human pinned via update_game — sync/"
                "enrichment writers skip these columns. "
                "e.g. SELECT g.name FROM games g, json_each(g.manual_overrides) je "
                "WHERE je.value = 'completion_status'."
            ),
            "igdb_platforms": "JSON array of IGDB platform ids the title is available on (ownership-independent).",
            "is_farmed": "1 if playtime looks like an idle/AFK farming artifact, not real play.",
            "cover_image_id": "IGDB cover slug; NULL falls back to the Steam capsule by appid at read time (not stored).",
        },
    },
    "game_platforms": {
        "description": (
            "Ownership/playtime per (game, platform). A row here ALWAYS means a "
            "real platform relationship — it is never a wishlist-only entry (see "
            "game_wishlist, a separate table by design)."
        ),
        "columns": {
            "owned": (
                "1 = actually owned on this platform. A row can exist with "
                "owned=0 (a manual stub, or ownership that ENDED — see "
                "unowned_at) — filter owned=1 for real ownership."
            ),
            "unowned_at": (
                "Set when ownership ended: a refund, a revoked key, or a lapsed "
                "subscription title. The row survives (with owned=0) so its "
                "acquisition history does too, which is why every spend/count "
                "query must filter owned=1 rather than assume a row means "
                "ownership. Written only by add_game_to_platform(unowned_at=…)."
            ),
            "last_seen_in_source": (
                "Last sync in which the platform's OWN source returned this row "
                "— not last_synced, which any write touches. NULL = never seen "
                "in a source (hand-added, or predating the column). A row the "
                "source stopped returning is a refund/delisting CANDIDATE, "
                "never a conclusion: check_library's ownership.unseen_in_source "
                "reports it."
            ),
            "platform": (
                "Canonical values (see platforms_registry.py): steam, epic, gog, "
                "switch2, ps5, xbox, itchio, ea, ubisoft, other. NOTE: it's "
                "'switch2', never 'switch' or 'nintendo' (those are just public "
                "aliases some tools accept as input)."
            ),
            "playtime_minutes": (
                "NOT AUTHORITATIVE for platform='switch2' — it can lag the real "
                "per-day Parental Controls data. Use v_game_playtime (or "
                "v_owned_games.playtime_minutes) instead of this column directly "
                "whenever switch2 rows might be involved."
            ),
            "delisted": (
                "1 = ownership confirmed via the Steam account license list for an "
                "app the public owned-games API no longer returns (typically "
                "retired from the store). Set by audit_steam_licenses; cleared "
                "when the app reappears in a normal sync."
            ),
            "price_paid": "Per-currency — see price_currency. NEVER SUM price_paid across rows with different price_currency values.",
            "price_currency": "ISO-ish currency code for price_paid/acquired_at. Group by this before summing money.",
            "purchase_source": (
                "How the copy was acquired. Vocabulary includes storefront names "
                "plus 'key_reseller' (GAMIVO/Kinguin/G2A/GMG/…), 'free', 'gift', "
                "etc. — see the live enums block for values actually present."
            ),
            "bundle_name": "Set when acquired as part of a multi-game bundle (split_bundle_acquisition).",
            "acquired_at": "ISO date/timestamp the copy was acquired, where known.",
            "manual_overrides": (
                "JSON array of column names pinned by hand (playtime_minutes/"
                "last_played via set_playtime; delisted and owned via "
                "add_game_to_platform) — the sync write paths skip these. "
                "e.g. json_each(gp.manual_overrides)."
            ),
            "last_played": "Cross-platform per-platform ISO date (YYYY-MM-DD), best-effort.",
        },
    },
    "game_platform_identifiers": {
        "description": "Provider IDs per platform row (steam_appid, gog_product_id, xbox_title_id, nintendo_title_id, epic_artifact_id, …).",
        "columns": {
            "identifier_type": "e.g. 'steam_appid', 'nintendo_title_id' — the join key v_game_playtime uses to bridge to nintendo_play_summary.",
            "identifier_value": "The provider's own ID, stored as text.",
            "is_primary": "1 if this is the primary identifier of its type for the game_platform row (a row can carry more than one of the same type in edge cases).",
        },
    },
    "steam_platform_data": {
        "description": "Steam-specific enrichment keyed 1:1 on game_platforms.id (only populated for platform='steam' rows).",
        "columns": {
            "protondb_tier": "ProtonDB compatibility tier (e.g. platinum/gold/silver/bronze/borked), Linux/Steam Deck relevance only.",
            "steam_review_score": "Steam's review percentage (0-100), distinct from ratings.raw_score (personal ratings).",
        },
    },
    "game_platform_enrichment": {
        "description": "Cross-platform critic-score enrichment keyed 1:1 on game_platforms.id (Metacritic + OpenCritic).",
        "columns": {
            "metacritic_score": "0-100 critic Metascore (NOT the 0-10 user score).",
            "opencritic_score": "0-100 OpenCritic aggregate.",
        },
    },
    "ratings": {
        "description": (
            "Personal 1-10 scores. One row per (game, source): source-specific "
            "weights feed tag_affinity — backloggd 1.0, manual 1.0, steam_review 0.5 "
            "(see data/db/affinity.py SOURCE_WEIGHTS)."
        ),
        "columns": {
            "source": "'backloggd' | 'manual' | 'steam_review'.",
            "normalized_score": "The 1-10 score used for affinity/display; raw_score is the source's native scale before normalization.",
        },
    },
    "tag_affinity": {
        "description": (
            "Precomputed per-tag taste scores (recompute_tag_affinity), built from "
            "explicit ratings plus a low-weight playtime pseudo-rating. Signed and "
            "mean-centered: positive = liked above your own average, negative = "
            "avoided, near zero = neutral. Not a raw popularity count."
        ),
        "columns": {
            "affinity_score": "Signed, mean-centered, shrunk taste score: Sum(w*(score-mean))/(Sum(w)+k), where k is the prior weight estimated per recompute (meta key 'tag_affinity_scale'). Already accounts for how much evidence backs the tag, so do NOT damp it again by game_count. It has no fixed scale — compare tags to each other, or to the recorded strong_affinity cut, never to a constant.",
            "game_count": "How many rated/played games contributed to this tag's score. Low counts are already shrunk toward zero in affinity_score; the column is for display and filtering, not for re-weighting.",
        },
    },
    "meta": {
        "description": "Generic key-value store (sync timestamps, cached series lookups, per-platform sync-status metadata, etc.). Not game-scoped.",
        "columns": {},
    },
    "game_series": {
        "description": "IGDB collections/franchises, deduplicated by (kind, igdb_id).",
        "columns": {"kind": "'collection' or 'franchise' (IGDB's own distinction)."},
    },
    "game_series_membership": {
        "description": "Many-to-many join between games and game_series.",
        "columns": {},
    },
    "game_aliases": {
        "description": "Alternate names for a game (used for cross-platform/fuzzy name reconciliation), not shown in normal responses.",
        "columns": {},
    },
    "nintendo_play_summary": {
        "description": (
            "Per-(device, application, day) Switch/Switch 2 playtime from the "
            "Parental Controls API — the ONLY authoritative source for switch2 "
            "playtime. Forward-only; a synthetic device_id='manual-baseline' row "
            "(period_key='1970-01-01') can hold user-backfilled pre-tracking "
            "playtime (set_switch2_playtime_baseline) and IS included in a plain "
            "SUM (it represents real playtime)."
        ),
        "columns": {
            "application_id": "Nintendo title id, normalized uppercase at ingest — bridge to games via game_platform_identifiers.identifier_type='nintendo_title_id' (also normalized uppercase, so the join is plain equality).",
            "period_type": "'day' for daily rows (what v_game_playtime and get_play_history sum over).",
        },
    },
    "game_wishlist": {
        "description": (
            "\"Want to play\" tracking — deliberately SEPARATE from game_platforms. "
            "A game_platforms row never means wishlisted, and a game_wishlist row "
            "never means owned; a game can have neither, either, or (transiently, "
            "before cleanup) both."
        ),
        "columns": {
            "source": "'steam' | 'dekudeals' | 'manual'.",
            "platform": "Canonical platform name (see game_platforms.platform), the platform the item is wishlisted on.",
        },
    },
    "game_prices": {
        "description": "CURRENT price cache (ITAD), overwritten in place on refresh — NOT history. UNIQUE(game_id, platform, shop).",
        "columns": {
            "price": (
                "Current price in `currency`; NULL if unpriced (e.g. no ITAD key "
                "configured). A NULL-price switch2/dekudeals row is also the "
                "negative cache get_wishlist(with_prices=True) writes when a "
                "per-title DekuDeals search finds no card — fetched_at is when "
                "that miss was confirmed, and it is re-tried after 72h. Filter "
                "`price IS NOT NULL` for real prices."
            )
        },
    },
    "game_assessments": {
        "description": (
            "Recorded game-quality VERDICTS and their components (ADR 0006 "
            "decision 5) — what was decided about a game, not what is true of "
            "it. Append-only history with at most one row per (game_id, UTC "
            "day): a same-day re-record replaces that day's row. A row can "
            "point at a game owned nowhere and wishlisted nowhere (a 'skip' on "
            "a candidate), which is a legitimate shape, not an orphan. NEVER "
            "join this into taste/affinity or recommendation queries: verdicts "
            "are model output and feeding them back into ranking is a "
            "self-reinforcement loop the ADR forbids."
        ),
        "columns": {
            "verdict": (
                "buy_now / wishlist_for_sale / try_demo / skip / "
                "play_what_you_own."
            ),
            "craft_adjusted": (
                "The 0-1 sample-adjusted review score (see "
                "get_assessment_context); craft_positive_pct is the raw 0-100 "
                "percentage — different scales, don't mix them."
            ),
            "owned_at_assessment": (
                "Ownership AT THE TIME of the verdict (0/1). Compare it with "
                "current ownership to ask whether a verdict was followed."
            ),
            "wishlisted_at_assessment": "Wishlist state at the time of the verdict (0/1).",
            "anchors_cited": "JSON array of {name, game_id?} — the library games the verdict rested on.",
            "flags": "JSON array of short strings (the verdict's red flags).",
            "target_price": "The 'wishlist at €X' threshold, in price_currency.",
            "instead_game_id": (
                "For 'play_what_you_own': the games.id he was pointed at "
                "instead. ON DELETE SET NULL."
            ),
            "steam_appid": (
                "Identity evidence carried on the row itself — an unowned "
                "candidate has no game_platforms row to hang an identifier on."
            ),
            "skill": (
                "Methodology provenance: the skill that produced the verdict "
                "(e.g. 'game-quality'), as DECLARED by the recording client. "
                "Never stamped server-side — NULL means unknown, not "
                "'no skill', and every row predating this column is NULL."
            ),
            "skill_version": (
                "That skill's frontmatter version at execution time, declared "
                "by the client (case preserved). NULL = unknown."
            ),
            "model": (
                "The model identifier the assessing client's environment "
                "declared, verbatim and lowercased. Expect family-level values "
                "from router-based clients ('gpt-5') — the exact variant is "
                "not reliably visible to the model itself. NULL = unknown."
            ),
        },
    },
    "play_history": {
        "description": (
            "Cumulative per-(game, platform) playtime SNAPSHOTS (totals, never "
            "deltas), at most one row per UTC day, written after each platform "
            "sync. Excludes switch2 entirely — see nintendo_play_summary/"
            "v_game_playtime for that platform instead. To get a windowed delta, "
            "subtract the snapshot before the window start from the latest "
            "snapshot in/before the window end (get_play_history does this)."
        ),
        "columns": {"playtime_minutes": "A TOTAL as of snapshot_date, not an incremental amount."},
    },
    "scrape_config": {
        "description": "Versioned DB overrides for the healable scrapers (backloggd/steam_reviews/metacritic/dekudeals). At most one status='active' row per provider; an empty table means every provider runs on code defaults.",
        "columns": {},
    },
    "query_log": {
        "description": "Audit trail of every query_library() call (success or error) — written after each call via the normal RW connection, never the read-only query connection.",
        "columns": {"truncated": "1 if the result set was cut to the row_limit."},
    },
    "v_owned_games": {
        "description": (
            "One row per owned (game, platform) — semantic view, owned=1 only. "
            "playtime_minutes is already switch2-corrected (sourced from "
            "v_game_playtime), so prefer this over hand-joining games+game_platforms "
            "for \"what do I own\" questions. Still filter is_primary_library_item=1 "
            "if you want games only, not DLC/editions."
        ),
        "columns": {
            "playtime_minutes": "switch2-correct (see v_game_playtime) — safe to SUM/compare directly, unlike game_platforms.playtime_minutes.",
        },
    },
    "v_game_playtime": {
        "description": (
            "Per-(game_id, platform) unified playtime_minutes: switch2 rows are "
            "SUM(nintendo_play_summary.playtime_minutes) joined through the game's "
            "nintendo_title_id identifier (the same join get_play_history uses), "
            "except a set_playtime-pinned row keeps its pinned "
            "game_platforms.playtime_minutes and a row with no summary data falls "
            "back to the stored value instead of NULL; every other platform passes "
            "through game_platforms.playtime_minutes unchanged. This is the view to "
            "join against for any total-playtime question that might touch switch2."
        ),
        "columns": {},
    },
}

# Live low-cardinality columns worth enumerating verbatim so the model doesn't
# have to guess casing/spelling (e.g. 'switch2' vs 'switch'). Each is
# (table, column); capped at 50 distinct values in get_db_schema.
_ENUM_COLUMNS: tuple[tuple[str, str], ...] = (
    ("game_platforms", "platform"),
    ("games", "content_type"),
    ("games", "completion_status"),
    ("game_platforms", "purchase_source"),
    ("ratings", "source"),
    ("game_wishlist", "source"),
    ("game_wishlist", "platform"),
)
_ENUM_CAP = 50

EXAMPLE_QUERIES: tuple[dict[str, str], ...] = (
    {
        "question": "How many owned games do I have per platform (games only, not DLC)?",
        "sql": (
            "SELECT platform, COUNT(*) AS n\n"
            "FROM v_owned_games\n"
            "WHERE is_primary_library_item = 1\n"
            "GROUP BY platform\n"
            "ORDER BY n DESC"
        ),
    },
    {
        "question": "Total playtime per platform, switch2-correct.",
        "sql": (
            "SELECT platform, SUM(playtime_minutes) AS total_minutes\n"
            "FROM v_game_playtime\n"
            "GROUP BY platform\n"
            "ORDER BY total_minutes DESC"
        ),
    },
    {
        "question": "Spending by purchase_source, grouped by currency (never summed across currencies).",
        "sql": (
            "SELECT purchase_source, price_currency, SUM(price_paid) AS spent, COUNT(*) AS n\n"
            "FROM game_platforms\n"
            "WHERE owned = 1 AND price_paid IS NOT NULL\n"
            "GROUP BY purchase_source, price_currency\n"
            "ORDER BY price_currency, spent DESC"
        ),
    },
    {
        "question": "Which owned primary games are tagged 'soulslike'?",
        "sql": (
            "SELECT DISTINCT g.id, g.name\n"
            "FROM games g, json_each(g.tags) je\n"
            "WHERE je.value = 'soulslike'\n"
            "  AND g.is_primary_library_item = 1\n"
            "  AND EXISTS (SELECT 1 FROM game_platforms gp WHERE gp.game_id = g.id AND gp.owned = 1)\n"
            "ORDER BY g.name"
        ),
    },
    {
        "question": "Most-played owned games I've never rated.",
        "sql": (
            "SELECT g.name, SUM(vgp.playtime_minutes) AS minutes\n"
            "FROM games g\n"
            "JOIN game_platforms gp ON gp.game_id = g.id AND gp.owned = 1\n"
            "JOIN v_game_playtime vgp ON vgp.game_id = gp.game_id AND vgp.platform = gp.platform\n"
            "WHERE g.is_primary_library_item = 1\n"
            "  AND NOT EXISTS (SELECT 1 FROM ratings r WHERE r.game_id = g.id)\n"
            "GROUP BY g.id\n"
            "HAVING minutes IS NOT NULL\n"
            "ORDER BY minutes DESC\n"
            "LIMIT 20"
        ),
    },
)

GUIDANCE: tuple[str, ...] = (
    (
        "Filter games.is_primary_library_item = 1 (or use v_owned_games) for any "
        "'how many games' question — otherwise DLC/editions/soundtracks inflate the count."
    ),
    (
        "Never trust game_platforms.playtime_minutes for platform='switch2' — join "
        "v_game_playtime (or select from v_owned_games) instead; it sums the real "
        "per-day nintendo_play_summary data through the nintendo_title_id identifier."
    ),
    (
        "game_wishlist and game_platforms are separate tables by design — a "
        "game_platforms row never means wishlisted, and vice versa."
    ),
    (
        "Never SUM game_platforms.price_paid across rows with different "
        "price_currency — group by currency first."
    ),
    (
        "games.tags and games.manual_overrides (and game_platforms.manual_overrides) "
        "are JSON arrays — use json_each(column) to query into them."
    ),
    (
        "game_prices is a current-price CACHE (overwritten in place), not a price "
        "history table; play_history is the opposite — cumulative snapshots, never "
        "overwritten, at most one row per game/platform/day."
    ),
    (
        "Canonical platform values come from platforms_registry.py: steam, epic, "
        "gog, switch2, ps5, xbox, itchio, ea, ubisoft, other — note it's 'switch2', "
        "never 'switch'."
    ),
)

# ── query_library ─────────────────────────────────────────────────────────────

_TABLE_NOT_FOUND_RE = re.compile(r"no such (table|column)", re.IGNORECASE)
# Two distinct rejection paths both mean "this tool is read-only": the
# authorizer denial (a statement that starts with an allowed keyword but
# attempts a write/PRAGMA/ATTACH inside it) and the first-keyword belt raised
# by data/db/readonly.execute_readonly_query for statements that don't even
# start with SELECT/WITH/EXPLAIN/VALUES (DML/DDL/PRAGMA/ATTACH always fail
# here, since the authorizer never gets a chance to see them).
_AUTH_DENIED_RE = re.compile(
    r"not authorized|readonly database|read-only|statements are allowed", re.IGNORECASE
)
_TIMEOUT_RE = re.compile(r"interrupted", re.IGNORECASE)
_TOO_BIG_RE = re.compile(r"string or blob too big", re.IGNORECASE)


def _error_hint(message: str) -> str | None:
    if _TABLE_NOT_FOUND_RE.search(message):
        return "Call query_library() with no arguments first to see the exact table/column names and views available."
    if _AUTH_DENIED_RE.search(message):
        return "This tool is read-only; only a single SELECT/WITH/EXPLAIN/VALUES statement is allowed — no writes, PRAGMA, or ATTACH."
    if _TOO_BIG_RE.search(message):
        return (
            "The query tried to build a string/blob over the 1 MiB per-value cap. "
            "Select smaller expressions — cells are truncated to 300 chars in the response anyway."
        )
    if _TIMEOUT_RE.search(message):
        return (
            f"The query ran longer than the {DEFAULT_QUERY_TIMEOUT_SECONDS:.0f}s limit. "
            "Narrow it with a WHERE clause, add a LIMIT, or use an aggregate instead of scanning everything."
        )
    return None


def _truncate_cell(value: object) -> tuple[object, bool]:
    if isinstance(value, str) and len(value) > CELL_TRUNCATE_LENGTH:
        return value[:CELL_TRUNCATE_LENGTH] + "…", True
    return value, False


async def _log_query(
    sql: str,
    *,
    row_count: int | None,
    truncated: bool | None,
    elapsed_ms: int,
    error: str | None,
) -> None:
    try:
        async with get_db() as db:
            await db.execute(
                """INSERT INTO query_log (sql, row_count, truncated, elapsed_ms, error)
                   VALUES (?, ?, ?, ?, ?)""",
                (sql, row_count, int(bool(truncated)) if truncated is not None else None, elapsed_ms, error),
            )
            await db.commit()
    except Exception:
        # A logging failure must never fail (or mask the result of) the query
        # itself — same posture as record_play_history_snapshots.
        logger.warning("query_library: failed to write query_log row", exc_info=True)


async def query_library(sql: str, row_limit: int = DEFAULT_ROW_LIMIT) -> dict:
    """See main.py's query_library docstring for the MCP-facing contract."""
    started = time.monotonic()
    sql = sql or ""
    clamped_limit = max(1, min(row_limit, MAX_ROW_LIMIT))

    if not sql.strip():
        error = "sql must be a non-empty SELECT/WITH/EXPLAIN/VALUES statement"
        await _log_query(sql, row_count=None, truncated=None, elapsed_ms=0, error=error)
        return {"error": error, "sql": sql, "hint": "Call query_library() with no arguments first to see what's queryable."}

    try:
        columns, rows, truncated = await execute_readonly_query(sql, row_limit=clamped_limit)
    except sqlite3.Error as exc:
        elapsed_ms = round((time.monotonic() - started) * 1000)
        message = str(exc)
        result = {"error": message, "sql": sql}
        hint = _error_hint(message)
        if hint:
            result["hint"] = hint
        await _log_query(sql, row_count=None, truncated=None, elapsed_ms=elapsed_ms, error=message)
        return result

    truncated_cells: list[str] = []
    out_rows: list[list[object]] = []
    for row in rows:
        out_row = []
        for col_name, value in zip(columns, row):
            new_value, was_truncated = _truncate_cell(value)
            out_row.append(new_value)
            if was_truncated and col_name not in truncated_cells:
                truncated_cells.append(col_name)
        out_rows.append(out_row)

    elapsed_ms = round((time.monotonic() - started) * 1000)
    await _log_query(sql, row_count=len(out_rows), truncated=truncated, elapsed_ms=elapsed_ms, error=None)

    return {
        "columns": columns,
        "rows": out_rows,
        "row_count": len(out_rows),
        "truncated": truncated,
        "truncated_cells": truncated_cells,
        "elapsed_ms": elapsed_ms,
    }


# ── get_db_schema ────────────────────────────────────────────────────────────

# FTS5 creates the games_fts virtual table plus internal shadow tables
# (games_fts_data/_idx/_docsize/_config/_content) that show up in sqlite_master
# like any other table but carry no queryable columns worth documenting —
# excluded here alongside SQLite's own sqlite_% bookkeeping tables.
_INTROSPECTION_EXCLUDE_SQL = (
    "name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
    "AND name != 'games_fts' AND name NOT LIKE 'games\\_fts\\_%' ESCAPE '\\'"
)


async def get_db_schema() -> dict:
    """See main.py's query_library docstring for the MCP-facing contract."""
    async with get_db() as db:
        table_rows = await db.execute_fetchall(
            f"""SELECT name, type FROM sqlite_master
                WHERE type IN ('table', 'view') AND {_INTROSPECTION_EXCLUDE_SQL}
                ORDER BY name"""
        )

        tables: list[dict] = []
        for row in table_rows:
            name, kind = row["name"], row["type"]
            annotation = TABLE_ANNOTATIONS.get(name, {})
            column_notes = annotation.get("columns", {})

            column_rows = await db.execute_fetchall(f"PRAGMA table_info({name})")
            columns = [
                {
                    "name": c["name"],
                    "type": c["type"],
                    "notnull": bool(c["notnull"]),
                    "pk": bool(c["pk"]),
                    "default": c["dflt_value"],
                    "notes": column_notes.get(c["name"]),
                }
                for c in column_rows
            ]

            fk_rows = await db.execute_fetchall(f"PRAGMA foreign_key_list({name})")
            foreign_keys = [
                {"column": fk["from"], "references": f"{fk['table']}.{fk['to']}"} for fk in fk_rows
            ]

            tables.append(
                {
                    "name": name,
                    "type": kind,
                    "description": annotation.get("description"),
                    "columns": columns,
                    "foreign_keys": foreign_keys,
                }
            )

        enums: dict[str, list] = {}
        for table, column in _ENUM_COLUMNS:
            rows = await db.execute_fetchall(
                f"SELECT DISTINCT {column} AS v FROM {table} "
                f"WHERE {column} IS NOT NULL ORDER BY {column} LIMIT {_ENUM_CAP}"
            )
            enums[f"{table}.{column}"] = [r["v"] for r in rows]

    return {
        "tables": tables,
        "enums": enums,
        "example_queries": list(EXAMPLE_QUERIES),
        "guidance": list(GUIDANCE),
    }
