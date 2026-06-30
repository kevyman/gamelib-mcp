"""Pydantic response models for MCP output schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict, RootModel


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
    platforms: list[PlatformEntry] | None = None
    playtime_hours: float | None = None
    playtime_2weeks_hours: float | None = None
    hltb_main: float | None = None
    metacritic_score: int | None = None
    opencritic_score: int | None = None
    protondb_tier: str | None = None
    steam_review_desc: str | None = None
    is_farmed: bool | None = None
    content_type: str | None = None
    parent_game_id: int | None = None
    is_primary_library_item: bool | None = None
    play_state: str | None = None
    matched_alias: str | None = None
    tags: list[str] | None = None
    series: list[Any] | None = None
    suggested_platform: str | None = None
    match_score: float | None = None
    match_type: str | None = None


class PaginatedGamesResponse(FlexibleModel):
    results: list[GameSummary]
    total_matches: int
    has_more: bool


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


class LibraryStatsResponse(PaginatedGamesResponse):
    total_games: int
    played: int
    unplayed: int
    unknown: int
    farmed_games: int
    total_playtime_hours: float
    filter: str
    sort_by: str


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
    pass


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


class AddGameToPlatformResponse(FlexibleModel):
    created: bool
    game_id: int
    game_platform_id: int
    name: str
    platform: str
    playtime_minutes: int | None = None
    identifier: dict[str, str] | None = None


class NintendoSessionResponse(FlexibleModel):
    cookie_count: int
    path: str


class UpdateGameResponse(FlexibleModel):
    game_id: int
    name: str
    updated: dict[str, Any]
    cleared: list[str]
    manual_overrides: list[str]


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
