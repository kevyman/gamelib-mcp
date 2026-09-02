"""Pydantic response models for MCP output schemas."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FlexibleModel(BaseModel):
    """Allow provider-specific keys while still exposing useful schema anchors."""

    model_config = ConfigDict(extra="allow")


class BatchItemResult(FlexibleModel):
    """Per-item envelope for a tool's bulk (``items=``) mode.

    status is "ok" or "error" plus tool-specific values (previewed/deleted/
    refused for delete_game, stale_id for merge_games). ok items additionally
    spread the single-item result's full keys.
    """

    status: str
    game_id: int | None = None
    name: str | None = None
    error: str | None = None
    # Original item payload, echoed on error.
    item: dict[str, Any] | None = None


class BatchEnvelope(FlexibleModel):
    """Top-level keys every bulk-mode response carries.

    Merged single/bulk tools declare every field optional: a single-item call
    returns the flat result keys and none of these, an ``items=`` call returns
    these and none of the flat keys. See ADR 0004.
    """

    results: list[BatchItemResult] | None = None
    total: int | None = None
    ok: int | None = None
    errors: int | None = None
    dry_run: bool | None = None


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


class SearchGamesResponse(FlexibleModel):
    """search_games in either mode.

    ``query`` mode fills results/total_matches/has_more (the paginated
    envelope); ``queries`` mode fills results_by_query instead.
    """

    results: list[GameSummary] | None = None
    total_matches: int | None = None
    has_more: bool | None = None
    results_by_query: dict[str, list[GameSummary]] | None = None


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



class SeriesGapMember(FlexibleModel):
    igdb_id: int
    name: str
    release_date: str | None = None
    game_type: int
    available_on: list[str] = Field(default_factory=list)
    # True when a wishlisted (but unowned) library game resolves to this
    # member by igdb_id, edition alias, or normalized name.
    on_wishlist: bool = False
    # Names of edition/re-release members collapsed into this canonical entry
    # (one missing game must not count as two gaps). Absent when none.
    variants: list[str] | None = None


class SeriesGapEntry(FlexibleModel):
    series_id: int
    series_name: str
    kind: str
    owned_count: int
    avg_rating: float | None = None
    total_playtime_hours: float
    gaps: list[SeriesGapMember] = Field(default_factory=list)
    # Members dropped because IGDB lists them on no platform this library
    # tracks (dead-platform / region-locked); include_unavailable=True keeps them.
    unavailable_excluded: int = 0


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
    # Recorded verdicts (record_assessment), newest first — SINGLE mode only,
    # capped at 5 with assessment_count the true total. Absent when the game
    # was never assessed.
    assessments: list[dict[str, Any]] | None = None
    assessment_count: int | None = None
    assessments_truncated: bool | None = None
    # media=True (SINGLE mode only): the neutral game representation —
    # trailer + screenshots (capped, with screenshot_count/truncated), IGDB's
    # similar games annotated with ownership (capped, with count/truncated),
    # and `pedigree` — the developer (name, founding year, catalogue size),
    # their previous games annotated with ownership/rating/playtime (capped at
    # 6, empty under the big-studio damper) and library_track_record. All
    # absent when nothing resolved or the lookup failed.
    media: dict[str, Any] | None = None
    similar: dict[str, Any] | None = None
    pedigree: dict[str, Any] | None = None
    # Bulk (items=) mode: GameSummary's game_id/name move inside each result.
    game_id: int | None = None  # type: ignore[assignment]
    name: str | None = None  # type: ignore[assignment]
    results: list[BatchItemResult] | None = None
    total: int | None = None
    ok: int | None = None
    errors: int | None = None
    # "skipped" whenever lazy provider fetches were not run (always in bulk).
    enrichment: str | None = None





class GetStatsResponse(FlexibleModel):
    """One aggregate report, selected by `report`.

    Only the selected report's keys are present; `report` echoes which one ran.
    The per-report payloads are produced unchanged by the same functions that
    backed the five separate tools this replaced (see ADR 0004).
    """

    report: str
    # report="backlog"
    playing: int | None = None
    completed: int | None = None
    abandoned: int | None = None
    evergreen: int | None = None
    unplayed_spend: dict[str, Any] | None = None
    # report="platforms"
    by_platform: list[dict[str, Any]] | None = None
    total_unique_games: int | None = None
    total_unique_addons: int | None = None
    # overlap_count is the true total; overlap_games is capped at overlap_limit.
    overlap_count: int | None = None
    overlap_truncated: bool | None = None
    overlap_limit: int | None = None
    overlap_games: list[dict[str, Any]] | None = None
    # report="taste"
    summary: dict[str, Any] | None = None
    top_tags: list[dict[str, Any]] | None = None
    bottom_tags: list[dict[str, Any]] | None = None
    # The estimated affinity scale (prior weight + variance components) the
    # tags above are expressed on — affinity_score has no fixed scale.
    shrinkage: dict[str, Any] | None = None
    # Owned, unrated games whose ratings would teach the profile most, capped
    # at RATE_NEXT_LIMIT; rate_next_candidates is the true total.
    rate_next: list[dict[str, Any]] | None = None
    rate_next_candidates: int | None = None
    rate_next_truncated: bool | None = None  # candidates > len(rate_next)
    # report="spending"
    owned_rows: int | None = None
    priced_rows: int | None = None
    coverage_pct: float | None = None
    zero_cost_rows: int | None = None
    totals: list[dict[str, Any]] | None = None
    by_year: list[dict[str, Any]] | None = None
    by_source: list[dict[str, Any]] | None = None
    by_bundle: list[dict[str, Any]] | None = None
    # by_bundle is capped; these carry the true total and the flag.
    by_bundle_count: int | None = None
    by_bundle_truncated: bool | None = None
    by_family: list[dict[str, Any]] | None = None
    top_expensive: list[dict[str, Any]] | None = None
    cost_per_hour: dict[str, Any] | None = None
    # report="series" (the only paginated report)
    results: list[SeriesBreakdownEntry] | None = None
    counting_mode: str | None = None
    # report="series" and report="assessments" (both paginated)
    total_matches: int | None = None
    has_more: bool | None = None
    # report="assessments" — the page itself, plus the verdict filter echoed.
    assessments: list[dict[str, Any]] | None = None
    verdict: str | None = None
    # report="calibration"
    overall: dict[str, Any] | None = None
    by_verdict: list[dict[str, Any]] | None = None
    # Declared methodology provenance, each a capped {items, count, truncated}
    # block including the unknown (NULL) bucket.
    by_methodology: dict[str, Any] | None = None
    by_model: dict[str, Any] | None = None
    wishlist_for_sale: dict[str, Any] | None = None
    mismatches: dict[str, Any] | None = None
    play_what_you_own_follow_through: dict[str, Any] | None = None


class SyncResponse(FlexibleModel):
    """One entry per requested target; absent targets are omitted.

    `library` is a fire-and-forget ack ({status, platforms, already_running})
    polled via get_sync_status; `wishlist` and `ratings` are blocking and
    carry their results inline.
    """

    library: dict[str, Any] | None = None
    wishlist: dict[str, Any] | None = None
    ratings: dict[str, Any] | None = None
    targets: list[str]


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


class CheckFinding(FlexibleModel):
    check: str
    severity: str
    game_id: int | None = None
    name: str | None = None
    message: str
    evidence: dict[str, Any] = {}
    suggested_action: dict[str, Any] | None = None


class CheckLibraryResponse(FlexibleModel):
    findings: list[CheckFinding] = []
    # check_id -> {"findings": n, "max_severity": s|None, "truncated": bool, **extras}
    summary: dict[str, dict[str, Any]] = {}
    checks_run: list[str] = []
    # {"check", "reason"}: "not_selected_network", "unconfigured:igdb", "unconfigured:steam_session"
    checks_skipped: list[dict[str, Any]] = []
    # check_id -> apply result; populated only for check ids named in `apply`.
    applied: dict[str, dict[str, Any]] = {}
    # {"check", "error"} — per-check isolation: one check raising never kills the run.
    errors: list[dict[str, Any]] = []
    suppressed_count: int = 0
    suppressions_changed: int = 0
    # id/category/description/network/writes_on_apply/default_severity/options — populated
    # only when list_checks=True.
    catalog: list[dict[str, Any]] = []



class RateGameResponse(BatchEnvelope):
    game_id: int | None = None
    name: str | None = None
    source: str | None = None
    score: float | None = None
    review_text: str | None = None
    tags_affected: list[str] | None = None
    # Single mode: this call's recompute. Bulk mode: the one deferred recompute
    # run after every item was written (0 when nothing was written or dry_run).
    tag_affinity_tags_updated: int | None = None


class HardwarePreferenceResponse(FlexibleModel):
    hardware_preference: list[str]


class AcquisitionInfo(FlexibleModel):
    acquired_at: str | None = None
    price_paid: float | None = None
    price_currency: str | None = None
    purchase_source: str | None = None
    bundle_name: str | None = None


class AcquisitionBatchItemResult(FlexibleModel):
    # applied | filled | no_change | created | unmatched | no_platform_row | error
    status: str
    platform: str | None = None
    game_id: int | None = None
    matched_name: str | None = None
    match_type: str | None = None  # identifier | id | name | alias | fuzzy | created
    acquisition: AcquisitionInfo | None = None
    # Present on no_platform_row: the platforms the game IS owned/recorded on.
    platforms: list[str] | None = None
    error: str | None = None
    # Original item payload, echoed on unmatched/no_platform_row/error.
    item: dict[str, Any] | None = None


class StorePushResult(FlexibleModel):
    """The outcome of add_game_to_platform(push_to_store=True) for one item."""

    # attempted: the push function was actually invoked (steam with a resolved
    # appid); False for switch2/manual-path, unsupported platforms, and
    # missing-appid errors.
    attempted: bool
    pushed: bool
    # Steam success: which route landed it ("webapi" | "storefront") + the appid.
    via: str | None = None
    appid: str | None = None
    wishlist_count: int | None = None
    # Failure / unsupported-platform / missing-appid explanation.
    error: str | None = None
    # switch2 only: DekuDeals has no write API — manual add link + why.
    manual_url: str | None = None
    note: str | None = None


class AddGameToPlatformResponse(BatchEnvelope):
    # Single mode: did this call mint a new games row. Bulk mode: how many
    # items did (the batch reports a count under the same key).
    created: bool | int | None = None
    game_id: int | None = None
    game_platform_id: int | None = None
    wishlist_id: int | None = None
    name: str | None = None
    platform: str | None = None
    owned: bool | None = None
    playtime_minutes: int | None = None
    identifier: dict[str, str] | None = None
    # Populated when acquisition params were passed (owned=True only).
    acquisition: AcquisitionInfo | None = None
    # Echoes the delisted flag when one was passed (owned=True only); null
    # means the column was left to sync/the license audit.
    delisted: bool | None = None
    # The row's ownership-ended stamp after the call (refund/revoked key/lapsed
    # subscription). Null on every ordinary add, and after unowned_at="none"
    # restores ownership.
    unowned_at: str | None = None
    # Single mode: push_to_store=True on a wet (non-dry-run) call only — null
    # otherwise (push_to_store=False, or dry_run even with push_to_store=True).
    store_push: StorePushResult | None = None
    # Filled on owned=False results (single or per-item): the
    # game_wishlist.source value the row actually holds after the call —
    # "manual" when wishlist_source wasn't passed, and the PRESERVED existing
    # source when the game was already wishlisted (a hand write never rewrites
    # provenance). Null when owned=True.
    wishlist_source: str | None = None



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
    # The latest recorded verdict for this game ({verdict, assessed_at,
    # target_price}); absent when the game was never assessed.
    assessment: dict[str, Any] | None = None


class GetWishlistResponse(FlexibleModel):
    """get_wishlist in either mode.

    Default mode fills count/items. with_prices=True fills the deal keys
    instead (live ITAD/DekuDeals lookup).
    """

    count: int | None = None
    total_matches: int | None = None
    has_more: bool | None = None
    items: list[WishlistItem] | None = None
    # with_prices=True
    deals: list["WishlistDealEntry"] | None = None
    unpriced: list[str] | None = None
    fetched_at: str | None = None
    price_refresh_errors: list[str] | None = None
    itad: str | None = None
    currency_note: str | None = None
    # The capped per-title DekuDeals lookup queue, each omitted when zero:
    # priced this call / still outstanding after it (the real remaining
    # backlog, not a static candidates-minus-cap figure) / confirmed absent
    # from DekuDeals and negatively cached / excluded because no IGDB platform
    # list says whether a Switch release exists at all.
    switch2_lookups_performed: int | None = None
    switch2_lookups_deferred: int | None = None
    switch2_lookups_not_found: int | None = None
    switch2_availability_unknown: int | None = None
    availability_pending: int | None = None


class SessionIngestLinkResponse(FlexibleModel):
    url: str
    provider: str
    expires_in_minutes: int


class SkillIndexEntry(FlexibleModel):
    name: str
    description: str
    version: str
    # Relative paths accepted by get_skill(skill=..., path=...); the same
    # files are served as skill://<name>/<path> resources.
    files: list[str]


class GetSkillResponse(FlexibleModel):
    """get_skill in either mode (ADR 0006 decision 4b)."""

    # Index mode (no arguments): every skill this server carries.
    skills: list[SkillIndexEntry] | None = None
    # Set only in index mode when the server has no skills directory at all —
    # a deployment packaging bug worth surfacing over an empty list.
    note: str | None = None
    # File mode (skill= given): one file's text, to load into context.
    skill: str | None = None
    path: str | None = None
    version: str | None = None
    content: str | None = None


class UpdateGameResponse(BatchEnvelope):
    game_id: int | None = None
    name: str | None = None
    updated: dict[str, Any] | None = None
    cleared: list[str] | None = None
    manual_overrides: list[str] | None = None
    # Bulk mode: the single deferred recompute; 0 when no tags changed.
    tag_affinity_tags_updated: int | None = None


class SetAcquisitionResponse(FlexibleModel):
    """set_acquisition in either mode.

    Bulk mode carries its own richer per-item status vocabulary (applied/
    filled/no_change/created/unmatched/no_platform_row/error) rather than the
    plain ok/error of BatchEnvelope, so it declares `results` itself.
    """

    # Single mode.
    game_id: int | None = None
    name: str | None = None
    platform: str | None = None
    game_platform_id: int | None = None
    platform_row_created: bool | None = None
    acquisition: AcquisitionInfo | None = None
    cleared: list[str] | None = None
    # Bulk (items=) mode.
    results: list[AcquisitionBatchItemResult] | None = None
    total: int | None = None
    applied: int | None = None
    filled: int | None = None
    no_change: int | None = None
    created: int | None = None
    # New owned games minted by create_missing (game_id, name, platform) —
    # nothing flags a bad mint after the fact (cleanup is a manual
    # delete_game), so callers eyeball this list.
    created_details: list[dict[str, Any]] | None = None
    unmatched: list[dict[str, Any]] | None = None
    # Items create_missing declined to mint (near-duplicate of an existing row,
    # or nested content with no parent). Counted in unmatched; this says why.
    create_refused_details: list[dict[str, Any]] | None = None
    no_platform_row: int | None = None
    # Detail for the no_platform_row rows (game_id, matched_name, platforms),
    # so a caller can triage by name instead of an opaque count.
    no_platform_row_details: list[dict[str, Any]] | None = None
    errors: int | None = None
    dry_run: bool | None = None


class SetPlaytimeResponse(BatchEnvelope):
    game_id: int | None = None
    name: str | None = None
    platform: str | None = None
    game_platform_id: int | None = None
    platform_row_created: bool | None = None
    updated: dict[str, Any] | None = None
    cleared: list[str] | None = None
    playtime_minutes: int | None = None
    last_played: str | None = None
    manual_overrides: list[str] | None = None


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


class BundleGameResult(FlexibleModel):
    # applied | filled | no_change | created | unmatched
    status: str
    game_id: int | None = None
    name: str | None = None  # input name, present on unmatched
    matched_name: str | None = None
    match_type: str | None = None  # identifier | id | name | alias | fuzzy | created
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
    # Every list here that grows with the fetched history rather than with a
    # fixed vocabulary — created_details, unmatched, unmatched_free,
    # unconfirmed_unmatched, skipped, bundles_needing_split,
    # create_refused_details, no_platform_row_details, family_conflict_details
    # — is CAPPED at 200 entries per source and ships <list>_count (the true
    # total) and <list>_truncated beside it. Read the count, never len() of the
    # list: `totals` does.
    sources: dict[str, dict[str, Any]]
    dry_run: bool
    totals: dict[str, Any]



class MergeGamesResponse(BatchEnvelope):
    source: dict[str, Any] | None = None
    target: dict[str, Any] | None = None
    platforms_moved: list[str] | None = None
    platforms_merged: list[str] | None = None
    ratings_moved: list[str] | None = None
    ratings_kept_target: list[str] | None = None
    series_memberships_transferred: int | None = None
    aliases_transferred: int | None = None
    # Bulk mode: items whose source/target id was consumed by an earlier merge.
    stale_id: int | None = None
    # Bulk mode: the single deferred recompute; 0 when no ratings moved.
    tag_affinity_tags_updated: int | None = None
    play_history_rows_transferred: int = 0
    # game_wishlist / game_prices FK-cascade with the source row, so the merge
    # transfers them explicitly; "dropped" = target already had the row (or the
    # wishlist entry was fulfilled by a platform the merged target owns).
    wishlist_entries_transferred: int = 0
    wishlist_entries_dropped: int = 0
    price_rows_transferred: int = 0
    price_rows_dropped: int = 0
    # Children of the source re-pointed at the target; a nested target that
    # absorbed its parent or inherited children is promoted to primary.
    children_reparented: int = 0
    target_promoted_to_primary: bool = False
    # Single mode only: True after a wet merge deleted the source row, False on
    # dry_run (bulk mode carries it per item instead). Optional like every other
    # mode-dependent field (ADR 0004) — declaring it required made the bulk
    # (items=) envelope fail output validation, which blocked dry-run previews.
    source_deleted: bool | None = None


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
    # Single mode: was this game deleted. Bulk mode: how many were.
    deleted: bool | int | None = None
    game_id: int | None = None
    name: str | None = None
    # Populated on a confirm=False preview.
    would_delete: dict[str, int] | None = None
    hint: str | None = None
    # Populated on an actual delete (confirm=True).
    deleted_counts: dict[str, int] | None = None
    # Bulk (items=) mode.
    results: list[BatchItemResult] | None = None
    total: int | None = None
    previewed: int | None = None
    refused: int | None = None
    errors: int | None = None
    confirm: bool | None = None
    # Summed per-table counts: preview (confirm=False) vs actual (confirm=True).
    would_delete_total: dict[str, int] | None = None
    deleted_counts_total: dict[str, int] | None = None


class ScrapeConfigVersion(FlexibleModel):
    version: int
    status: str
    source: str
    note: str | None = None
    created_at: str


class GetScrapeConfigResponse(FlexibleModel):
    """get_scrape_config in either mode: stored config, or a live diagnose."""

    provider: str
    # diagnose=False
    on_defaults: bool | None = None
    defaults: dict[str, Any] | None = None
    active_override: dict[str, Any] | None = None
    effective_config: dict[str, Any] | None = None
    pending_versions: list[ScrapeConfigVersion] | None = None
    history: list[ScrapeConfigVersion] | None = None
    require_approval: bool | None = None
    # diagnose=True
    status: str | None = None
    active_config: dict[str, Any] | None = None
    parsed_rows: int | None = None
    selector_matches: dict[str, int] | None = None
    untrusted_page_excerpt: str | None = None


class ManageScrapeConfigResponse(FlexibleModel):
    """propose / approve / rollback — the union of their three result shapes."""

    provider: str
    status: str | None = None
    # action="propose"
    applied: bool | None = None
    validation: dict[str, Any] | None = None
    # action="propose" | "approve"
    version: int | None = None
    # action="approve" | "rollback"
    effective_config: dict[str, Any] | None = None
    # action="rollback"
    restored_version: int | None = None
    on_defaults: bool | None = None


class WishlistDealAlternative(FlexibleModel):
    platform: str
    shop: str
    price: float
    regular_price: float | None = None
    cut_pct: int | None = None
    currency: str | None = None
    deal_url: str | None = None
    # ITAD's all-time low for the game, in its OWN currency (never converted),
    # and when this deal expires. Null on every DekuDeals (switch2) row.
    history_low: float | None = None
    history_low_currency: str | None = None
    deal_ends_at: str | None = None


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
    history_low: float | None = None
    history_low_currency: str | None = None
    deal_ends_at: str | None = None
    # True only when the RECOMMENDED price has reached history_low in the SAME
    # currency; a missing low or a currency mismatch is False, not unknown.
    at_history_low: bool | None = None
    wishlisted_at: str | None = None
    wishlisted_on: list[str] = Field(default_factory=list)
    recommendation_reason: str | None = None
    alternatives: list[WishlistDealAlternative] = Field(default_factory=list)
    # Latest recorded verdict ({verdict, assessed_at, target_price}), absent
    # when never assessed; below_assessed_target is set only when the best
    # same-currency price has reached that target.
    assessment: dict[str, Any] | None = None
    below_assessed_target: bool | None = None


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



class AssessmentAnchor(FlexibleModel):
    """One owned library game sharing the candidate's core tags."""

    game_id: int
    name: str
    # Which of the candidate's core tags this game carries.
    matched_core_tags: list[str] = Field(default_factory=list)
    # {source, score} from the highest-weight explicit rating; None if unrated.
    rating: dict[str, Any] | None = None
    playtime_hours: float | None = None
    completion_status: str | None = None
    play_state: str | None = None


