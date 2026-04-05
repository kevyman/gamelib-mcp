"""Epic Games Store library sync via Legendary's local cache.

Requires a readable Legendary config directory containing at least:
- ``user.json`` for Epic auth tokens
- ``metadata/*.json`` for owned game metadata

Set ``EPIC_LEGENDARY_PATH`` to override the config directory. In Docker, mount
that directory read-only into the container and point ``EPIC_LEGENDARY_PATH``
at the mount path.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from gamelib_mcp.data.db import (
    EPIC_ARTIFACT_ID,
    load_fuzzy_candidates,
    upsert_game_platform,
    upsert_game_platform_enrichment,
    upsert_game_platform_identifier,
)
from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB

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
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True

    return expiry <= datetime.now(timezone.utc) + _TOKEN_REFRESH_SKEW


async def _read_json_file(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


async def _load_epic_user_data() -> dict[str, Any]:
    user_path = _legendary_config_path() / "user.json"
    if not user_path.is_file():
        raise FileNotFoundError(f"missing Epic credentials file: {user_path}")

    data = await _read_json_file(user_path)
    if not isinstance(data, dict):
        raise RuntimeError(f"unexpected Epic credentials payload in {user_path}")
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
        response.raise_for_status()
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


async def fetch_epic_playtime() -> dict[str, int]:
    """Return a mapping of Epic artifact id to total playtime minutes."""
    try:
        session = await _get_epic_session()
    except Exception as exc:
        logger.warning("Epic playtime unavailable: %s", exc)
        return {}

    account_id = session.get("account_id")
    access_token = session.get("access_token")
    refresh_token = session.get("refresh_token")
    if not account_id or not access_token:
        logger.warning("Epic playtime unavailable: missing account_id or access_token")
        return {}

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
            except Exception as exc:
                logger.warning("Epic playtime refresh failed after 401: %s", exc)
                return {}
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

    return playtime


async def sync_epic() -> dict:
    """
    Sync Epic Games library into game_platforms.

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    config_path = _legendary_config_path()
    if not config_path.exists():
        logger.info("Epic config path does not exist (%s) — skipping Epic sync", config_path)
        return {"added": 0, "matched": 0, "skipped": 0}

    try:
        games, playtime_by_artifact = await asyncio.gather(fetch_epic_library(), fetch_epic_playtime())
    except Exception as exc:
        logger.warning("Epic sync failed: %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    if not games:
        logger.info("Epic metadata cache is empty at %s — skipping Epic sync", config_path / "metadata")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for game in games:
        title = _extract_epic_title(game)
        if not title:
            skipped += 1
            continue

        igdb_platform_id = PLATFORM_TO_IGDB.get("epic")
        game_id, igdb_game = await resolve_and_link_game(title, igdb_platform_id, candidates)
        if game_id in candidates:
            matched += 1
        else:
            candidates[game_id] = title
            added += 1

        artifact_id = _extract_epic_artifact_id(game)
        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="epic",
            # Epic reports raw playtime in seconds; normalize to canonical minutes.
            playtime_minutes=playtime_by_artifact.get(artifact_id) if artifact_id else None,
            owned=1,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

        if artifact_id:
            await upsert_game_platform_identifier(platform_id, EPIC_ARTIFACT_ID, artifact_id)

    logger.info(
        "Epic sync: added=%d matched=%d skipped=%d playtime_rows=%d",
        added,
        matched,
        skipped,
        len(playtime_by_artifact),
    )
    return {"added": added, "matched": matched, "skipped": skipped}
