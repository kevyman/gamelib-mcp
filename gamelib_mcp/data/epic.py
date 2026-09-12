"""Epic Games Store library sync via Legendary's local cache.

Requires a readable Legendary config directory containing at least:
- ``user.json`` for Epic auth tokens
- ``metadata/*.json`` for owned game metadata

Set ``EPIC_LEGENDARY_PATH`` to override the config directory. In Docker, mount
that directory read-only into the container and point ``EPIC_LEGENDARY_PATH``
at the mount path.
"""

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from gamelib_mcp.data.content import (
    CONTENT_BASE_GAME,
    CONTENT_DLC,
    NESTED_CONTENT_TYPES,
    ContentClassification,
)
from gamelib_mcp.data.db import (
    EPIC_ARTIFACT_ID,
    adopt_platform_identifier,
    apply_content_classification,
    get_db,
    get_game_by_identifier,
    get_manual_overrides,
    load_fuzzy_candidates,
    resolve_parent_game,
    upsert_game,
    upsert_game_alias,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB, resolve_and_link_game
from gamelib_mcp.data.last_played import EPIC_LAST_PLAYED_KEYS, extract_last_played
from gamelib_mcp.data.title_normalization import (
    normalize_search_text,
    prepare_catalog_title,
)

logger = logging.getLogger(__name__)

_EPIC_OAUTH_URL = "https://account-public-service-prod03.ol.epicgames.com/account/api/oauth/token"
_EPIC_PLAYTIME_URL = (
    "https://library-service.live.use1a.on.epicgames.com/library/api/public/playtime/account/"
    "{account_id}/all"
)
_EPIC_CLIENT_ID = os.getenv("EPIC_CLIENT_ID", "34a02cf8f4414e29b15921876da36f9a")
_EPIC_CLIENT_SECRET = os.getenv("EPIC_CLIENT_SECRET", "daafbccc737745039dffe53d94fc76cf")
_EPIC_USER_AGENT = "UELauncher/11.0.1-14907503+++Portal+Release-Live Windows/10.0.19041.1.256.64bit"
_EPIC_TIMEOUT = 20.0
_TOKEN_REFRESH_SKEW = timedelta(minutes=10)
# Legendary's own non-game rules: the "ue" asset namespace is the Unreal
# Engine marketplace, and a "mods" category entry marks user-made content.
_UE_ASSET_NAMESPACE = "ue"
_MOD_CATEGORY_PATH = "mods"


class EpicConfigurationError(RuntimeError):
    """Raised when local Legendary auth state is missing or stale."""


def _legendary_config_path() -> Path:
    configured = os.getenv("EPIC_LEGENDARY_PATH") or os.getenv("LEGENDARY_CONFIG_PATH")
    if configured:
        return Path(configured).expanduser()

    xdg_config_home = os.getenv("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "legendary"

    return Path.home() / ".config" / "legendary"


def _token_expiring_soon(expires_at: str | None) -> bool:
    if not expires_at:
        return True

    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return True

    return expiry <= datetime.now(UTC) + _TOKEN_REFRESH_SKEW


async def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


async def _load_epic_user_data() -> dict[str, Any]:
    user_path = _legendary_config_path() / "user.json"
    if not user_path.is_file():
        raise EpicConfigurationError(f"missing Epic credentials file: {user_path}")

    data = await _read_json_file(user_path)
    if not isinstance(data, dict):
        raise EpicConfigurationError(f"unexpected Epic credentials payload in {user_path}")
    return data


async def _refresh_epic_session(refresh_token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(
        timeout=_EPIC_TIMEOUT,
        headers={"User-Agent": _EPIC_USER_AGENT},
        auth=(_EPIC_CLIENT_ID, _EPIC_CLIENT_SECRET),
    ) as client:
        response = await client.post(
            _EPIC_OAUTH_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "token_type": "eg1",
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                raise EpicConfigurationError(
                    "Legendary refresh token rejected; rerun `legendary auth` and "
                    "`legendary list --force-refresh`"
                ) from exc
            raise
        payload = response.json()

    if not isinstance(payload, dict) or "access_token" not in payload:
        raise RuntimeError("Epic token refresh returned no access token")
    return payload


async def _get_epic_session(force_refresh: bool = False) -> dict[str, Any]:
    user_data = await _load_epic_user_data()
    needs_refresh = force_refresh or _token_expiring_soon(user_data.get("expires_at"))

    if needs_refresh:
        refresh_token = user_data.get("refresh_token")
        if not refresh_token:
            if force_refresh:
                raise RuntimeError("Epic access token refresh required but no refresh_token was found")
            return user_data
        return await _refresh_epic_session(str(refresh_token))

    return user_data


async def fetch_epic_library() -> list[dict[str, Any]]:
    """Return owned Epic games from Legendary's cached metadata directory."""
    metadata_dir = _legendary_config_path() / "metadata"
    if not metadata_dir.is_dir():
        return []

    games: list[dict[str, Any]] = []
    for path in sorted(metadata_dir.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Skipping unreadable Epic metadata file %s: %s", path, exc)
            continue

        if isinstance(payload, dict):
            games.append(payload)

    return games


def _extract_epic_title(game: dict[str, Any]) -> str | None:
    title = game.get("title") or game.get("app_title")
    if title:
        return str(title)

    metadata = game.get("metadata")
    if isinstance(metadata, dict):
        metadata_title = metadata.get("title")
        if metadata_title:
            return str(metadata_title)

    app_name = game.get("app_name")
    return str(app_name) if app_name else None


def _extract_epic_artifact_id(game: dict[str, Any]) -> str | None:
    asset_infos = game.get("asset_infos")
    if isinstance(asset_infos, dict):
        preferred = asset_infos.get("Windows")
        candidate_assets = [preferred] if isinstance(preferred, dict) else []
        candidate_assets.extend(
            asset for key, asset in asset_infos.items() if key != "Windows" and isinstance(asset, dict)
        )
        for asset in candidate_assets:
            artifact_id = asset.get("asset_id") or asset.get("app_name")
            if artifact_id:
                return str(artifact_id)

    app_name = game.get("app_name")
    return str(app_name) if app_name else None


def _epic_metadata(game: dict[str, Any]) -> dict[str, Any]:
    metadata = game.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _is_unreal_engine_asset(game: dict[str, Any]) -> bool:
    """True for an Unreal Engine marketplace asset, which is not a game.

    Legendary's own rule: anything in the "ue" asset namespace is engine
    content (a plugin, a mesh pack, an AI system) sold through the UE
    marketplace and cached beside real games — production carried three
    versioned artifacts of one such asset as library titles.
    """
    asset_infos = game.get("asset_infos")
    if isinstance(asset_infos, dict):
        for asset in asset_infos.values():
            if (
                isinstance(asset, dict)
                and str(asset.get("namespace") or "").strip().lower() == _UE_ASSET_NAMESPACE
            ):
                return True
    return str(_epic_metadata(game).get("namespace") or "").strip().lower() == _UE_ASSET_NAMESPACE


def _is_mod(game: dict[str, Any]) -> bool:
    """True when the catalog item carries the "mods" category (also not a game)."""
    categories = _epic_metadata(game).get("categories")
    if not isinstance(categories, list):
        return False
    return any(
        isinstance(category, dict)
        and str(category.get("path") or "").strip().lower() == _MOD_CATEGORY_PATH
        for category in categories
    )


def _epic_main_game(game: dict[str, Any]) -> dict[str, Any] | None:
    """Return the base catalog item this item is DLC for, else None.

    ``metadata.mainGameItem`` IS Legendary's DLC marker: its presence means the
    item is an add-on, and the dict describes the base catalog item (always
    ``id``, usually ``title``/``namespace``, sometimes ``releaseInfo[{appId}]``).
    """
    main_game = _epic_metadata(game).get("mainGameItem")
    return main_game if isinstance(main_game, dict) else None


def _epic_catalog_item_ids(game: dict[str, Any]) -> list[str]:
    """Every catalog-item id this metadata file claims (``metadata.id`` first)."""
    ids: list[str] = []
    catalog_id = _epic_metadata(game).get("id")
    if catalog_id:
        ids.append(str(catalog_id))
    asset_infos = game.get("asset_infos")
    if isinstance(asset_infos, dict):
        for asset in asset_infos.values():
            if not isinstance(asset, dict):
                continue
            asset_catalog_id = asset.get("catalog_item_id")
            if asset_catalog_id and str(asset_catalog_id) not in ids:
                ids.append(str(asset_catalog_id))
    return ids


def _main_game_artifact_ids(main_game: dict[str, Any]) -> list[str]:
    """Artifact ids the base item names in ``releaseInfo[*].appId``."""
    release_info = main_game.get("releaseInfo")
    if not isinstance(release_info, list):
        return []
    artifact_ids: list[str] = []
    for release in release_info:
        if not isinstance(release, dict):
            continue
        app_id = release.get("appId")
        if app_id and str(app_id) not in artifact_ids:
            artifact_ids.append(str(app_id))
    return artifact_ids


def _is_parentless_nested(row: Any) -> bool:
    """True for a nested row (dlc/expansion/…) that has no parent link yet."""
    try:
        content_type = row["content_type"]
        parent_game_id = row["parent_game_id"]
    except (KeyError, IndexError, TypeError):
        return False
    return content_type in NESTED_CONTENT_TYPES and parent_game_id is None


def _is_default_classification(row: Any) -> bool:
    """True when a games row still carries the untouched base_game default."""
    try:
        content_type = row["content_type"]
        is_primary = row["is_primary_library_item"]
        parent_game_id = row["parent_game_id"]
    except (KeyError, IndexError, TypeError):
        return False
    return (
        (content_type or CONTENT_BASE_GAME) == CONTENT_BASE_GAME
        and bool(is_primary)
        and parent_game_id is None
    )


async def _apply_epic_catalog_title(game_id: int, prepared_title: str) -> bool:
    """Adopt Epic's catalog spelling when Epic is the row's only source of truth.

    Mirrors the Steam sync's rename, with one extra condition: name is the
    CROSS-platform reconciliation key, so a row that also owns another platform
    keeps the name that platform's sync agreed on. A ``name`` pinned in
    manual_overrides is left alone like every other user-set column.
    """
    async with get_db() as db:
        row = await db.execute_fetchone("SELECT name FROM games WHERE id = ?", (game_id,))
        if row is None or row["name"] == prepared_title:
            return False
        if "name" in await get_manual_overrides(db, game_id):
            return False
        other_platform = await db.execute_fetchone(
            "SELECT 1 FROM game_platforms WHERE game_id = ? AND platform != 'epic' LIMIT 1",
            (game_id,),
        )
        if other_platform is not None:
            return False
        await db.execute(
            "UPDATE games SET name = ?, name_normalized = ? WHERE id = ?",
            (prepared_title, normalize_search_text(prepared_title), game_id),
        )
        await db.commit()
        return True


async def _resolve_epic_parent(
    main_game: dict[str, Any], catalog_to_game: dict[str, int]
) -> int | None:
    """Find the base game an Epic DLC hangs off — never mints one.

    Order: this run's base-game catalog map, then any row already carrying one
    of the base item's own artifact ids, then an exact/normalized name match.
    None is an honest answer (ADR 0002 decision 6: parents are never minted
    from a title) and leaves a parentless nested row for manual repair.
    """
    main_id = main_game.get("id")
    if main_id is not None and str(main_id) in catalog_to_game:
        return catalog_to_game[str(main_id)]

    for artifact_id in _main_game_artifact_ids(main_game):
        parent = await get_game_by_identifier(EPIC_ARTIFACT_ID, artifact_id)
        if parent is not None:
            return int(parent["id"])

    main_title = main_game.get("title")
    if main_title:
        return await resolve_parent_game(str(main_title), create=False)
    return None


async def fetch_epic_playtime(
    suppress_configuration_errors: bool = True,
) -> tuple[dict[str, int], dict[str, str]]:
    """Return (playtime minutes, last-played dates) keyed by Epic artifact id.

    The last-played map is best-effort and usually sparser than the playtime
    map: Epic's playtime payload is undocumented, so the field is probed
    across EPIC_LAST_PLAYED_KEYS and simply omitted when absent. An artifact
    with playtime but no parseable date is present in the first map only.
    """
    try:
        session = await _get_epic_session()
    except EpicConfigurationError as exc:
        if not suppress_configuration_errors:
            raise
        logger.info("Epic playtime unavailable: %s", exc)
        return {}, {}
    except Exception as exc:
        logger.warning("Epic playtime unavailable: %s", exc)
        return {}, {}

    account_id = session.get("account_id")
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not account_id or not access_token:
        logger.info("Epic playtime unavailable: missing account_id or access_token")
        return {}, {}

    async with httpx.AsyncClient(
        timeout=_EPIC_TIMEOUT,
        headers={
            "Accept": "application/json",
            "Authorization": f"bearer {access_token}",
            "User-Agent": _EPIC_USER_AGENT,
        },
    ) as client:
        response = await client.get(_EPIC_PLAYTIME_URL.format(account_id=account_id))
        if response.status_code == 401 and refresh_token:
            try:
                session = await _refresh_epic_session(str(refresh_token))
            except EpicConfigurationError as exc:
                if not suppress_configuration_errors:
                    raise
                logger.info("Epic playtime unavailable: %s", exc)
                return {}, {}
            except Exception as exc:
                logger.warning("Epic playtime refresh failed after 401: %s", exc)
                return {}, {}
            response = await client.get(
                _EPIC_PLAYTIME_URL.format(account_id=session["account_id"]),
                headers={
                    "Accept": "application/json",
                    "Authorization": f"bearer {session['access_token']}",
                    "User-Agent": _EPIC_USER_AGENT,
                },
            )

        response.raise_for_status()
        payload = response.json()

    if not isinstance(payload, list):
        raise RuntimeError("unexpected Epic playtime payload")

    playtime: dict[str, int] = {}
    last_played: dict[str, str] = {}
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        artifact_id = entry.get("artifactId")
        total_time = entry.get("totalTime")
        if artifact_id is None or total_time is None:
            continue
        try:
            playtime[str(artifact_id)] = int(total_time) // 60
        except (TypeError, ValueError):
            logger.debug("Skipping Epic playtime row with invalid totalTime: %r", entry)
            continue
        seen_at = extract_last_played(entry, EPIC_LAST_PLAYED_KEYS)
        if seen_at is not None:
            last_played[str(artifact_id)] = seen_at

    return playtime, last_played


async def sync_epic() -> dict:
    """
    Sync Epic Games library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int, "dlc": int}
    """
    config_path = _legendary_config_path()
    if not config_path.exists():
        logger.info("Epic config path does not exist (%s) — skipping Epic sync", config_path)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "dlc": 0,
            "sync_status": "unconfigured",
            "error_summary": f"Epic config path does not exist: {config_path}",
            "error_classification": "missing_configuration",
        }

    try:
        games = await fetch_epic_library()
    except Exception as exc:
        logger.warning("Epic sync failed: %s", exc)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "dlc": 0,
            "sync_status": "failed",
            "error_summary": f"Epic sync failed: {exc}",
        }

    playtime_error: EpicConfigurationError | None = None
    try:
        playtime_by_artifact, last_played_by_artifact = await fetch_epic_playtime(
            suppress_configuration_errors=False
        )
    except EpicConfigurationError as exc:
        logger.info("Epic playtime stale; syncing ownership only: %s", exc)
        playtime_error = exc
        playtime_by_artifact = {}
        last_played_by_artifact = {}
    except Exception as exc:
        logger.warning("Epic playtime unavailable (non-fatal): %s", exc)
        playtime_by_artifact = {}
        last_played_by_artifact = {}

    if not games:
        logger.info("Epic metadata cache is empty at %s — skipping Epic sync", config_path / "metadata")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "dlc": 0,
            "sync_status": "unconfigured",
            "error_summary": f"Epic metadata cache is empty at {config_path / 'metadata'}",
            "error_classification": "missing_configuration",
        }

    added = matched = skipped = dlc_synced = 0
    candidates = await load_fuzzy_candidates()
    igdb_platform_id = PLATFORM_TO_IGDB.get("epic")

    # Legendary's metadata cache mixes three kinds of item. Partition first:
    # non-games are dropped here, and DLC is held back so every base game in
    # this run is already resolved when a DLC looks for its parent (the cache
    # directory has no ordering guarantee of its own).
    base_items: list[tuple[dict[str, Any], str, str, str | None]] = []
    dlc_items: list[tuple[dict[str, Any], str, str, str | None, dict[str, Any]]] = []
    for game in games:
        title = _extract_epic_title(game)
        if not title:
            skipped += 1
            continue
        prepared_title = prepare_catalog_title(title)
        if prepared_title is None:
            skipped += 1
            continue
        if _is_unreal_engine_asset(game):
            logger.debug("Skipping Epic Unreal Engine marketplace asset: %s", title)
            skipped += 1
            continue
        if _is_mod(game):
            logger.debug("Skipping Epic mod item: %s", title)
            skipped += 1
            continue

        artifact_id = _extract_epic_artifact_id(game)
        main_game = _epic_main_game(game)
        if main_game is None:
            base_items.append((game, title, prepared_title, artifact_id))
        else:
            dlc_items.append((game, title, prepared_title, artifact_id, main_game))

    # Pass 1 — base games. catalog_to_game is what a DLC's mainGameItem.id
    # resolves against in pass 2.
    catalog_to_game: dict[str, int] = {}
    for game, title, prepared_title, artifact_id in base_items:
        # Prefer the stable Epic artifact id: a re-sync matches the existing game
        # directly so name/fuzzy resolution (which now refuses to attach onto an
        # existing Epic-owning row) never re-creates it as a duplicate.
        existing = (
            await get_game_by_identifier(EPIC_ARTIFACT_ID, artifact_id) if artifact_id else None
        )
        # Identifier miss but a same-name Epic row exists without any
        # epic_artifact_id: adopt the identifier onto it instead of letting the
        # exclude_platform guard fork a stranded duplicate.
        adopted_game_id = (
            await adopt_platform_identifier(
                name=prepared_title,
                platform="epic",
                identifier_type=EPIC_ARTIFACT_ID,
                identifier_value=artifact_id,
            )
            if existing is None and artifact_id
            else None
        )
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            matched += 1
        elif adopted_game_id is not None:
            game_id = adopted_game_id
            igdb_game = None
            matched += 1
        else:
            game_id, igdb_game = await resolve_and_link_game(
                prepared_title, igdb_platform_id, candidates, platform="epic"
            )
            if game_id in candidates:
                matched += 1
            else:
                candidates[game_id] = prepared_title
                added += 1

        for catalog_id in _epic_catalog_item_ids(game):
            catalog_to_game.setdefault(catalog_id, game_id)

        await _apply_epic_catalog_title(game_id, prepared_title)

        if title != prepared_title:
            await upsert_game_alias(
                game_id,
                title,
                alias_type="edition",
                source="epic",
                source_key=artifact_id,
            )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="epic",
            # Epic reports raw playtime in seconds; normalize to canonical minutes.
            playtime_minutes=playtime_by_artifact.get(artifact_id) if artifact_id else None,
            last_played=last_played_by_artifact.get(artifact_id) if artifact_id else None,
            owned=1,
            from_source=True,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

        if artifact_id:
            await upsert_game_platform_identifier(platform_id, EPIC_ARTIFACT_ID, artifact_id)

    # Pass 2 — DLC. Nested items match by EXACT identity only (ADR 0002
    # decision 7): the artifact id, nothing else. No adopt, no IGDB, no fuzzy
    # name — two different games' "Ultra HD Texture Pack" are two rows, and
    # letting either of those resolvers see a generic add-on title is exactly
    # how they collapsed onto one in production.
    for game, title, prepared_title, artifact_id, main_game in dlc_items:
        dlc_synced += 1
        parent_game_id = await _resolve_epic_parent(main_game, catalog_to_game)
        main_title = main_game.get("title")
        existing = (
            await get_game_by_identifier(EPIC_ARTIFACT_ID, artifact_id) if artifact_id else None
        )

        if existing is not None:
            game_id = existing["id"]
            matched += 1
            # A row still at the untouched default is reclassified; so is a
            # row already nested but PARENTLESS once its base item resolves —
            # a giveaway DLC routinely arrives before its base game, and the
            # first sync after the base is claimed is when the link becomes
            # possible. Either way the shared writer's guards
            # (parent-must-stay-primary, substance, edition-ownership, manual
            # overrides) have the last word.
            if _is_default_classification(existing) or (
                parent_game_id is not None and _is_parentless_nested(existing)
            ):
                await apply_content_classification(
                    game_id,
                    ContentClassification(
                        content_type=CONTENT_DLC,
                        is_primary_library_item=False,
                        parent_name=str(main_title) if main_title else None,
                    ),
                    source="epic",
                    parent_game_id=parent_game_id,
                )
        else:
            # No parent resolved is the honest "DLC without its base game"
            # shape (Epic giveaways produce it routinely) — a parent is never
            # minted from a title guess.
            game_id = await upsert_game(
                appid=None,
                name=prepared_title,
                match_existing_by_name=False,
                content_type=CONTENT_DLC,
                parent_game_id=parent_game_id,
            )
            added += 1

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="epic",
            playtime_minutes=playtime_by_artifact.get(artifact_id) if artifact_id else None,
            last_played=last_played_by_artifact.get(artifact_id) if artifact_id else None,
            owned=1,
            from_source=True,
        )

        if artifact_id:
            await upsert_game_platform_identifier(platform_id, EPIC_ARTIFACT_ID, artifact_id)

    logger.info(
        "Epic sync: added=%d matched=%d skipped=%d dlc=%d playtime_rows=%d",
        added,
        matched,
        skipped,
        dlc_synced,
        len(playtime_by_artifact),
    )
    result: dict[str, object] = {
        "added": added,
        "matched": matched,
        "skipped": skipped,
        "dlc": dlc_synced,
    }
    if playtime_error is not None:
        result.update(
            {
                "sync_status": "stale",
                "error_summary": str(playtime_error),
                "error_classification": "auth_stale",
            }
        )
    return result