class AssessmentResolution(FlexibleModel):
    """How an assessment tool resolved the identity it was given (issue #150).

    mode: "by_id" | "by_appid" | "by_assessed_appid" | "exact" | "minted"
    (record_assessment) / plus "partial" | "fuzzy" | "none"
    (get_assessment_context, whose name matching stays loose). matched_name is
    the resolved row's games.name — absent when nothing resolved.
    rejected_near_miss is filled only by get_assessment_context, when the
    trailing-ordinal guard refused a sequel-shaped match and answered
    not_found instead.
    """

    mode: str
    query: str | None = None
    matched_name: str | None = None
    rejected_near_miss: str | None = None


class AssessmentContextResponse(FlexibleModel):
    """get_assessment_context: every block optional — presence depends on inputs.

    craft — when caller review numbers were passed (source="caller", full
    sample-adjusted formula) or the resolved game has cached Steam review
    data (source="server_cache": review enum/description only, no counts,
    with as_of + limitations). Absent when neither exists.
    fit / anchors / anchor_count / anchors_truncated — whenever candidate
    tags exist (caller-supplied `tags`, else the resolved game's stored
    tags). anchors is CAPPED at 8; anchor_count is the true total.
    game / game_resolution — when identity (name/appid/game_id) was given;
    game only when it resolved (game_resolution="resolved").
    past_assessments (+ count/truncated) — when identity resolved AND the
    game carries recorded verdicts; CAPPED at 5, newest first.
    deal — when identity resolved AND a price is cached for it.
    pace — always (last-30-day play summary).
    """

    craft: dict[str, Any] | None = None
    fit: dict[str, Any] | None = None
    game: dict[str, Any] | None = None
    # "resolved" | "not_found"; absent when no identity was passed.
    game_resolution: str | None = None
    # How that resolution happened; absent when no identity was passed.
    resolution: AssessmentResolution | None = None
    anchors: list[AssessmentAnchor] | None = None
    anchor_count: int | None = None
    anchors_truncated: bool | None = None
    past_assessments: list[dict[str, Any]] | None = None
    past_assessment_count: int | None = None
    past_assessments_truncated: bool | None = None
    # Cheapest CACHED price for a resolved game ({platform, shop, price,
    # currency, cut_pct, history_low, at_history_low, deal_ends_at,
    # fetched_at, stale}); absent when nothing is cached. Never fetched here.
    deal: dict[str, Any] | None = None
    pace: dict[str, Any] | None = None


