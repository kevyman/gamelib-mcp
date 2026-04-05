"""GOG owned games sync via lgogdownloader CLI.

One-time local setup:
  1. Install lgogdownloader (apt install lgogdownloader)
  2. Run: lgogdownloader --login
  3. Mount ~/.config/lgogdownloader/ into Docker (see deploy.md)

Playtime is not available from lgogdownloader output.

Note: lgogdownloader --list j (JSON mode) crashes on lgogdownloader 3.12, so we use
plain --list which outputs one slug per line with ANSI color codes and optional [N]
update indicators. Slugs are converted to title-cased strings for fuzzy matching
against existing game names.
"""

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

from gamelib_mcp.data.db import (
    load_fuzzy_candidates,
    upsert_game_platform,
    upsert_game_platform_enrichment,
)
from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB

logger = logging.getLogger(__name__)

_LGOGDOWNLOADER_BIN = "lgogdownloader"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_UPDATE_INDICATOR = re.compile(r"\s+\[\d+\]$")


def _config_dir() -> Path:
    """Return the lgogdownloader config directory (where auth session is stored)."""
    override = os.getenv("LGOGDOWNLOADER_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
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


def _slug_to_title(slug: str) -> str:
    """Convert a lgogdownloader slug to a human-readable title."""
    return slug.replace("_", " ").title()


def _parse_lgogdownloader_output(stdout: str) -> list[str]:
    """
    Parse lgogdownloader --list plain text output into a list of game titles.

    Each line is a slug with optional ANSI color codes and trailing [N] update
    indicator. Strips both, then title-cases for fuzzy matching.

    Example input line: "\x1b[01;34mcyberpunk_2077 [1]\x1b[0m"
    Example output: "Cyberpunk 2077"
    """
    titles = []
    for line in stdout.splitlines():
        line = _ANSI_ESCAPE.sub("", line).strip()
        if not line:
            continue
        line = _UPDATE_INDICATOR.sub("", line).strip()
        if not line:
            continue
        titles.append(_slug_to_title(line))
    return titles


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms via lgogdownloader --list.

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
            "--list",
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
            "lgogdownloader --list failed (rc=%d): %s",
            proc.returncode,
            stderr_bytes.decode()[:300],
        )
        return {"added": 0, "matched": 0, "skipped": 0}

    titles = _parse_lgogdownloader_output(stdout_bytes.decode())
    if not titles:
        logger.info("GOG sync: no games found in lgogdownloader output")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for title in titles:
        igdb_platform_id = PLATFORM_TO_IGDB.get("gog")
        game_id, igdb_game = await resolve_and_link_game(title, igdb_platform_id, candidates)
        if game_id in candidates:
            matched += 1
        else:
            candidates[game_id] = title
            added += 1

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="gog",
            playtime_minutes=None,
            owned=1,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
