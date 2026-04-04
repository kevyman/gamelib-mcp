"""Cached canonical Metacritic score access."""

from .db import get_db


async def get_metacritic(game_id: int) -> int | None:
    """Return the cached canonical Metacritic score for a game."""
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT metacritic_score FROM games WHERE id = ?",
            (game_id,),
        )
    return row["metacritic_score"] if row else None
