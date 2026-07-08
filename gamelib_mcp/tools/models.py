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


class DetectStrandedDuplicatesResponse(FlexibleModel):
    stranded_count: int
    candidates: list[dict[str, Any]]


class PlatformBreakdownResponse(FlexibleModel):
    by_platform: list[dict[str, Any]]
    total_unique_games: int
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


class GetWishlistResponse(FlexibleModel):
    count: int
    items: list[WishlistItem]


class NintendoSessionResponse(FlexibleModel):
    cookie_count: int
    path: str


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


class AcquisitionBatchItemResult(FlexibleModel):
    # applied | filled | no_change | unmatched | no_platform_row | error
    status: str
    platform: str | None = None
    game_id: int | None = None
    matched_name: str | None = None
    match_type: str | None = None  # id | name | fuzzy
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
    unmatched: list[dict[str, Any]]
    no_platform_row: int
    errors: int


class ImportPurchasesResponse(FlexibleModel):
    # Per-source results keyed by importer name (eshop, humble, ...). Each is
    # {source, status: "ok"|"error", ...} — ok carries fetched/applied/filled/
    # no_change/unmatched/no_platform_row/errors/skipped (or dry_run+proposed),
    # error carries the fetch failure message.
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


class DetectCrossPlatformCollapsesResponse(FlexibleModel):
    checked: int
    collapsed_count: int
    candidates: list[dict[str, Any]]
    igdb_configured: bool


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
