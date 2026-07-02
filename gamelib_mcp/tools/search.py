"""Shared name-match SQL builder + fuzzy fallback for search-style tools.

The per-module ``_GAME_ROLLUP_CTE`` definitions stay separate (see common.py);
this module only builds WHERE/rank fragments over a normalized-name column so
library/detail tools rank matches identically:

    rank 0  exact normalized match
    rank 1  normalized prefix ("sekiro shadows" -> "sekiro shadows die twice")
    rank 2  normalized whole-phrase substring
    rank 3  every query token appears somewhere (token-AND)

Token-AND is the broadest WHERE tier, so punctuation inside titles (colons,
apostrophes, ™) can never break a match. When even that yields nothing, the
fuzzy fallback reuses the provider-matching utility (rapidfuzz token-sort with
a stdlib fallback) to catch misspellings and plural drift.
"""

from dataclasses import dataclass

from ..data.db import extract_best_fuzzy_key
from ..data.db.fuzzy import load_fuzzy_candidates
from ..data.title_normalization import normalize_search_text
from .common import like_escape as _like_escape

# Search is more permissive than provider identity-matching (cutoff 85): a human
# typed the query and a wrong-but-close result is recoverable, a miss is not.
SEARCH_FUZZY_CUTOFF = 80

# Expression that tolerates rows whose name_normalized backfill hasn't landed
# yet (e.g. mid-sync); lower(name) still token-AND matches most queries.
NORMALIZED_NAME_SQL = "COALESCE(g.name_normalized, lower(g.name))"


@dataclass
class NameMatch:
    where_sql: str
    where_params: list
    rank_sql: str
    rank_params: list
    # Blank query -> match everything ("list the library"); punctuation-only
    # query (e.g. "%") -> match nothing. Neither should trigger fuzzy fallback.
    is_empty_query: bool = False
    is_noise_query: bool = False

    @property
    def fuzzy_eligible(self) -> bool:
        return not (self.is_empty_query or self.is_noise_query)


def _fts_match_expr(tokens: list[str], id_column: str) -> tuple[str, str]:
    """(sql_fragment, match_param) routing tokens through the trigram index."""
    quoted = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    return (
        f"{id_column} IN (SELECT rowid FROM games_fts WHERE games_fts MATCH ?)",
        quoted,
    )


def build_name_match(
    query: str,
    column: str = "name_normalized",
    *,
    use_fts: bool = False,
    id_column: str = "g.id",
) -> NameMatch:
    """Build WHERE + rank SQL fragments matching ``query`` against ``column``.

    ``column`` must hold normalize_search_text() output (or the COALESCE
    fallback above). With ``use_fts=True`` (callers gate on
    ``data.db.fts_ready()``), tokens of length >= 3 filter through the
    games_fts trigram index instead of LIKE — same match semantics, indexed.
    The trigram tokenizer cannot see sequences shorter than 3 chars, so short
    tokens keep their LIKE fragment. Rank tiers are unchanged.
    """
    phrase = normalize_search_text(query)
    tokens = phrase.split()
    if not tokens:
        if not query.strip():
            return NameMatch("1=1", [], "0", [], is_empty_query=True)
        return NameMatch("1=0", [], "0", [], is_noise_query=True)

    escaped_phrase = _like_escape(phrase)
    fts_tokens = [t for t in tokens if use_fts and len(t) >= 3]
    like_tokens = [t for t in tokens if not (use_fts and len(t) >= 3)]

    fragments = []
    where_params: list = []
    if fts_tokens:
        fts_sql, fts_param = _fts_match_expr(fts_tokens, id_column)
        fragments.append(fts_sql)
        where_params.append(fts_param)
    for token in like_tokens:
        fragments.append(rf"{column} LIKE ? ESCAPE '\'")
        where_params.append(f"%{_like_escape(token)}%")
    where_sql = "(" + " AND ".join(fragments) + ")"

    rank_sql = f"""CASE
        WHEN {column} = ? THEN 0
        WHEN {column} LIKE ? ESCAPE '\\' THEN 1
        WHEN {column} LIKE ? ESCAPE '\\' THEN 2
        ELSE 3
    END"""
    rank_params = [phrase, f"{escaped_phrase}%", f"%{escaped_phrase}%"]

    return NameMatch(where_sql, where_params, rank_sql, rank_params)


async def fuzzy_fallback_game_ids(query: str, cutoff: int = SEARCH_FUZZY_CUTOFF) -> list[int]:
    """Best-effort fuzzy match returning game ids (at most one today)."""
    if not normalize_search_text(query):
        return []
    candidates = await load_fuzzy_candidates()
    best_id = extract_best_fuzzy_key(query, candidates, cutoff=cutoff)
    return [] if best_id is None else [best_id]
