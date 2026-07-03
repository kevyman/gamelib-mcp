# IGDB Wishlist Backfill — Resolution Gaps (Handover)

**Date:** 2026-07-03
**Status:** Investigation only — no fixes proposed in this document.

## Background

PR #56 ("Preference-aware cross-platform wishlist deals") merged to `main` and
auto-deployed to production (Hetzner, `fac4f60`). It introduced schema v19
(`games.igdb_platforms`) and a migration step that re-claims IGDB enrichment
for every currently-wishlisted game, so background enrichment re-fetches IGDB
data and backfills per-game platform availability (needed to recommend
cross-platform wishlist deals, e.g. pricing a Steam-wishlisted game on
Switch 2).

Shortly after deploy, the migration and background enrichment ran as
expected: server logs showed live IGDB + DekuDeals search traffic, ending in
`Background enrichment complete: [0, 1, 0, 0, 0, 0, 187]`.

## Observed problem

Querying the production DB directly:

```
schema_version: 19
wishlist_total: 187
wishlist_igdb_platforms_populated: 178
wishlist_igdb_platforms_null: 9
wishlist_igdb_cached_at_null: 0
```

All 187 wishlisted games were attempted (`igdb_cached_at` is set for all of
them — no game is stuck un-attempted), but **9 games never got
`igdb_platforms` populated** — meaning IGDB enrichment ran for them but
either found no matching game or found a match with no platform data.

The 9 games (as of this investigation):

| Game | `igdb_id` | Note |
|---|---|---|
| A Hat in Time - Nyakuza Metro + Online Party | NULL | DLC/bundle-shaped title |
| NieR:Automata | 391942 | Has a matched `igdb_id`, but `igdb_platforms` is still NULL |
| Persona 3 Reload | NULL | Investigated — see below |
| S.T.A.L.K.E..: Call of Pripyat | NULL | Title contains a typo (`S.T.A.L.K.E..:` — double dot) |
| Sea of Stars: Sunset Edition | NULL | Edition-suffixed title |
| Sea of Thieves: 2026 Edition | NULL | Edition-suffixed title |
| The Seance of Blake Manor | NULL | Investigated — see below |
| Warhammer: Vermintide 2 - Shadows Over Bogenhafen | NULL | DLC-shaped title |
| 无主之地4 | NULL | Chinese-language duplicate of an existing "Borderlands 4" row (same Steam appid, same `wishlisted_at`) — resolved separately via `merge_games`, not part of this investigation's open questions |

Only **Persona 3 Reload** and **The Seance of Blake Manor** were investigated
in depth below. The other five (A Hat in Time DLC, S.T.A.L.K.E.. typo, both
"Edition"-suffixed titles, Warhammer DLC) were not individually root-caused —
they are plausibly related to the same families of failure described below
(DLC/bundle title shape, or exact-title search brittleness), but this is
speculation, not confirmed investigation. NieR:Automata is a distinct
anomaly: it has a matched `igdb_id`, so IGDB search succeeded, but the
platforms field still came back empty for that record — not yet explained.

## Investigation: Persona 3 Reload

`igdb.search_game("Persona 3 Reload", None)` returns only 5 results (the
query's hardcoded `limit 5`), and **none of the 5 is the actual base game** —
all 5 are DLC/cosmetic content packs:

```
266009 'Persona 3 Reload: Persona 5 Royal Persona Set 1'
301578 'Persona 3 Reload: Persona 4 Golden Persona Set'
266008 'Persona 3 Reload: Persona 5 Royal Persona Set 2'
301573 'Persona 3 Reload: Persona 5 Royal BGM Set'
289702 'Persona 3 Reload: Persona 5 Royal EX BGM Set'
```

Raising the query's `limit` to 20 (ad hoc, for investigation only) returned
20 results without truncation, including the real base game at position 11:

```
252647 'Persona 3 Reload'   category=None  game_type=8   parent_game=None (parent is 'Persona 3', id 9577)
```

Two things stand out from this raw data:

1. **`category` is `None`/unpopulated for every single result**, base game
   and DLC alike. The `category` field is not a usable signal for this
   title — IGDB simply hasn't populated it here.
2. **`game_type` does distinguish them correctly** — the base game is
   `game_type=8` (matches IGDB's documented "remake" type; Persona 3 Reload
   is a remake of the original Persona 3), the DLC/cosmetic packs are
   `game_type=13` ("pack"), "Episode Aigis" is `game_type=2` ("expansion"),
   "Expansion Pass" is `game_type=3` ("bundle").
3. `gamelib_mcp/data/igdb.py` already defines constants
   (`CATEGORY_MAIN_GAME=0`, `CATEGORY_DLC=1`, `CATEGORY_REMAKE=8`, etc.)
   whose numeric values line up with IGDB's `game_type` enum, not IGDB's
   `category` field (which came back unpopulated above). Whether this is a
   pre-existing mislabeling in the codebase, or whether `category` and
   `game_type` are simply inconsistently populated by IGDB depending on the
   game, was not resolved.

`_build_search_game_query` (in `gamelib_mcp/data/igdb.py`) does not filter on
`category` or `game_type` at all — its only optional filter is
`platforms = ...`; otherwise it relies solely on IGDB's own `search "name";`
relevance ranking, then a hardcoded `limit 5;`. `resolve_game` (which calls
`search_game` and then re-ranks the returned candidates using
`extract_best_fuzzy_key`, already-used fuzzy-matching infrastructure
elsewhere in this codebase) never gets a chance to consider the base game at
all in this case, because it isn't in the 5 candidates IGDB's own relevance
model returned.

## Investigation: The Seance of Blake Manor

The game's stored name in the `games` table was hex-dumped directly to rule
out a display/encoding artifact:

```
546865205365616E6365206F6620426C616B65204D616E6F72 = "The Seance of Blake Manor"
```

Confirmed: the stored title genuinely lacks a diacritic. IGDB's actual
canonical title for this game is `"The Séance of Blake Manor"` (with an
accented é), igdb_id 335833 — found by searching the substring `"Blake
Manor"`, which returned exactly one result.

Testing IGDB's `search` operator directly against several query variants:

| Query sent to IGDB | Result count |
|---|---|
| `"The Seance of Blake Manor"` (as stored, no accent) | 0 |
| `"Blake Manor"` (substring, no accent needed) | 1 (correct match) |
| `"Séance of Blake Manor"` (accented, no "The") | 0 |
| `"Seance"` alone | 5 (unrelated games named "Seance"/"Silly Seance"/etc.) |

The zero-result cases show this isn't simply "IGDB's search is
diacritic-sensitive" in a clean, isolable way — even the accented variant
without "The" also failed to match. The exact mechanics of IGDB's `search`
relevance/matching that make the full stored title fail were not fully
isolated in this investigation.

Because IGDB's `search` operator returned **zero** candidates for the stored
title, `resolve_game`'s post-fetch fuzzy re-ranking (`extract_best_fuzzy_key`)
never ran on this title at all — there was nothing to rank. The failure
happened entirely upstream of any fuzzy-matching step already present in the
codebase.

## Scope note

This document exists to hand off the investigation as-is. Remediation
approaches were discussed in the originating conversation but are
intentionally not recorded here, per this document's scope: problem and
investigation only.
