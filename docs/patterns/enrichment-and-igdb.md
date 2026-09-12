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

**One match key.** Every "is this the same title?" decision goes through
`title_normalization.match_key`, and nothing else. It folds trademark glyphs,
diacritics and case; apostrophes (JOINED, not split — "Death's Door" is "deaths
door"); dotted acronyms ("F.E.A.R." is "fear", while "Mr. Driller" stays two
words); the ampersand/"and" spelling (Steam ships "Rabbit and Steel", IGDB holds
"Rabbit & Steel"); and, per whole token, Roman numerals ii–ix and the number
words zero–ten ("Hades II" / "Hades 2", "Left 4 Dead" / "Left Four Dead"). It
deliberately does NOT fold "x" or a lone "i": "Mega Man X" is not "Mega Man 10",
and a bare "I" is too ambiguous to read as a number. `normalize_series_gap_title`
(the strict name gate, `_igdb_name_agrees`, series-gap and dedup comparisons),
`normalize_edition_comparison_title`, `normalize_same_product_sku_title`,
`is_edition_variant_of` and `data/db/fuzzy.py`'s sequel-identity tokens all end
here (identity tokens strip the widest edition list FIRST, or the number fold
would read "Watch Dogs: Day One Edition" as a sequel to "Watch Dogs"); before that, six normalizers folded six different subsets of this
variation, which is how #184's "&" bug and the Humble folding mismatch happened.
`normalize_search_text` is NOT the match key and is untouched: it backs
`games.name_normalized` and FTS, so changing it would need a stored-column
rebuild — which is also why anything that PREFILTERS in SQL against
`name_normalized` must speak that language (`strip_comparison_edition_suffixes`
exists for acquisition.py's edition-sibling probe, which otherwise silently
dropped every apostrophe/ampersand/numeral title).

**Year tiebreak.** No name key can separate two IGDB records that genuinely
share a title, so `_select_best_match` takes the library row's own release year
(`reference_release_date`, or a trailing "(YYYY)" read off the title BEFORE any
stripping — `normalize_catalog_title` drops it). Gate-passing candidates split
into tier 1 (`match_key` equality — the exact title) and tier 2 (equal only once
an edition suffix is stripped); tier 1 is considered alone whenever it is
non-empty, so a remake marketed as an edition can never outrank the real thing.
With a known reference year, candidates within ±1 win in ranked order (platform
and region releases drift by months); with none inside that window, a LONE tier-1
candidate is still accepted (a re-release carries a later store date and the
exact title vouches — an unknown IGDB year never blocks it) while SEVERAL tier-1
candidates refuse. A LONE tier-2 candidate gets a ONE-SIDED window: an edition
cannot predate its own game, so an older or same-age candidate is the original
and is accepted however far back it sits ("Deus Ex: Game of the Year Edition",
store date 2013, reaches "Deus Ex" from 2000, or one with no date at all), and
only a candidate more than two years NEWER than the row is refused — "Mafia"
2002 vs "Mafia: Definitive Edition" 2020. SEVERAL tier-2 candidates are
ambiguous again and keep the symmetric ±2 window. The asymmetry is deliberate
and does have a blind side (a row NAMED "Mafia: Definitive Edition" accepts a
lone 2002 "Mafia"); it is the right call far more often, and IGDB normally holds
the edition's own record, which wins as tier 1. With no reference year, two or
more distinct candidates whose known years disagree by more than a year are
refused rather than ranked ("Dead Space" 2008 and 2023 — IGDB's own ordering is
not evidence). Every refusal logs at info with the candidates' ids and years.
The exact-name equality path hands its ambiguity to the same selector once a
year exists, and keeps its flat refusal when none does.

**Resolver version.** `igdb.IGDB_RESOLVER_VERSION` is stamped onto
`games.igdb_resolver_version` beside every `igdb_cached_at`, and
`claim_game_ids_for_igdb` takes any row that is unlinked and stamped by an
OLDER generation (never-checked rows sort first, so a stale backlog drains
behind new rows instead of starving them). Bump the constant whenever a matching
rule changes and the stale no-matches re-queue themselves; linked rows are never
touched by a bump. This retires a pattern: a no-match stamp used to be permanent,
so every resolver improvement needed its own hand-written re-queue migration
guessing at the affected titles — v10, v28 and v41, of which v41 was the last.

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
