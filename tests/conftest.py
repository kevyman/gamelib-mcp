"""Shared harness + seed helpers for tool characterization tests.

No pytest-asyncio is installed, so these tests follow the repo's existing
pattern (``unittest.IsolatedAsyncioTestCase`` over a real temp SQLite DB with
HTTP mocked). ``ToolDBTestCase`` points ``DATABASE_URL`` at a throwaway file and
runs the real migrations via ``init_db``; the ``seed_*`` helpers below write rows
through the production upsert/SQL paths so tests exercise real schema behavior.

pytest's default ``prepend`` import mode puts ``tests/`` on ``sys.path`` (no
``__init__.py`` here), so other test modules can ``from conftest import ...``.
"""

import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from gamelib_mcp.data import db as db_module
from gamelib_mcp.data.title_normalization import normalize_search_text


class ToolDBTestCase(unittest.IsolatedAsyncioTestCase):
    """Base case giving each test an isolated, migrated SQLite database."""

    async def asyncSetUp(self) -> None:
        self._tmpdir = TemporaryDirectory()
        self._db_path = Path(self._tmpdir.name) / "tools.sqlite"
        self._prev_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"file:{self._db_path}"
        db_module._DB_READY_PATH = None
        await db_module.init_db()

    async def asyncTearDown(self) -> None:
        db_module._DB_READY_PATH = None
        if self._prev_database_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._prev_database_url
        self._tmpdir.cleanup()


# --- seed helpers (write through production paths) ---------------------------

async def seed_game(
    name: str,
    *,
    tags: list[str] | None = None,
    genres: list[str] | None = None,
    hltb_main: float | None = None,
    hltb_extra: float | None = None,
    hltb_complete: float | None = None,
    is_farmed: int = 0,
    release_date: str | None = None,
    short_description: str | None = None,
    content_type: str | None = None,
    parent_game_id: int | None = None,
    is_primary_library_item: int | None = None,
) -> int:
    """Create a canonical games row and return its id."""
    fields: dict = {"is_farmed": is_farmed}
    if tags is not None:
        fields["tags"] = json.dumps(tags)
    if genres is not None:
        fields["genres"] = json.dumps(genres)
    if hltb_main is not None:
        fields["hltb_main"] = hltb_main
    if hltb_extra is not None:
        fields["hltb_extra"] = hltb_extra
    if hltb_complete is not None:
        fields["hltb_complete"] = hltb_complete
    if release_date is not None:
        fields["release_date"] = release_date
    if short_description is not None:
        fields["short_description"] = short_description
    game_id = await db_module.upsert_game(None, name, **fields)
    related_updates = {
        "content_type": content_type,
        "parent_game_id": parent_game_id,
        "is_primary_library_item": is_primary_library_item,
    }
    related_updates = {key: value for key, value in related_updates.items() if value is not None}
    if related_updates:
        cols_sql = ", ".join(f"{column} = ?" for column in related_updates)
        async with db_module.get_db() as db:
            await db.execute(
                f"UPDATE games SET {cols_sql} WHERE id = ?",
                (*related_updates.values(), game_id),
            )
            await db.commit()
    return game_id


async def add_game_alias(
    game_id: int,
    alias: str,
    *,
    alias_type: str = "edition",
    source: str | None = None,
    source_key: str | None = None,
) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO game_aliases
               (game_id, alias, alias_normalized, alias_type, source, source_key)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, alias, normalize_search_text(alias), alias_type, source, source_key),
        )
        await db.commit()


async def add_platform(
    game_id: int,
    platform: str,
    *,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    owned: int = 1,
) -> int:
    """Attach a platform to a game and return the game_platform id."""
    return await db_module.upsert_game_platform(
        game_id,
        platform,
        playtime_minutes=playtime_minutes,
        playtime_2weeks_minutes=playtime_2weeks_minutes,
        owned=owned,
    )


async def add_identifier(
    game_platform_id: int,
    identifier_type: str,
    identifier_value: str | int,
    *,
    is_primary: bool = True,
) -> None:
    await db_module.upsert_game_platform_identifier(
        game_platform_id, identifier_type, identifier_value, is_primary=is_primary
    )


async def add_steam_appid(game_platform_id: int, appid: int) -> None:
    await add_identifier(game_platform_id, db_module.STEAM_APP_ID, appid)


async def add_steam_data(game_platform_id: int, **fields) -> None:
    await db_module.upsert_steam_platform_data(game_platform_id, **fields)


async def add_enrichment(game_platform_id: int, **fields) -> None:
    await db_module.upsert_game_platform_enrichment(game_platform_id, **fields)


async def add_rating(
    game_id: int,
    source: str,
    raw_score: float,
    normalized_score: float,
    review_text: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO ratings
               (game_id, source, raw_score, normalized_score, review_text, synced_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (game_id, source, raw_score, normalized_score, review_text, now),
        )
        await db.commit()


async def set_tag_affinity(
    tag: str,
    affinity_score: float,
    avg_score: float,
    game_count: int,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with db_module.get_db() as db:
        await db.execute(
            """INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (tag, affinity_score, avg_score, game_count, now),
        )
        await db.commit()


async def make_steam_game(
    name: str,
    appid: int,
    *,
    playtime_minutes: int | None = None,
    playtime_2weeks_minutes: int | None = None,
    tags: list[str] | None = None,
    genres: list[str] | None = None,
    hltb_main: float | None = None,
    is_farmed: int = 0,
    metacritic_score: int | None = None,
    opencritic_score: int | None = None,
    protondb_tier: str | None = None,
    steam_review_desc: str | None = None,
    steam_review_score: int | None = None,
    rtime_last_played: int | None = None,
) -> int:
    """Convenience: a Steam-owned game with optional enrichment, returns game_id."""
    game_id = await seed_game(
        name,
        tags=tags,
        genres=genres,
        hltb_main=hltb_main,
        is_farmed=is_farmed,
    )
    gpid = await add_platform(
        game_id,
        "steam",
        playtime_minutes=playtime_minutes,
        playtime_2weeks_minutes=playtime_2weeks_minutes,
    )
    await add_steam_appid(gpid, appid)
    steam_fields: dict = {}
    if protondb_tier is not None:
        steam_fields["protondb_tier"] = protondb_tier
    if steam_review_desc is not None:
        steam_fields["steam_review_desc"] = steam_review_desc
    if steam_review_score is not None:
        steam_fields["steam_review_score"] = steam_review_score
    if rtime_last_played is not None:
        steam_fields["rtime_last_played"] = rtime_last_played
    if steam_fields:
        await add_steam_data(gpid, **steam_fields)
    enrichment_fields: dict = {}
    if metacritic_score is not None:
        enrichment_fields["metacritic_score"] = metacritic_score
    if opencritic_score is not None:
        enrichment_fields["opencritic_score"] = opencritic_score
    if enrichment_fields:
        await add_enrichment(gpid, **enrichment_fields)
    return game_id
