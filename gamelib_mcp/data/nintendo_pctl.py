"""Nintendo Switch playtime via the Parental Controls (znma) API.

Unlike the Coral/NSO ``play-activity`` path — which needs an ``f`` attestation the
public providers no longer serve — the Parental Controls API is plain Nintendo
Account OAuth (no ``f``). It reports per-game playtime for any console registered to
Parental Controls, **ownership-agnostic**: games played on this console under another
account (e.g. a spouse's purchase) appear too.

This module is the **playtime** layer for the ``switch2`` platform; VGCS ownership
sync (``data/nintendo.py``) provides ownership. Playtime is forward-only — Parental
Controls only tracks from console registration onward, with no retroactive history.

Per-game minutes come from **finalized** daily summaries (``result != 'CALCULATING'``);
each finalized day is stored idempotently in ``nintendo_play_summary`` and the switch2
playtime total is ``SUM`` of those days, so re-syncing never double-counts.

Auth: a Nintendo Account session token (Parental Controls OAuth client) stored in
``NINTENDO_PCTL_SESSION_FILE`` (default ``data/nintendo_pctl_session.json``), populated
via the ``set_nintendo_pctl_session`` MCP tool.
"""

import json
import logging
import os

from gamelib_mcp.data.db import (
    adopt_platform_identifier,
    get_game_by_identifier,
    get_nintendo_play_totals,
    load_fuzzy_candidates,
    upsert_game_platform,
    upsert_game_platform_identifier,
    upsert_nintendo_play_summary,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB_ANY, resolve_and_link_game
from gamelib_mcp.data.title_normalization import prepare_catalog_title

logger = logging.getLogger(__name__)

PLATFORM = "switch2"
NINTENDO_TITLE_ID = "nintendo_title_id"
# Parental Controls (Nintendo Switch app, "znma") OAuth client.
PCTL_CLIENT_ID = "54789befb391a838"
_DEFAULT_TOKEN_FILE = "data/nintendo_pctl_session.json"


def _token_file_path() -> str:
    return os.getenv("NINTENDO_PCTL_SESSION_FILE", _DEFAULT_TOKEN_FILE)


def _load_pctl_session_token() -> str | None:
    """Read the stored Parental Controls session token (mirrors _load_vgcs_cookies)."""
    path = _token_file_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.warning("Failed to load Nintendo PCTL session token from %s: %s", path, exc)
        return None

    if isinstance(data, dict):
        return data.get("session_token") or None
    if isinstance(data, str):
        return data or None
    return None


def is_pctl_configured() -> bool:
    return _load_pctl_session_token() is not None


def _classify_pctl_error(message: str) -> str:
    lowered = message.lower()
    if any(
        token in lowered
        for token in ("auth", "expired", "session", "token", "login", "401", "invalidsession")
    ):
        return "auth_stale"
    return "unexpected"


def _extract_rows(device_id: str, summaries: list) -> list[dict]:
    """Flatten one device's daily summaries into play-summary rows.

    Per the live API, each day's ``players[].playedGames[]`` entry nests the game
    identity under ``meta`` (``applicationId``, ``title``) with ``playingTime`` at
    the entry top level. Minutes are summed across player profiles for a given
    (app, day). The current day is included even while ``result == 'CALCULATING'``:
    its counters are already populated and refine through the day, and the natural
    PK makes each re-sync an idempotent overwrite rather than a double-count.
    """
    # (application_id, date) -> {"minutes": int, "name": str | None}
    agg: dict[tuple[str, str], dict] = {}
    for day in summaries or []:
        period_key = day.get("date")
        if not period_key:
            continue
        for player in day.get("players") or []:
            for game in player.get("playedGames") or []:
                meta = game.get("meta") or {}
                app_id = (
                    meta.get("applicationId")
                    or game.get("applicationId")
                    or game.get("titleId")
                )
                minutes = game.get("playingTime")
                if minutes is None:
                    minutes = game.get("playTime")
                if not app_id or minutes is None:
                    continue
                name = meta.get("title") or game.get("title") or game.get("name")
                slot = agg.setdefault((str(app_id), period_key), {"minutes": 0, "name": None})
                slot["minutes"] += int(minutes)
                if name and not slot["name"]:
                    slot["name"] = str(name)
    return [
        {
            "device_id": device_id,
            "application_id": app_id,
            "period_type": "day",
            "period_key": date,
            "playtime_minutes": slot["minutes"],
            "app_name": slot["name"],
        }
        for (app_id, date), slot in agg.items()
    ]


async def fetch_pctl_play_summaries() -> list[dict]:
    """Fetch per-game daily playtime from the Parental Controls API.

    Returns row dicts: ``device_id``, ``application_id``, ``period_type='day'``,
    ``period_key`` (``YYYY-MM-DD``), ``playtime_minutes``, ``app_name`` — see
    ``_extract_rows`` for the parsing contract.
    """
    import aiohttp
    from pynintendoparental import NintendoParental
    from pynintendoparental.authenticator import Authenticator

    token = _load_pctl_session_token()
    if not token:
        raise RuntimeError("No Nintendo Parental Controls session token configured")

    rows: list[dict] = []
    async with aiohttp.ClientSession() as session:
        auth = Authenticator(session_token=token, client_session=session)
        await auth.async_complete_login(use_session_token=True)
        nintendo = await NintendoParental.create(auth)

        for device in nintendo.devices.values():
            device_id = str(device.device_id)
            try:
                resp = await nintendo._api.async_get_device_daily_summaries(device_id)
            except Exception as exc:
                logger.warning("PCTL daily summaries failed for device %s: %s", device_id, exc)
                continue
            data = resp.get("json", resp) if isinstance(resp, dict) else resp
            summaries = (data or {}).get("dailySummaries", []) if isinstance(data, dict) else []
            rows.extend(_extract_rows(device_id, summaries))

    return rows


async def sync_nintendo_pctl() -> dict:
    """Sync Switch playtime from Parental Controls into switch2 game_platforms rows.

    Stores finalized daily summaries (idempotent), recomputes each title's running
    total, then for every played title: updates the matching switch2 game's playtime,
    or — for titles not already in the library (e.g. played on another account) —
    creates the game and links it as a normal owned switch2 title.

    Returns ``{added, matched, titles, sync_status?}``.
    """
    if not is_pctl_configured():
        return {
            "added": 0,
            "matched": 0,
            "titles": 0,
            "sync_status": "unconfigured",
            "error_summary": (
                "Parental Controls not configured: set a session token via "
                "set_nintendo_pctl_session"
            ),
            "error_classification": "missing_configuration",
        }

    try:
        rows = await fetch_pctl_play_summaries()
    except Exception as exc:
        classification = _classify_pctl_error(str(exc))
        logger.warning("Parental Controls playtime fetch failed: %s", exc)
        return {
            "added": 0,
            "matched": 0,
            "titles": 0,
            "sync_status": "stale" if classification == "auth_stale" else "failed",
            "error_summary": f"Parental Controls fetch failed: {exc}",
            "error_classification": classification,
        }

    await upsert_nintendo_play_summary(rows)

    totals = await get_nintendo_play_totals("day")
    igdb_platform_id = PLATFORM_TO_IGDB_ANY.get(PLATFORM)
    candidates = await load_fuzzy_candidates()

    added = matched = 0
    for application_id, info in totals.items():
        total_minutes = info["minutes"]
        minutes_2weeks = info["minutes_2weeks"]
        last_played = info["last_played"]

        existing = await get_game_by_identifier(NINTENDO_TITLE_ID, application_id)
        if existing is not None:
            await upsert_game_platform(
                game_id=existing["id"],
                platform=PLATFORM,
                playtime_minutes=total_minutes,
                playtime_2weeks_minutes=minutes_2weeks,
                last_played=last_played,
                owned=1,
            )
            matched += 1
            continue

        # Not in the library yet (typically a game owned on another account but
        # played on this console). Create it and link as a normal owned switch2 title.
        name = prepare_catalog_title(info["app_name"] or application_id)
        if not name:
            continue
        # Identifier miss but a same-name switch2 row exists without any
        # nintendo_title_id (e.g. ingested by the ownership sync before this
        # title id was seen): adopt the identifier onto it instead of letting
        # the exclude_platform guard fork a stranded duplicate.
        adopted_game_id = await adopt_platform_identifier(
            name=name,
            platform=PLATFORM,
            identifier_type=NINTENDO_TITLE_ID,
            identifier_value=application_id,
        )
        if adopted_game_id is not None:
            await upsert_game_platform(
                game_id=adopted_game_id,
                platform=PLATFORM,
                playtime_minutes=total_minutes,
                playtime_2weeks_minutes=minutes_2weeks,
                last_played=last_played,
                owned=1,
            )
            matched += 1
            continue
        game_id, _igdb_game = await resolve_and_link_game(
            name, igdb_platform_id, candidates, platform=PLATFORM
        )
        candidates.setdefault(game_id, name)
        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform=PLATFORM,
            playtime_minutes=total_minutes,
            playtime_2weeks_minutes=minutes_2weeks,
            last_played=last_played,
            owned=1,
        )
        await upsert_game_platform_identifier(platform_id, NINTENDO_TITLE_ID, application_id)
        added += 1

    logger.info(
        "Parental Controls playtime sync: added=%d matched=%d titles=%d",
        added, matched, len(totals),
    )
    return {"added": added, "matched": matched, "titles": len(totals)}
