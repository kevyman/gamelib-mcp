"""Pydantic response models for MCP output schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel


class FlexibleModel(BaseModel):
    """Allow provider-specific keys while still exposing useful schema anchors."""

    model_config = ConfigDict(extra="allow")


class PlatformEntry(FlexibleModel):
    platform: str | None = None
    owned: bool | None = None
    playtime_minutes: int | None = None
    playtime_2weeks_minutes: int | None = None
    last_played_date: str | None = None


class GameSummary(FlexibleModel):
    game_id: int
    appid: int | None = None
    steam_appid: int | None = None
    name: str
    # IGDB cover art (Steam capsule fallback); rendered by the game-cards app.
    cover_url: str | None = None
    platforms: list[PlatformEntry] | None = None
    playtime_hours: float | None = None
    playtime_2weeks_hours: float | None = None
    hltb_main: float | None = None
    metacritic_score: int | None = None
    opencritic_score: int | None = None
    protondb_tier: str | None = None
    steam_review_desc: str | None = None
    is_farmed: bool | None = None
    completion_status: str | None = None
    content_type: str | None = None
    parent_game_id: int | None = None
    # Content-type flag (real game vs DLC/soundtrack/edition) — NOT ownership.
    # Use owned/wishlisted below to know whether a game is actually owned.
    is_primary_library_item: bool | None = None
    owned: bool | None = None
    wishlisted: bool | None = None
    play_state: str | None = None
    matched_alias: str | None = None
    # Only populated for nested-content search hits (match_type="nested_content")
    # and addon listings — the name of the parent game this DLC/expansion/
    # edition belongs to.
    parent_name: str | None = None
    tags: list[str] | None = None
    series: list[Any] | None = None
    suggested_platform: str | None = None
    match_score: float | None = None
    # match_score normalized against the library-wide best match (0-100).
    match_percent: int | None = None
    match_type: str | None = None


class PaginatedGamesResponse(FlexibleModel):
    results: list[GameSummary]
    total_matches: int
    has_more: bool
    # Present when results are rank-ordered (discover_games): the pagination
    # offset, used by the game-cards app to number cards globally.
    offset: int | None = None


class SearchGamesBatchResponse(RootModel[dict[str, list[GameSummary]]]):
    pass


class SeriesBreakdownEntry(FlexibleModel):
    series_id: int
    series_name: str
    kind: str
    count: int
    count_entries: int
    count_distinct_games: int
    count_base_games_only: int
    total_playtime_hours: float | None = None
    included_games: list[str] | None = None
    collapsed_entries: list[Any] | None = None


class SeriesBreakdownResponse(FlexibleModel):
    results: list[SeriesBreakdownEntry]
    counting_mode: str
    total_matches: int
    has_more: bool


class SeriesGapMember(FlexibleModel):
    igdb_id: int
    name: str
    release_date: str | None = None
    game_type: int
    available_on: list[str] = Field(default_factory=list)
    # True when a wishlisted (but unowned) library game resolves to this
    # member by igdb_id, edition alias, or normalized name.
    on_wishlist: bool = False


class SeriesGapEntry(FlexibleModel):
    series_id: int
    series_name: str
    kind: str
    owned_count: int
    avg_rating: float | None = None
    total_playtime_hours: float
    gaps: list[SeriesGapMember] = Field(default_factory=list)


class SeriesGapsError(FlexibleModel):
    series: str
    error: str


class SeriesGapsResponse(FlexibleModel):
    results: list[SeriesGapEntry]
    series_checked: int
    errors: list[SeriesGapsError] = Field(default_factory=list)
    status: str | None = None
    error_summary: str | None = None


class LibraryStatsResponse(PaginatedGamesResponse):
    total_games: int
    played: int
    unplayed: int
    unknown: int
    farmed_games: int
    total_playtime_hours: float
    filter: str
    sort_by: str
    # Library-wide spending summary over owned rows (per-currency totals,
    # never cross-currency) — independent of the current filter parameters.
    spending: dict[str, Any]
    # Always-present, additive summary of owned nested content (DLC/expansions/
    # editions), independent of the `content` param: {count, spend:
    # {currency: total_price_paid}, top_parents: [{game_id, name, addon_count}]}.
    addons: dict[str, Any] | None = None


class GameRating(FlexibleModel):
    game_id: int
    appid: int | None = None
    steam_appid: int | None = None
    name: str
    platforms: list[PlatformEntry] | None = None
    source: str
    raw_score: float | None = None
    normalized_score: float | None = None
    review_text: str | None = None
    synced_at: str | None = None


class RatingsResponse(FlexibleModel):
    results: list[GameRating]
    total_matches: int
    has_more: bool


class GameDetailResponse(GameSummary):
    release_date: str | None = None
    genres: list[str] | None = None
    features: list[str] | None = None
    short_description: str | None = None
    steam_review_score: int | None = None
    metacritic_url: str | None = None
    opencritic_tier: str | None = None
    opencritic_percent_rec: float | None = None
    opencritic_url: str | None = None
    hltb_extra: float | None = None
    hltb_complete: float | None = None
    protondb_tier: str | None = None
    my_rating: dict[str, Any] | None = None
    manual_overrides: list[str] | None = None
    related_content: dict[str, list[dict[str, Any]]] | None = None


class TasteProfileResponse(FlexibleModel):
    summary: dict[str, Any]
    top_tags: list[dict[str, Any]]
    bottom_tags: list[dict[str, Any]]


class SyncRatingsResponse(FlexibleModel):
    backloggd: dict[str, Any]
    steam_reviews: dict[str, Any]
    tag_affinity_tags_updated: int
    status: str


class BacklogStatsResponse(FlexibleModel):
    playing: int
    completed: int
    abandoned: int
    evergreen: int
    # Money recorded on owned, effectively-unplayed games:
    # {"totals": [{currency, spent, count}], "top": [up to 5 priced games]}.
    unplayed_spend: dict[str, Any]


class RefreshLibraryResponse(FlexibleModel):
    status: str  # "started" or "already_running"
    platforms: list[str]
    already_running: bool


class SyncStatusResponse(FlexibleModel):
    status: str  # "in_progress" or "idle"
    started_at: str | None = None
    finished_at: str | None = None
    platforms: dict[str, dict[str, Any]]


class IntegrationStatusResponse(FlexibleModel):
    pass


class DetectFarmedGamesResponse(FlexibleModel):
    farming_days: list[dict[str, Any]]
    candidates: int
    steam_appids: list[int]
    threshold_hours: float
    dry_run: bool
    sample_games: list[dict[str, Any]]


class DetectCollapsedGamesResponse(FlexibleModel):
    collapsed_count: int
    candidates: list[dict[str, Any]]


class OrphanGameEntry(FlexibleModel):
    game_id: int
    name: str
    igdb_id: int | None = None


class DetectOrphanGamesResponse(FlexibleModel):
    orphans: list[OrphanGameEntry]
    orphan_count: int
    wishlist_only_count: int
    # {"configured": bool, "unclassified_at_last_run": int|None} — whether the
    # Steam license audit could still turn some of these "orphans" into owned
    # (retired) games. None = the audit has never run.
    license_audit: dict[str, Any] = {}


class AuditSteamLicensesResponse(FlexibleModel):
    status: str
    owned_licenses: int = 0
    library_appids: int = 0
    unclassified: int = 0
    probed: int = 0
    minted: list[dict[str, Any]] = []
    minted_delisted: list[dict[str, Any]] = []
    skipped_non_game: list[dict[str, Any]] = []
    unresolved: list[int] = []
    remaining: int = 0
    error_summary: str | None = None


class DetectStrandedDuplicatesResponse(FlexibleModel):
    stranded_count: int
    candidates: list[dict[str, Any]]


class PlatformBreakdownResponse(FlexibleModel):
    # Each by_platform entry: {platform, owned_games (primary items only),
    # owned_addons (owned DLC/expansions/editions on that platform)}.
    by_platform: list[dict[str, Any]]
    total_unique_games: int
    # Owned nested-content rows (DLC/expansions/editions/bundles), counted
    # separately from total_unique_games so addon ownership doesn't inflate it.
    total_unique_addons: int
    overlap_count: int
    overlap_games: list[dict[str, Any]]


class RateGameResponse(FlexibleModel):
    game_id: int
    name: str
    source: str
    score: float
    review_text: str | None = None
    tags_affected: list[str]
    tag_affinity_tags_updated: int


class HardwarePreferenceResponse(FlexibleModel):
    hardware_preference: list[str]


class AcquisitionInfo(FlexibleModel):
    acquired_at: str | None = None
    price_paid: float | None = None
    price_currency: str | None = None
    purchase_source: str | None = None
    bundle_name: str | None = None


class AddGameToPlatformResponse(FlexibleModel):
    created: bool
    game_id: int
    game_platform_id: int | None = None
    wishlist_id: int | None = None
    name: str
    platform: str
    owned: bool = True
    playtime_minutes: int | None = None
    identifier: dict[str, str] | None = None
    # Populated when acquisition params were passed (owned=True only).
    acquisition: AcquisitionInfo | None = None


class SyncWishlistResponse(FlexibleModel):
    pass


class WishlistItem(FlexibleModel):
    game_id: int
    name: str
    platform: str
    wishlisted_at: str | None = None
    source: str | None = None
    owned: bool
    # base_game (the common case) or a nested type (dlc/expansion/edition/…)
    # when the wishlisted item is itself DLC/an edition rather than a base game.
    content_type: str | None = None


class GetWishlistResponse(FlexibleModel):
    count: int
    items: list[WishlistItem]


class SessionIngestLinkResponse(FlexibleModel):
    url: str
    provider: str
    expires_in_minutes: int


class UpdateGameResponse(FlexibleModel):
    game_id: int
    name: str
    updated: dict[str, Any]
    cleared: list[str]
    manual_overrides: list[str]


class SetAcquisitionResponse(FlexibleModel):
    game_id: int
    name: str
    platform: str
    game_platform_id: int
    platform_row_created: bool
    acquisition: AcquisitionInfo
    cleared: list[str]


class SetPlaytimeResponse(FlexibleModel):
    game_id: int
    name: str
    platform: str
    game_platform_id: int
    platform_row_created: bool
    updated: dict[str, Any]
    cleared: list[str]
    playtime_minutes: int | None = None
    last_played: str | None = None
    manual_overrides: list[str]


class SetSwitch2PlaytimeBaselineResponse(FlexibleModel):
    game_id: int
    name: str
    platform: str
    application_id: str
    identifier_recorded: bool
    total_hours: float
    total_minutes: int
    synced_minutes: int
    baseline_minutes: int
    previous_baseline_minutes: int | None = None
    baseline_removed: bool
    playtime_minutes: int
    dry_run: bool


class AcquisitionBatchItemResult(FlexibleModel):
    # applied | filled | no_change | created | unmatched | no_platform_row | error
    status: str
    platform: str | None = None
    game_id: int | None = None
    matched_name: str | None = None
    match_type: str | None = None  # identifier | id | name | fuzzy | created
    acquisition: AcquisitionInfo | None = None
    # Present on no_platform_row: the platforms the game IS owned/recorded on.
    platforms: list[str] | None = None
    error: str | None = None
    # Original item payload, echoed on unmatched/no_platform_row/error.
    item: dict[str, Any] | None = None


class SetAcquisitionsBatchResponse(FlexibleModel):
    results: list[AcquisitionBatchItemResult]
    total: int
    applied: int
    filled: int
    no_change: int
    created: int
    # New owned games minted by create_missing (game_id, name, platform) — a
    # created row has no delete tool, so callers eyeball this list.
    created_details: list[dict[str, Any]]
    unmatched: list[dict[str, Any]]
    no_platform_row: int
    # Detail for the no_platform_row rows (game_id, matched_name, platforms),
    # so a caller can triage by name instead of an opaque count.
    no_platform_row_details: list[dict[str, Any]]
    errors: int


class BundleGameResult(FlexibleModel):
    # applied | filled | no_change | created | unmatched
    status: str
    game_id: int | None = None
    name: str | None = None  # input name, present on unmatched
    matched_name: str | None = None
    match_type: str | None = None  # identifier | id | name | fuzzy | created
    price_paid: float | None = None  # the proposed split share for this game
    # What actually persisted on the row (fill-only can preserve an older price,
    # so this may differ from price_paid); drives allocated_price/reconciled.
    recorded_price: float | None = None
    acquisition: AcquisitionInfo | None = None
    item: dict[str, Any] | None = None  # echoed on unmatched


class SplitBundleAcquisitionResponse(FlexibleModel):
    bundle_name: str
    platform: str
    dry_run: bool
    total_price: float | None = None
    price_currency: str | None = None
    games: list[BundleGameResult]
    # Rows actually written (applied+filled+created) — no_change rows excluded.
    recorded: int
    created: int
    no_change: int
    unmatched: int
    allocated_price: float
    unallocated_price: float
    reconciled: bool


class ImportPurchasesResponse(FlexibleModel):
    # Per-source results keyed by importer name (eshop, humble, ...). Each is
    # {source, status: "ok"|"error", ...} — ok carries fetched/applied/filled/
    # no_change/created/created_details/unmatched/no_platform_row/
    # bundles_needing_split/errors/skipped (or dry_run+proposed+would_create),
    # error carries the fetch failure message. bundles_needing_split holds
    # multi-game bundles to hand to split_bundle_acquisition (they're never
    # written by this tool); created_details names games minted from
    # unmatched single-game purchases (create_missing, default on).
    sources: dict[str, dict[str, Any]]
    dry_run: bool
    totals: dict[str, Any]


class SpendingStatsResponse(FlexibleModel):
    owned_rows: int
    priced_rows: int
    coverage_pct: float
    zero_cost_rows: int
    # Monetary aggregates are grouped by currency, never summed across them.
    totals: list[dict[str, Any]]
    by_year: list[dict[str, Any]]
    by_source: list[dict[str, Any]]
    by_platform: list[dict[str, Any]]
    by_bundle: list[dict[str, Any]]
    # Content-grouped spend: base game + its owned DLC/expansions rolled up per
    # content family (root COALESCE(parent_game_id, id)), only for families with
    # a real nested addon, top 10 per currency. Each: {family_game_id,
    # family_name, currency, base_spent, addon_spent, total_spent, addon_count,
    # family_playtime_hours, family_cost_per_hour}. Distinct from by_bundle,
    # which groups by a purchase's bundle_name.
    by_family: list[dict[str, Any]]
    top_expensive: list[dict[str, Any]]
    cost_per_hour: dict[str, Any]


class MergeGamesResponse(FlexibleModel):
    dry_run: bool
    source: dict[str, Any]
    target: dict[str, Any]
    platforms_moved: list[str]
    platforms_merged: list[str]
    ratings_moved: list[str]
    ratings_kept_target: list[str]
    series_memberships_transferred: int
    aliases_transferred: int
    source_deleted: bool


class SplitGameResponse(FlexibleModel):
    dry_run: bool
    source_game_id: int
    source_name: str
    new_game_id: int | None
    new_name: str
    platform: str
    identifiers_moved: list[str]
    moved_whole_platform: bool
    identifiers_remaining_on_source: list[str]


class DeleteGameResponse(FlexibleModel):
    deleted: bool
    game_id: int
    name: str
    # Populated on a confirm=False preview.
    would_delete: dict[str, int] | None = None
    hint: str | None = None
    # Populated on an actual delete (confirm=True).
    deleted_counts: dict[str, int] | None = None


class BatchItemResult(FlexibleModel):
    """Per-item envelope shared by the *_batch tools.

    status is "ok" or "error" plus tool-specific values (previewed/deleted/
    refused for delete_games_batch, stale_id for merge_games_batch). ok items
    additionally spread the single-item tool's full result keys.
    """

    status: str
    game_id: int | None = None
    name: str | None = None
    error: str | None = None
    # Original item payload, echoed on error.
    item: dict[str, Any] | None = None


class UpdateGamesBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    ok: int
    errors: int
    dry_run: bool
    # From the single deferred recompute; 0 when no tags changed or dry_run.
    tag_affinity_tags_updated: int


class AddGamesToPlatformBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    ok: int
    # ok items that minted a brand-new games row.
    created: int
    errors: int
    dry_run: bool


class SetPlaytimeBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    ok: int
    errors: int
    dry_run: bool


class RateGamesBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    ok: int
    errors: int
    dry_run: bool
    # From the single deferred recompute; 0 when nothing was written or dry_run.
    tag_affinity_tags_updated: int


class GameDetailsBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    ok: int
    errors: int
    # Always "skipped": batch detail never triggers lazy provider fetches.
    enrichment: str


class DeleteGamesBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    previewed: int
    deleted: int
    refused: int
    errors: int
    confirm: bool
    # Summed per-table counts: preview (confirm=False) vs actual (confirm=True).
    would_delete_total: dict[str, int] | None = None
    deleted_counts_total: dict[str, int] | None = None
    hint: str | None = None


class MergeGamesBatchResponse(FlexibleModel):
    results: list[BatchItemResult]
    total: int
    ok: int
    stale_id: int
    errors: int
    dry_run: bool
    # From the single deferred recompute; 0 when no ratings moved or dry_run.
    tag_affinity_tags_updated: int


class DetectCrossPlatformCollapsesResponse(FlexibleModel):
    checked: int
    collapsed_count: int
    candidates: list[dict[str, Any]]
    igdb_configured: bool


class DetectMisclassifiedDlcResponse(FlexibleModel):
    # Each candidate: {game_id, name, reason (bucket), evidence, suggested_update
    # (ready-to-apply update_game kwargs, or null)}. counts maps each bucket to
    # its candidate count; probed/probe_remaining track the live Steam probe;
    # skipped lists appids whose live fetch errored.
    candidates: list[dict[str, Any]]
    counts: dict[str, int]
    probed: int
    probe_remaining: int
    skipped: list[dict[str, Any]] = Field(default_factory=list)


class RevalidateIgdbMatchesResponse(FlexibleModel):
    dry_run: bool
    igdb_configured: bool
    checked: int
    mismatch_count: int
    mismatches: list[dict[str, Any]]
    reset_count: int
    skipped_overridden: int
    unresolved_igdb_ids: int


class ScrapeConfigVersion(FlexibleModel):
    version: int
    status: str
    source: str
    note: str | None = None
    created_at: str


class GetScrapeConfigResponse(FlexibleModel):
    provider: str
    on_defaults: bool
    defaults: dict[str, Any]
    active_override: dict[str, Any] | None = None
    effective_config: dict[str, Any]
    pending_versions: list[ScrapeConfigVersion]
    history: list[ScrapeConfigVersion]
    require_approval: bool


class DiagnoseScrapeResponse(FlexibleModel):
    provider: str
    status: str
    active_config: dict[str, Any] | None = None
    parsed_rows: int | None = None
    selector_matches: dict[str, int] | None = None
    untrusted_page_excerpt: str | None = None


class ProposeScrapeConfigResponse(FlexibleModel):
    provider: str
    applied: bool
    status: str
    version: int | None = None
    validation: dict[str, Any]


class ApproveScrapeConfigResponse(FlexibleModel):
    provider: str
    status: str
    version: int
    effective_config: dict[str, Any]


class RollbackScrapeConfigResponse(FlexibleModel):
    provider: str
    restored_version: int | None = None
    on_defaults: bool
    effective_config: dict[str, Any]


class WishlistDealAlternative(FlexibleModel):
    platform: str
    shop: str
    price: float
    regular_price: float | None = None
    cut_pct: int | None = None
    currency: str | None = None
    deal_url: str | None = None


class WishlistDealEntry(FlexibleModel):
    game_id: int
    name: str
    platform: str
    shop: str
    price: float
    regular_price: float | None = None
    cut_pct: int | None = None
    currency: str | None = None
    deal_url: str | None = None
    wishlisted_at: str | None = None
    wishlisted_on: list[str] = Field(default_factory=list)
    recommendation_reason: str | None = None
    alternatives: list[WishlistDealAlternative] = Field(default_factory=list)


class CompletionSuggestion(FlexibleModel):
    game_id: int
    name: str
    suggested_status: str
    reason: str
    playtime_hours: float
    # None on the no-HLTB evergreen branch (a game with no HowLongToBeat
    # main-story entry can still be suggested as evergreen on playtime alone).
    hltb_main: float | None = None
    last_played: str | None = None


class CompletionSuggestionsResponse(FlexibleModel):
    suggestions: list[CompletionSuggestion]
    count: int


class WishlistDealsResponse(FlexibleModel):
    deals: list[WishlistDealEntry]
    unpriced: list[str]
    fetched_at: str
    count: int
    price_refresh_errors: list[str] | None = None
    itad: str | None = None
    currency_note: str | None = None
    switch2_lookups_deferred: int | None = None
    availability_pending: int | None = None


class PlayHistoryWindow(FlexibleModel):
    start: str
    end: str


class PlayHistoryEntry(FlexibleModel):
    game_id: int
    name: str
    platform: str
    minutes_played: int
    hours_played: float


class PlayHistoryResponse(FlexibleModel):
    window: PlayHistoryWindow
    total_minutes: int
    total_hours: float
    by_platform: dict[str, int]
    games: list[PlayHistoryEntry]
    switch2_unmatched_minutes: int
