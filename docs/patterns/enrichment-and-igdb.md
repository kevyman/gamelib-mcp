# Enrichment, IGDB linking, series gaps

Why this exists: the IGDB mapping is authoritative but not infallible, and the
guard that keeps it from replacing a correct link (`_igdb_name_agrees`) only
makes sense next to the production incident that motivated it. The root
`CLAUDE.md` keeps the linking order and the guard as rules; the rest moved here
on 2026-09-01.

## Lazy enrichment

**Lazy enrichment**: `get_game_detail` fetches provider enrichment on demand and caches; bulk calls skip unenriched fields.

## IGDB linking order

**IGDB linking order**: `backfill_missing_games` resolves via `external_games` (Steam appid → game, authoritative) first, then the stored igdb_id, then name resolution. The mapping self-corrects wrong-edition links, but it is not infallible — prod appid 212680 maps to a junk duplicate (178437 "Faster than light?") and once replaced FTL's correct link to 3075 — so it only overrides a STORED link whose IGDB name matches the library row when the mapping's own record matches too (`_igdb_name_agrees`). A manual `igdb_id` override outranks everything.

**Ampersand spellings are one title.** Steam ships "Rabbit and Steel" while IGDB holds "Rabbit & Steel", and `normalize_search_text` drops the `&` entirely — so `normalize_series_gap_title` (the strict name gate, `_igdb_name_agrees`, and the series-gap/dedup comparisons built on it) now folds a standalone `&` into the word "and" before normalizing, `ampersand_alternate` leads the resolve ladder as an identity-preserving rung, and both exact-name equality paths (`_resolve_game_with_status` and `media.py::_resolve_igdb_id_by_name`) retry the other spelling once when the stored one matched nothing — never when it was ambiguous. `normalize_search_text` itself is untouched: it backs `games.name_normalized`, so changing it would need a stored-column rebuild. No-match stamps the old resolver wrote for these titles are permanent on their own (the background claim only takes `igdb_cached_at IS NULL`), so migration v41 NULLs them once for rows matching `%&%` or a whole-word "and" — the same one-time re-claim v10 used, not a retry on any hot path.

The backfill gets the Steam appid from platform identifiers first, then the
Steam wishlist's `store_identifier`. Wishlist-only games have no platform row,
so omitting that fallback sends an exact store identity through name matching
and can attach metadata for another game with the same title. Only a wishlist
row with `platform = 'steam'` supplies this fallback, regardless of its source
(synced, manual, or assessment); other stores' identifiers are not Steam appids.
Third and last comes the newest `game_assessments.steam_appid` — the same
ownership-free shape one table over, since `record_assessment` mints a row for
an unowned candidate and puts the appid there for exactly the same reason.
This does not create an ownership row or requeue already-cached enrichment.

## Series gap analysis

**Series gap analysis**: `discover_series_gaps` = owned series/taste + live IGDB member lookup, cached in `meta` KV (7-day TTL; stale cache served on fetch failure). Matching is `igdb_id`-only — deliberately no fuzzy-name fallback, so run IGDB backfill first to avoid false-positive gaps. Per-series failures land in `errors`, not the whole call.
