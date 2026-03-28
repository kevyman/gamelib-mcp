"""One-shot migration: old single-table Steam schema → cross-platform schema.

Run with: python -m steam_mcp.migrate
"""

import asyncio
import os

import aiosqlite

from steam_mcp.data.db import _db_path, init_db


async def migrate() -> None:
    db_path = _db_path()
    print(f"Migrating database: {db_path}")

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=OFF")

        # Step 1: Check if migration already done
        tables = {
            row[0]
            for row in await db.execute_fetchall(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "game_platforms" in tables:
            print("game_platforms table already exists — migration already applied.")
            return

        if "games" not in tables:
            print("No games table found — running init_db() on empty database.")
            await init_db()
            return

        print("Step 1: Renaming existing tables to *_old …")
        await db.execute("ALTER TABLE games RENAME TO games_old")
        if "ratings" in tables:
            await db.execute("ALTER TABLE ratings RENAME TO ratings_old")
        if "tag_affinity" in tables:
            await db.execute("ALTER TABLE tag_affinity RENAME TO tag_affinity_old")
        await db.commit()

    # Step 2: Create fresh schema
    print("Step 2: Creating new schema …")
    await init_db()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=OFF")

        # Step 3: Migrate games rows
        print("Step 3: Migrating games …")
        old_games = await db.execute_fetchall("SELECT * FROM games_old")
        old_cols = {desc[0] for desc in (await db.execute("SELECT * FROM games_old LIMIT 0")).description}

        for row in old_games:
            # Build insert, preserving all columns that still exist in new schema
            keep = [
                "appid", "name", "genres", "tags", "short_description",
                "metacritic_score", "hltb_main", "hltb_extra",
                "protondb_tier", "steam_review_score", "steam_review_desc",
                "store_cached_at", "hltb_cached_at", "metacritic_cached_at",
                "protondb_cached_at", "steamspy_cached_at",
                "rtime_last_played", "is_farmed", "library_updated_at",
            ]
            present = [c for c in keep if c in old_cols and row[c] is not None]
            if not present:
                continue
            cols_sql = ", ".join(present)
            placeholders = ", ".join("?" for _ in present)
            values = [row[c] for c in present]

            # Map renamed column: hltb_completionist -> hltb_complete
            if "hltb_completionist" in old_cols and row["hltb_completionist"] is not None:
                cols_sql += ", hltb_complete"
                placeholders += ", ?"
                values.append(row["hltb_completionist"])

            await db.execute(
                f"INSERT OR IGNORE INTO games ({cols_sql}) VALUES ({placeholders})",
                values,
            )

        await db.commit()

        # Step 4: Create game_platforms rows for Steam
        print("Step 4: Creating game_platforms rows for Steam …")
        for row in old_games:
            game = await db.execute_fetchone(
                "SELECT id FROM games WHERE appid = ?", (row["appid"],)
            )
            if game is None:
                continue
            playtime = row["playtime_forever"] if "playtime_forever" in old_cols else None
            playtime_2weeks = row["playtime_2weeks"] if "playtime_2weeks" in old_cols else None
            await db.execute(
                """INSERT OR IGNORE INTO game_platforms
                   (game_id, platform, owned, playtime_minutes, playtime_2weeks_minutes, last_synced)
                   VALUES (?, 'steam', 1, ?, ?, datetime('now'))""",
                (game["id"], playtime, playtime_2weeks),
            )
        await db.commit()

        # Step 5: Migrate ratings
        if "ratings_old" in {row[0] for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}:
            print("Step 5: Migrating ratings …")
            old_ratings = await db.execute_fetchall("SELECT * FROM ratings_old")
            for row in old_ratings:
                game = await db.execute_fetchone(
                    "SELECT id FROM games WHERE appid = ?", (row["appid"],)
                )
                if game is None:
                    continue
                await db.execute(
                    """INSERT OR IGNORE INTO ratings
                       (game_id, source, raw_score, normalized_score, review_text, synced_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        game["id"],
                        row["source"],
                        row["raw_score"],
                        row["normalized_score"],
                        row["review_text"],
                        row["synced_at"],
                    ),
                )
            await db.commit()
        else:
            print("Step 5: No ratings_old table — skipping.")

        # Step 6: Migrate tag_affinity (no FK change needed)
        tables_now = {row[0] for row in await db.execute_fetchall(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        if "tag_affinity_old" in tables_now:
            print("Step 6: Migrating tag_affinity …")
            await db.execute("DELETE FROM tag_affinity")
            await db.execute(
                "INSERT INTO tag_affinity SELECT * FROM tag_affinity_old"
            )
            await db.commit()
        else:
            print("Step 6: No tag_affinity_old table — skipping.")

        # Step 7: Drop *_old tables
        print("Step 7: Dropping *_old tables …")
        for t in ("games_old", "ratings_old", "tag_affinity_old"):
            await db.execute(f"DROP TABLE IF EXISTS {t}")
        await db.commit()

    print("Migration complete.")


if __name__ == "__main__":
    asyncio.run(migrate())