class RecordAssessmentResponse(BatchEnvelope):
    """record_assessment in either of its two modes (ADR 0004: all optional).

    Single mode fills the flat keys below; an ``items=`` call fills
    BatchEnvelope's results/total/ok/errors instead. Voiding a misfiled row is
    its own tool (see VoidAssessmentResponse) — a hard delete is not
    idempotent, and this write is.
    """

    game_id: int | None = None
    name: str | None = None
    # True when the candidate had no library row and one was minted.
    created: bool | None = None
    # True when a same-day assessment already existed and was replaced.
    replaced: bool | None = None
    assessment_id: int | None = None
    assessed_at: str | None = None
    verdict: str | None = None
    # How identity resolved (mode/query/matched_name); single mode only.
    resolution: AssessmentResolution | None = None
    # Only when this game was assessed on an EARLIER day:
    # {previous_count, last_assessed_at, last_verdict}.
    repeat_ask: dict[str, Any] | None = None
    # The add_game_to_platform promotion to offer for a wishlist_for_sale
    # verdict on an unwishlisted game. Reported, never performed.
    suggested_action: dict[str, Any] | None = None
    # Single recording mode only: the evaluation-card payload assembled after
    # the write — game/verdict/summary, the declared presentation, comparisons
    # and anchors resolved against the library, craft, fit_call, flags,
    # ownership, time, price, media, similar games, pedigree (the developer
    # and their previous games, annotated), past verdicts, and an
    # `errors` list naming whatever could not be gathered. Left untyped (like
    # the other display blocks here) because it is a render payload read whole
    # by the widget, not a queried structure; batch and void modes omit it.
    package: dict[str, Any] | None = None


class VoidAssessmentResponse(FlexibleModel):
    """void_assessment: the recorded row that was hard-deleted.

    Every key is always present except suggested_action, which appears only
    when the void left the game row with no ownership, wishlist entry or
    assessment behind — a delete_game preview, reported and never performed.
    """

    voided: bool
    assessment_id: int
    game_id: int
    name: str
    verdict: str
    assessed_at: str
    suggested_action: dict[str, Any] | None = None


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
    # Growth suppressed by the last-played gate: a stored-total correction
    # (sync fix, set_playtime pin) landing in this window rather than play.
    excluded_stale_games: int = 0
    excluded_stale_minutes: int = 0
