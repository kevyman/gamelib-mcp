"""Tag-affinity recomputation from rated games (drives recommendations)."""

import json
import math
from datetime import datetime, timezone

from . import get_db


async def recompute_tag_affinity() -> int:
    """
    Recompute tag_affinity from all rated games.

    affinity_score = weighted_avg_score x log(game_count + 1)

    Backloggd weight = 1.0, Steam review weight = 0.5.
    Returns number of tags updated.
    """
    source_weights = {"backloggd": 1.0, "steam_review": 0.5}

    async with get_db() as db:
        rows = await db.execute_fetchall(
            """
            SELECT r.game_id, r.source, r.normalized_score, g.tags
            FROM ratings r
            JOIN games g ON g.id = r.game_id
            WHERE g.tags IS NOT NULL AND r.normalized_score IS NOT NULL
            """
        )

    tag_data: dict[str, dict] = {}

    for row in rows:
        try:
            tags = json.loads(row["tags"])
        except (ValueError, TypeError):
            continue

        weight = source_weights.get(row["source"], 0.5)
        score = row["normalized_score"]
        game_id = row["game_id"]

        for tag in tags:
            tag_lower = tag.lower()
            if tag_lower not in tag_data:
                tag_data[tag_lower] = {
                    "weighted_sum": 0.0,
                    "weight_sum": 0.0,
                    "game_ids": set(),
                }
            tag_data[tag_lower]["weighted_sum"] += score * weight
            tag_data[tag_lower]["weight_sum"] += weight
            tag_data[tag_lower]["game_ids"].add(game_id)

    now = datetime.now(timezone.utc).isoformat()

    async with get_db() as db:
        await db.execute("DELETE FROM tag_affinity")
        for tag, data in tag_data.items():
            if data["weight_sum"] == 0:
                continue
            avg_score = data["weighted_sum"] / data["weight_sum"]
            game_count = len(data["game_ids"])
            affinity_score = avg_score * math.log(game_count + 1)
            await db.execute(
                """INSERT INTO tag_affinity (tag, affinity_score, avg_score, game_count, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (tag, affinity_score, avg_score, game_count, now),
            )
        await db.commit()

    return len(tag_data)
