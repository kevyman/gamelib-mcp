# Enrichment, IGDB linking, series gaps

Why this exists: the IGDB mapping is authoritative but not infallible, and the
guard that keeps it from replacing a correct link (`_igdb_name_agrees`) only
makes sense next to the production incident that motivated it. The root
`CLAUDE.md` keeps the linking order and the guard as rules; the rest moved here
on 2026-09-01.

## Lazy enrichment

**Lazy enrichment**: `get_game_detail` fetches provider enrichment on demand and caches; bulk calls skip unenriched fields.

## IGDB linking order

**IGDB linking order**: `backfill_missing_games` resolves via `external_games` (store id → game, authoritative) first — the Steam appid, then the GOG product id for a row Steam did not map — then the stored igdb_id, then name resolution. The mapping self-corrects wrong-edition links, but it is not infallible — prod appid 212680 maps to a junk duplicate (178437 "Faster than light?") and once replaced FTL's correct link to 3075 — so it only overrides a STORED link whose IGDB name matches the library row when the mapping's own record matches too (`_igdb_name_agrees`). A manual `igdb_id` override outranks everything.

**Two store batches, one mapping.** `resolve_external_ids_to_igdb(category,
uids)` is the one external_games helper; `resolve_steam_appids_to_igdb` is a
thin wrapper on category 1 and `IGDB_EXTERNAL_CATEGORY_GOG = 5` is the second
store. GOG earns its own batch because it is the platform whose sync has no
per-item store id, so its rows arrive with decorated names and name resolution
serves them worst: 107 of the library's 131 GOG-identified rows were unlinked
while IGDB had mapped their product ids all along. The GOG batch asks only
about rows the Steam batch left unmapped (a Steam mapping already answers
authoritatively), and an operational failure of either batch aborts the pass
and leaves every claim retryable — degrading to name search during an outage
would mass-produce wrong links. Both batches filter on `external_games.category`,
which IGDB has deprecated in favour of `external_game_source` but still serves
and accepts; migrating is its own change, not a side effect of adding a store.
Epic/PSN/Xbox are deliberately absent: their uid formats are unverified here,
and an unverified format maps nothing at best and the wrong game at worst. The
Steam-only drift audit (`detectors.revalidate_igdb_matches`) is unchanged.

**Alternative names.** IGDB's `alternative_names` are requested by both query
builders (`alternative_names.name`, in the shared `_FETCH_BY_ID_FIELDS` and the
search field list), parsed onto `IGDBGame.alternative_names` (stripped,
case-insensitively deduplicated, never the primary name, capped at
`ALTERNATIVE_NAME_CAP = 12`) and read by the gate. They are the ONLY place an
abbreviation or a regional title is written down — no normalization rule can
turn "GTA V" into "Grand Theft Auto V", or "Yakuza 0" into "Ryu ga Gotoku 0" —
so without them those rows can never link. In `_select_best_match` a candidate
speaks with every one of its names: names that conflict with the query on
sequel/version identity are discarded ONE BY ONE, a candidate is dropped only
when every spelling conflicts, and the gate and tier 1 then ask whether ANY
surviving spelling matches. So an alternative name can widen recall but never
smuggle in a sequel the primary name would have refused ("Alan Wake" still
refuses a record named "Alan Wake 2" that lists "Alan Wake II"). Nor can it
outrank a record's own title: each tier splits on WHICH name matched — 1a
primary-exact, 1b alt-exact, 2a primary-edition-stripped, 2b
alt-edition-stripped, first non-empty band considered alone — because
alternative names carry working titles, acronyms and regional names that
legitimately coincide with another record's primary ("Titan" is Blizzard's
working title for "Overwatch" and also a 2019 game). Ranking them equally
turned a clean exact-title link into a same-name ambiguity refusal. The exact-name
equality queries stay on `name`: IGDB's support for nested-field filters is
unverified, and recall for an alternative-only spelling comes through `search`
(whose index covers alternative names) plus the gate. Accepted matches that
came through an alternative name say so in the log.

The same names are persisted as `game_aliases` rows (`alias_type
= "alternative_name"`, `source = "igdb"`, `source_key` the igdb id) by
`_apply_igdb_metadata`, for LIBRARY-side search and name matching only —
`tools/library.py` already joins `game_aliases`. They are never fed back into
the resolver, which reads them off the IGDB record directly. Skipped for a
row whose `igdb_id` is a manual override, and for a spelling that normalizes to
the row's own name — the same two rules the `provider_name` alias seed follows.
The write is ONE `upsert_game_aliases` call (one connection, one commit — this
runs inside the enrichment loop, and the per-alias `upsert_game_alias` shape
meant a dozen of each per game), and that call also PRUNES the row's other
igdb-sourced aliases: a row re-pointed at another record by the external
mapping or a drift reset must not keep the previous record's spellings. The
prune runs even when the new record has no alternative names, and touches
nothing from another source (hand-added edition aliases, other providers'
names).

**One match key.** Every "is this the same title?" decision goes through
`title_normalization.match_key`, and nothing else. It folds trademark glyphs,
diacritics and case; apostrophes (JOINED, not split — "Death's Door" is "deaths
door"); dotted acronyms ("F.E.A.R." is "fear", while "Mr. Driller" stays two
words); the ampersand/"and" spelling (Steam ships "Rabbit and Steel", IGDB holds
"Rabbit & Steel"); and, per whole token, Roman numerals ii–ix and the number
words zero–ten ("Hades II" / "Hades 2", "Left 4 Dead" / "Left Four Dead"). It
the "versus"/"vs" spelling as a whole token ("Marvel vs. Capcom" / "Marvel
versus Capcom"); and it
deliberately does NOT fold "x" or a lone "i": "Mega Man X" is not "Mega Man 10",
and a bare "I" is too ambiguous to read as a number. `normalize_series_gap_title`
(`_igdb_name_agrees`, series-gap and dedup comparisons),
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

**The gate's edition strip.** The strict name gate in `_select_best_match`
compares under `normalize_edition_comparison_title` — the BROAD strip
(qualifier-anchored tails, the generic "<up to 3 words> Edition" tail, a
trailing "(YYYY)"), not the narrow `normalize_series_gap_title` it used through
generation 2. That narrow list enumerates edition words, so a SKU name nobody
listed never met its base game: "Watch Dogs: Day One Edition", "DARK SOULS:
Prepare To Die Edition" and "Marvel's Midnight Suns Digital+ Edition" all sat
unlinked beside records IGDB holds. The broad strip is safe here only because
the year tiebreak below now guards every edition fold — a tier-2 match is
either a lone candidate inside the one-sided window or several inside ±2 — and
because it is anchored, not greedy: a subtitle that is not an edition phrase
survives ("Halo: The Master Chief Collection" is still not "Halo", "Star Wars:
The Force Unleashed" is still not "Star Wars", "Persona 5 Royal" is still not
"Persona 5"), and two differently-decorated SKUs still miss each other
("Sacred 2 Gold" collapses to "Sacred 2" while "Sacred 2: Fallen Angel" keeps
its subtitle — the honest answer for two different products). A title that IS
just an edition phrase cannot match everything: the normalizer returns the RAW
key when stripping empties the title, so ": Complete Edition" compares as
"complete edition", never as "". `normalize_series_gap_title` stays where it
was for `discover_series_gaps` and for `_igdb_name_agrees`, which already tries
BOTH strips in sequence and so needed no change.

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
Generation 2 was one `match_key` plus the year-aware tiebreak; generation 3 is
alternative names in the gate, GOG external ids, the "versus" fold and the
broader edition strip — four matching changes, one constant, no migration.

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
