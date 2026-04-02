"""GOG owned games sync via lgogdownloader CLI.

One-time local setup:
  1. Install lgogdownloader (apt install lgogdownloader)
  2. Run: lgogdownloader --login
  3. Mount ~/.config/lgogdownloader/ into Docker (see deploy.md)

Playtime is not available from lgogdownloader output.
"""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from steam_mcp.data.db import (
    GOG_PRODUCT_ID,
    find_game_by_name_fuzzy,
    load_fuzzy_candidates,
    upsert_game,
    upsert_game_platform,
    upsert_game_platform_identifier,
)

logger = logging.getLogger(__name__)

_LGOGDOWNLOADER_BIN = "lgogdownloader"


def _config_dir() -> Path:
    """Return the lgogdownloader config directory (where auth session is stored)."""
    override = os.getenv("LGOGDOWNLOADER_CONFIG_PATH")
    if override:
        return Path(override)
    return Path.home() / ".config" / "lgogdownloader"


def _subprocess_env() -> dict:
    """
    Build env dict for lgogdownloader subprocess.

    lgogdownloader stores its session in XDG_CONFIG_HOME/lgogdownloader/.
    We set XDG_CONFIG_HOME to the parent of _config_dir() so lgogdownloader
    finds its session at the expected path.
    """
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(_config_dir().parent)
    return env


def _parse_lgogdownloader_json(stdout: str) -> list[dict]:
    """
    Parse lgogdownloader --list j JSON output.

    Returns a list of dicts with keys:
      - title (str): human-readable game title
      - product_id (int | None): GOG product ID, or None if absent

    Top-level array only — DLCs are nested inside each game object and are skipped.
    """
    try:
        items = json.loads(stdout)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse lgogdownloader JSON output: %s", exc)
        return []

    if not isinstance(items, list):
        logger.warning("Unexpected lgogdownloader JSON structure (expected list, got %s)", type(items).__name__)
        return []

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not title:
            continue
        product_id = item.get("product_id")
        results.append({"title": str(title), "product_id": int(product_id) if product_id is not None else None})
    return results


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms via lgogdownloader --list j.

    Silent skip conditions:
    - lgogdownloader binary not in PATH
    - lgogdownloader config dir does not exist (no session stored)

    Returns: {"added": int, "matched": int, "skipped": int}
    """
    if not shutil.which(_LGOGDOWNLOADER_BIN):
        logger.info("lgogdownloader not in PATH — skipping GOG sync")
        return {"added": 0, "matched": 0, "skipped": 0}

    config_path = _config_dir()
    if not config_path.exists():
        logger.info(
            "lgogdownloader config dir not found (%s) — skipping GOG sync", config_path
        )
        return {"added": 0, "matched": 0, "skipped": 0}

    try:
        proc = await asyncio.create_subprocess_exec(
            _LGOGDOWNLOADER_BIN,
            "--list", "j",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(),
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
    except Exception as exc:
        logger.warning("GOG sync failed (subprocess error): %s", exc)
        return {"added": 0, "matched": 0, "skipped": 0}

    if proc.returncode != 0:
        logger.warning(
            "lgogdownloader --list j failed (rc=%d): %s",
            proc.returncode,
            stderr_bytes.decode()[:300],
        )
        return {"added": 0, "matched": 0, "skipped": 0}

    games = _parse_lgogdownloader_json(stdout_bytes.decode())
    if not games:
        logger.info("GOG sync: no games found in lgogdownloader output")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for game in games:
        title = game["title"]
        existing = await find_game_by_name_fuzzy(title, candidates=candidates)
        if existing:
            game_id = existing["id"]
            matched += 1
        else:
            game_id = await upsert_game(appid=None, name=title)
            candidates[game_id] = title
            added += 1

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="gog",
            playtime_minutes=None,
            owned=1,
        )

        if game["product_id"] is not None:
            await upsert_game_platform_identifier(platform_id, GOG_PRODUCT_ID, game["product_id"])

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
