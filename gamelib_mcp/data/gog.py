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
    get_platform_game_by_normalized_name,
    load_fuzzy_candidates,
    upsert_game_alias,
    upsert_game_platform,
    upsert_game_platform_enrichment,
)
from gamelib_mcp.data.igdb import PLATFORM_TO_IGDB, resolve_and_link_game
from gamelib_mcp.data.title_normalization import prepare_catalog_title

_ROMAN_RE = re.compile(r"\b([IiVvXx]{2,})\b")
_ORDINAL_RE = re.compile(r"(\d+)(St|Nd|Rd|Th)\b")

logger = logging.getLogger(__name__)

_LGOGDOWNLOADER_BIN = "lgogdownloader"
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_UPDATE_INDICATOR = re.compile(r"\s+\[\d+\]$")
_AUTH_FILE_TOKENS = ("cookie", "token", "auth", "session", "galaxy")


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
    if slug.endswith("_game") and "_" in slug[:-5]:
        slug = slug[:-5]
    # GOG convention: trailing _the → "The " prefix (e.g. elder_scrolls_the → The Elder Scrolls)
    if slug.endswith("_the") and "_" in slug[:-4]:
        slug = "the_" + slug[:-4]
    title = slug.replace("_", " ").title()
    # Fix Roman numerals mangled by .title() (Ii→II, Iii→III, Iv→IV, Vi→VI …)
    title = _ROMAN_RE.sub(lambda m: m.group(0).upper(), title)
    # Fix ordinal suffixes (20Th→20th, 1St→1st, 2Nd→2nd, 3Rd→3rd)
    title = _ORDINAL_RE.sub(lambda m: m.group(1) + m.group(2).lower(), title)
    return title


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


def _has_auth_files(config_path: Path) -> bool:
    if not config_path.is_dir():
        return False
    return any(
        path.is_file() and any(token in path.name.lower() for token in _AUTH_FILE_TOKENS)
        for path in config_path.iterdir()
    )


async def sync_gog() -> dict:
    """
    Sync GOG library into game_platforms via lgogdownloader --list.

    Silent skip conditions:
    - lgogdownloader binary not in PATH
    - lgogdownloader config dir does not exist (no session stored)

    Returns: {"added": int, "matched": int, "skipped": int} plus sync failure
    metadata when the CLI cannot run.
    """
    if not shutil.which(_LGOGDOWNLOADER_BIN):
        logger.info("lgogdownloader not in PATH — skipping GOG sync")
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "degraded",
            "error_summary": "lgogdownloader not in PATH",
            "error_classification": "missing_runtime_dependency",
        }

    config_path = _config_dir()
    if not config_path.exists():
        logger.info(
            "lgogdownloader config dir not found (%s) — skipping GOG sync", config_path
        )
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": f"lgogdownloader config dir not found: {config_path}",
            "error_classification": "missing_configuration",
        }

    if not _has_auth_files(config_path):
        logger.info("lgogdownloader session files missing in %s — skipping GOG sync", config_path)
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "unconfigured",
            "error_summary": f"lgogdownloader session files missing in {config_path}; run lgogdownloader --login",
            "error_classification": "missing_configuration",
        }

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
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "failed",
            "error_summary": f"GOG sync failed: {exc}",
        }

    if proc.returncode != 0:
        stderr = stderr_bytes.decode(errors="replace")[:300]
        logger.warning(
            "lgogdownloader --list failed (rc=%d): %s",
            proc.returncode,
            stderr,
        )
        summary = f"lgogdownloader --list failed (rc={proc.returncode})"
        if stderr:
            summary = f"{summary}: {stderr}"
        return {
            "added": 0,
            "matched": 0,
            "skipped": 0,
            "sync_status": "failed",
            "error_summary": summary,
        }

    titles = _parse_lgogdownloader_output(stdout_bytes.decode())
    if not titles:
        logger.info("GOG sync: no games found in lgogdownloader output")
        return {"added": 0, "matched": 0, "skipped": 0}

    added = matched = skipped = 0
    candidates = await load_fuzzy_candidates()

    for title in titles:
        prepared_title = prepare_catalog_title(title)
        if prepared_title is None:
            skipped += 1
            continue
        igdb_platform_id = PLATFORM_TO_IGDB.get("gog")

        # GOG has no per-item store id, so the title IS the stable key: a
        # same-normalized-name row that already owns gog is this exact catalog
        # item re-syncing. Match it directly — re-running the title through
        # IGDB can land on a *different* same-named IGDB candidate whose
        # conflicting release year makes the fuzzy fallback refuse the
        # existing row and fork a duplicate (observed in prod: "Agony",
        # "Sigma Theory", "Under The Moon" pairs).
        existing = await get_platform_game_by_normalized_name(prepared_title, "gog")
        if existing is not None:
            game_id = existing["id"]
            igdb_game = None
            matched += 1
        else:
            game_id, igdb_game = await resolve_and_link_game(
                prepared_title, igdb_platform_id, candidates
            )
            if game_id in candidates:
                matched += 1
            else:
                candidates[game_id] = prepared_title
                added += 1

        if title != prepared_title:
            await upsert_game_alias(
                game_id,
                title,
                alias_type="edition",
                source="gog",
                source_key=None,
            )

        platform_id = await upsert_game_platform(
            game_id=game_id,
            platform="gog",
            playtime_minutes=None,
            owned=1,
            from_source=True,
        )

        if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
            await upsert_game_platform_enrichment(
                platform_id,
                platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
            )

    logger.info("GOG sync: added=%d matched=%d skipped=%d", added, matched, skipped)
    return {"added": added, "matched": matched, "skipped": skipped}
