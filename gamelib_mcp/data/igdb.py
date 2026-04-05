"""IGDB (Twitch) API client — game identity resolution with tags, genres, release dates."""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)

_TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_IGDB_GAMES_URL = "https://api.igdb.com/v4/games"

# IGDB platform IDs
IGDB_PLATFORM_PC = 6
IGDB_PLATFORM_PS5 = 167
IGDB_PLATFORM_PS4 = 48
IGDB_PLATFORM_SWITCH = 130  # Switch (Switch 2 not yet in IGDB)

# Our platform value → IGDB platform ID
PLATFORM_TO_IGDB: dict[str, int] = {
    "steam": IGDB_PLATFORM_PC,
    "epic": IGDB_PLATFORM_PC,
    "gog": IGDB_PLATFORM_PC,
    "ps5": IGDB_PLATFORM_PS5,
    "switch2": IGDB_PLATFORM_SWITCH,
}

# IGDB category values
CATEGORY_MAIN_GAME = 0
CATEGORY_DLC = 1
CATEGORY_EXPANSION = 2
CATEGORY_BUNDLE = 3
CATEGORY_STANDALONE_EXPANSION = 4
CATEGORY_MOD = 5
CATEGORY_EPISODE = 6
CATEGORY_SEASON = 7
CATEGORY_REMAKE = 8
CATEGORY_REMASTER = 9
CATEGORY_EXPANDED_GAME = 10
CATEGORY_PORT = 11

# Cached token
_token: str | None = None
_token_expires_at: datetime = datetime.min.replace(tzinfo=timezone.utc)


@dataclass
class IGDBGame:
    igdb_id: int
    name: str
    category: int
    first_release_date: str | None  # ISO date string YYYY-MM-DD
    genres: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)   # themes + keywords
    platform_release_dates: dict[int, str] = field(default_factory=dict)  # igdb_platform_id → ISO date


async def _get_token() -> str:
    """Return a valid Twitch OAuth2 access token, refreshing if needed."""
    global _token, _token_expires_at

    now = datetime.now(timezone.utc)
    if _token and now < _token_expires_at - timedelta(minutes=10):
        return _token

    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise EnvironmentError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set for IGDB enrichment")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            _TWITCH_TOKEN_URL,
            params={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    _token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    _token_expires_at = now + timedelta(seconds=expires_in)
    return _token


def _unix_to_iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except (OSError, OverflowError, ValueError):
        return None


async def search_game(name: str, igdb_platform_id: int | None = None) -> list[IGDBGame]:
    """
    Search IGDB for a game by name, optionally filtered to a platform.
    Returns up to 5 matches ranked by relevance.
    """
    client_id = os.environ.get("TWITCH_CLIENT_ID")
    if not client_id:
        return []

    platform_clause = f" & platforms = ({igdb_platform_id})" if igdb_platform_id else ""
    query = (
        f'fields id, name, category, first_release_date, '
        f'genres.name, themes.name, keywords.name, '
        f'release_dates.platform, release_dates.date; '
        f'search "{name}"; '
        f'where category != ({CATEGORY_DLC},{CATEGORY_BUNDLE},{CATEGORY_MOD},'
        f'{CATEGORY_EPISODE},{CATEGORY_SEASON}){platform_clause}; '
        f'limit 5;'
    )

    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _IGDB_GAMES_URL,
                content=query,
                headers={
                    "Client-ID": client_id,
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "text/plain",
                },
            )
            resp.raise_for_status()
            results = resp.json()
    except Exception as exc:
        logger.warning("IGDB search failed for %r: %s", name, exc)
        return []

    games = []
    for item in results:
        genres = [g["name"] for g in item.get("genres") or []]
        themes = [t["name"] for t in item.get("themes") or []]
        keywords = [k["name"] for k in item.get("keywords") or []]
        tags = list(dict.fromkeys(themes + keywords))[:30]  # deduplicate, cap at 30

        platform_dates: dict[int, str] = {}
        for rd in item.get("release_dates") or []:
            pid = rd.get("platform")
            date_ts = rd.get("date")
            if pid and date_ts:
                iso = _unix_to_iso(date_ts)
                if iso:
                    platform_dates[pid] = iso

        games.append(IGDBGame(
            igdb_id=item["id"],
            name=item["name"],
            category=item.get("category", CATEGORY_MAIN_GAME),
            first_release_date=_unix_to_iso(item.get("first_release_date")),
            genres=genres,
            tags=tags,
            platform_release_dates=platform_dates,
        ))

    return games


