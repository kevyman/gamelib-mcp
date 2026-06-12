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
    tags: list[str] | None = None
    suggested_platform: str | None = None
    match_score: float | None = None
    match_type: str | None = None


class PaginatedGamesResponse(FlexibleModel):
    results: list[GameSummary]
    total_matches: int
    has_more: bool


class SearchGamesBatchResponse(RootModel[dict[str, list[GameSummary]]]):
    pass


class LibraryStatsResponse(PaginatedGamesResponse):
    total_games: int
    played: int
    unplayed: int
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


class RefreshLibraryResponse(RootModel[dict[str, dict[str, Any]]]):
    pass


class IntegrationStatusResponse(FlexibleModel):
    pass


class DetectFarmedGamesResponse(FlexibleModel):
    farming_days: list[dict[str, Any]]
    candidates: int
    steam_appids: list[int]
    threshold_hours: float
    dry_run: bool
    sample_games: list[dict[str, Any]]


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
