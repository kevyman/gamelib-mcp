# ADR 0002: DLC and nested content are first-class library citizens

Status: accepted (2026-07-10)

## Context
- Game libraries routinely include DLC, expansions, editions, soundtracks, and
  remasters — distinct content rows that relate to base games but have their own
  ownership, pricing, and identifiers.
- Earlier, nested content was invisible to most APIs (search/stats/discover/series
  would skip them), unsearchable except by fuzzy name, unrepaired, and untracked
  in spending; they were second-class shadows.
- A content_type column (base_game / dlc / expansion / edition / …)
  existed but was never enforced as the source of truth for visibility. The
  is_primary_library_item boolean flag existed independently, creating the risk of
  desynchronization and silent corruption.
- Spending always included DLC rows in totals, but users couldn't see the breakdown
  or reason about it (was this a $30 base game or a $3 DLC?). Purchases would mint
  DLC rows as base games if they had no title match.
- Update_game couldn't repair misclassified rows (attach a DLC to its base game).

## Decision
1. **is_primary_library_item is always derived, never independent**: `is_primary_library_item`
   SHALL always be computed from `content_type ∈ PRIMARY_CONTENT_TYPES` (base_game,
   standalone_expansion, remake, remaster, expanded_game, port). It is never set
   directly; every write goes through `derive_is_primary(content_type)`.

2. **Classification precedence chain with default-clobber guard**: Classification writes
   follow a precedence chain — manual override > Steam store type/fullgame > IGDB
   category/version_parent > title overrides > importer hint > default base_game.
   MUST: A default-signal write (bare base_game + primary + no-parent) SHALL NOT
   overwrite a stored non-default classification — later syncs must never flip a
   stored DLC back to a primary library item.

3. **Every classification writer is shared**: All sync/enrichment/importer code that
   derives a content_type MUST go through either `apply_content_classification`
   (Steam, purchase importers) or `_apply_igdb_metadata` (IGDB). These two functions
   carry cross-reference comments and SHALL stay in sync on guards.

4. **Money spent on DLC always counts**: Spending surfaces (get_spending_stats,
   get_library_stats' spending/addons blocks) MUST include owned DLC/expansions/editions
   in monetary totals and breakdowns (separate by_family rolls each base game together
   with its owned nested children for cost-per-hour analysis). Spending is spending.

5. **Nested content is searchable and repairable, never invisible**: When search_games /
   search_games_batch find nothing in primary games, they MUST fall back to a nested-content
   search (match_type="nested_content", parent_name in results). Detect_misclassified_dlc
   is a read-only detector that never mints parents, only surfaces candidates whose
   suggested_update can be passed to update_game (game_id + content_type and/or parent_game_id).
   The repair flow is: detect → human confirms → update_game applies.

6. **Catalog data never mints games, parents are never minted from titles**: Steam's
   DLC appid arrays and IGDB's children lists (related_content) live in `meta` KV
   (steam_dlc_catalog:{appid}, igdb_children:{igdb_id}) — they inform DLC-ownership
   detail views and queries but NEVER create games rows. A parent must already exist
   as a primary library item for a nested row to link to it; missing parents are
   reported (suggested_update null) and left to manual repair.

7. **Nested purchase items match by exact identity only**: When a purchase item carries
   a nested content_type (e.g., DLC from eShop), import_purchases and set_acquisitions_batch
   MUST match by store identifier, explicit game_id, or exact/normalized-exact name only —
   never prefix/substring/token/fuzzy. Rationale: spend corruption (a DLC title
   token-matching its base game would attach DLC spend to the base row). With
   create_missing, an unmatched nested purchase is minted as an owned nested row
   (is_primary=0) with its parent resolved via split_addon_title against EXISTING
   primary games only; when no parent resolves, the row is minted with a NULL parent
   and surfaced by detect_misclassified_dlc's needs_parent bucket for manual repair.

8. **No database schema change was required**: The v11 schema already carries
   content_type (NOT NULL, default 'base_game') and parent_game_id (nullable FK to
   games.id, ON DELETE SET NULL).
   Catalogs live in `meta` (KV table, json values). This is a deliberate design choice:
   the minimum new state is declarative content classification, not catalog rows.

9. **A parent must be primary, enforced in both directions** (amended 2026-07-14):
   Decision 6's rule — a nested row may only hang off a primary library item —
   was enforced only on the child's side (update_game rejects a nested parent).
   The inverse was unguarded: any writer could demote a row that other rows
   already nest under, which hides it from the is_primary rollups AND strands its
   children behind it, so both rows fall out of the library. Observed in
   production: the Fallout: New Vegas base row carried content_type='edition'
   while its DLC and a separate "Ultimate Edition" row named it as parent.
   MUST: no classification writer nests a row for which `has_nested_children` is
   true. apply_content_classification and _apply_igdb_metadata skip the
   classification write (their metadata writes still land); update_game, as the
   highest-precedence writer, raises instead — directing the user to re-parent
   (update_game) or fold in (merge_games) the children first.
   detect_misclassified_dlc's `nested_parent` bucket surfaces rows already in
   this shape and suggests promoting them back to base_game.

## Consequences
### Positive
- Content relationships are explicit and auditable; is_primary_library_item is always
  correct and needs no separate guard.
- Search and stats APIs can safely navigate nested content — library browsing is complete.
- Spending breakdowns clarify whether a user spent $30 on base games or $100 on DLC.
- One repair loop (detect_misclassified_dlc + update_game) handles all
  misclassification shapes: missing parent links, wrong content_type, orphaned nested rows.
- No schema migration required — the feature runs on v11 as-is.

### Negative / revisit triggers
- **If a store adds first-class playtime on DLC**: Currently discover_games and backlog
  exclude nested content (no reliable playtime signal per DLC). If Steam, Epic, or GOG
  expose per-DLC playtime, the exclusion could be revisited (e.g., "play my DLC" searches).
  Today, playtime is attached to the owned platform row (base or nested), so the signal is
  present but not used in affinity scoring.
- **If nested chains are ever needed** (a DLC of an edition, etc.): The current model
  enforces parent_game_id → a primary library item only. Allowing chains (edition → dlc → base)
  would require relaxing the "parent must be primary" rule and re-examining identity logic
  (which row's playtime rolls up). Not a current constraint.
- **If a sale/gift marketplace needs "addon intent"**: Bundle splitting today assumes
  per-game prices for constituents. If future integrations expose "this purchase explicitly
  includes DLC X," we'd want to diff that against is_primary to decide: is this a base-game
  bundle or a pack of addons? Today we guess based on name/resolution; explicit intent would
  improve the split.
