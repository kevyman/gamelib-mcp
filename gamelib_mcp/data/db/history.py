"""play_history writes: cumulative snapshots deduped against the latest row."""

from datetime import UTC, datetime

from . import get_db


async def record_play_history_snapshots(
    platform: str, snapshot_date: str | None = None
) -> int:
    """Snapshot current game_platforms playtimes for one platform.

    Inserts (or same-day-updates) a row per owned game whose current
    playtime_minutes differs from its most recent snapshot. Cheap enough to
    run after every sync: unchanged games match the NOT-different guard and
    produce no writes.

    Each snapshot also records the platform's ``last_played`` AS OF that
    snapshot. It is copied rather than read live at query time because
    game_platforms.last_played moves: tools/history.py uses it to tell a real
    session from a correction to the stored total, and reading the mutable
    column would make a past window's answer change the next time the game is
    launched (see the v36 migration). A snapshot is an immutable observation.
    """
    day = snapshot_date or datetime.now(UTC).date().isoformat()
    async with get_db() as db:
        cursor = await db.execute(
            """
            INSERT INTO play_history
                (game_id, platform, snapshot_date, playtime_minutes, last_played)
            SELECT gp.game_id, gp.platform, ?, gp.playtime_minutes, gp.last_played
            FROM game_platforms gp
            WHERE gp.platform = ?
              AND gp.owned = 1
              AND gp.playtime_minutes IS NOT NULL
              AND gp.playtime_minutes IS NOT (
                  SELECT ph.playtime_minutes FROM play_history ph
                  WHERE ph.game_id = gp.game_id AND ph.platform = gp.platform
                  ORDER BY ph.snapshot_date DESC LIMIT 1
              )
            ON CONFLICT(game_id, platform, snapshot_date)
                DO UPDATE SET playtime_minutes = excluded.playtime_minutes,
                              last_played = excluded.last_played
            """,
            (day, platform),
        )
        await db.commit()
        return cursor.rowcount