async def resolve_game(name: str, igdb_platform_id: int | None) -> IGDBGame | None:
    """
    Find the best IGDB match for a game name + platform. Returns None if not found
    or IGDB credentials are not configured.
    """
    if not os.environ.get("TWITCH_CLIENT_ID"):
        return None

    results = await search_game(name, igdb_platform_id)
    if not results:
        # Try without platform filter as fallback
        if igdb_platform_id is not None:
            results = await search_game(name, igdb_platform_id=None)

    if not results:
        return None

    # Pick best name match
    from .db import extract_best_fuzzy_key
    choices = {i: g.name for i, g in enumerate(results)}
    best_idx = extract_best_fuzzy_key(name, choices, cutoff=70)
    if best_idx is None:
        best_idx = 0  # take top result if fuzzy fails (IGDB ranked by relevance)

    return results[best_idx]


async def resolve_and_link_game(
    name: str,
    igdb_platform_id: int | None,
    candidates: dict[int, str],
) -> tuple[int, "IGDBGame | None"]:
    """
    Resolve a game to its canonical games row via IGDB, creating a new row if needed.
    Also writes tags, genres, release_date, and igdb_id from IGDB if the game row
    doesn't already have them.

    Returns (game_id, igdb_game) so callers can write platform_release_date
    to game_platform_enrichment after upsert_game_platform gives them a platform_id.
    igdb_game is None when IGDB is unconfigured or returns no result.

    Falls back to fuzzy name matching if IGDB is unconfigured or returns no result.
    """
    from .db import find_game_by_name_fuzzy, get_game_by_igdb_id, get_db

    igdb_game = await resolve_game(name, igdb_platform_id)

    if igdb_game is not None:
        existing = await get_game_by_igdb_id(igdb_game.igdb_id)
        if existing is not None:
            game_id = existing["id"]
        else:
            # On upgraded databases we may already have the title row without igdb_id.
            existing = await find_game_by_name_fuzzy(name, candidates=candidates)
            if existing is None and igdb_game.name.casefold() != name.casefold():
                existing = await find_game_by_name_fuzzy(igdb_game.name, candidates=candidates)

            if existing is not None:
                game_id = existing["id"]
            else:
                async with get_db() as db:
                    cursor = await db.execute("INSERT INTO games (name) VALUES (?)", (name,))
                    game_id = cursor.lastrowid
                    await db.commit()

        await _apply_igdb_metadata(game_id, igdb_game)
        return game_id, igdb_game

    # No IGDB result — fall back to fuzzy matching
    existing = await find_game_by_name_fuzzy(name, candidates=candidates)
    if existing:
        return existing["id"], None

    from .db import upsert_game
    return await upsert_game(appid=None, name=name), None


async def _apply_igdb_metadata(game_id: int, igdb_game: IGDBGame) -> None:
    """Write IGDB fields to games row, skipping columns that are already populated."""
    from .db import get_db

    now = datetime.now(timezone.utc).isoformat()
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT tags, genres, release_date FROM games WHERE id = ?", (game_id,)
        )
        if row is None:
            return

        updates: dict = {"igdb_id": igdb_game.igdb_id, "igdb_cached_at": now}
        if row["release_date"] is None and igdb_game.first_release_date:
            updates["release_date"] = igdb_game.first_release_date
        if row["genres"] is None and igdb_game.genres:
            updates["genres"] = json.dumps(igdb_game.genres)
        if row["tags"] is None and igdb_game.tags:
            updates["tags"] = json.dumps(igdb_game.tags)

        cols_sql = ", ".join(f"{col} = ?" for col in updates)
        await db.execute(
            f"UPDATE games SET {cols_sql} WHERE id = ?",
            (*updates.values(), game_id),
        )
        await db.commit()
