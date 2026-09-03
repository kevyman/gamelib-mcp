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

## Series gap analysis

**Series gap analysis**: `discover_series_gaps` = owned series/taste + live IGDB member lookup, cached in `meta` KV (7-day TTL; stale cache served on fetch failure). Matching is `igdb_id`-only — deliberately no fuzzy-name fallback, so run IGDB backfill first to avoid false-positive gaps. Per-series failures land in `errors`, not the whole call.
