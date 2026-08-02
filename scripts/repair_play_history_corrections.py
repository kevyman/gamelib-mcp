#!/usr/bin/env python3
"""Remove stored-total corrections from play_history so they stop reading as play.

play_history rows are cumulative totals, and get_play_history derives a window's
minutes by subtracting two of them. That makes any correction to a stored total
indistinguishable from a play session: when the PSN cross-gen SKU fix started
SUMMING a game's PS4 and PS5 entries instead of letting the last one overwrite
the others, seven totals stepped up at once and Ghost of Tsushima's history
recorded 4887 minutes of "play" on the day of the re-sync.

get_play_history now suppresses those at read time (the last_played gate in
tools/history.py), but the stored series still carries the step, so anything
reading play_history directly still sees it. This script repairs the data.

THE RULE. A correction is growth recorded entirely AFTER the last day the
platform says you played:

    snapshot[n].playtime_minutes > snapshot[n-1].playtime_minutes
    AND game_platforms.last_played < snapshot[n-1].snapshot_date

Growth after your last session cannot be play. Nothing else is touched — a game
with no last_played (Steam before its backfill, GOG ever) is skipped rather than
guessed at, and a decrease is left alone.

THE REPAIR. The correction is a level shift: the total was understated by the
same amount for the whole prior series, so that amount is added to every
snapshot at or before the correction's baseline date. This removes the artificial
step while preserving the SHAPE of real growth before it — which zeroing or
truncating the series would destroy.

    before:  2026-07-04:   46    2026-08-02: 4933     (delta 4887 "played")
    after:   2026-07-04: 4933    2026-08-02: 4933     (delta 0)

Totals are never lowered and the latest snapshot is never modified, so the
current cumulative total always still matches game_platforms.

USAGE. Dry-run by default — it prints what it would change and writes nothing:

    python scripts/repair_play_history_corrections.py
    python scripts/repair_play_history_corrections.py --apply
    python scripts/repair_play_history_corrections.py --platform ps5 --apply

Set DATABASE_URL (or run from the deployment) so it opens the right database.
Safe to re-run: a repaired series no longer matches the rule.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelib_mcp.data.db import get_db

# Pairs of consecutive snapshots whose growth was recorded after the platform's
# own last-played date. LAG() would be tidier but SQLite's window functions are
# only guaranteed from 3.25; the correlated subquery works everywhere.
_CORRECTIONS_SQL = """
SELECT ph.game_id            AS game_id,
       ph.platform           AS platform,
       g.name                AS name,
       gp.last_played        AS last_played,
       prev.snapshot_date    AS baseline_date,
       prev.playtime_minutes AS baseline_total,
       ph.snapshot_date      AS correction_date,
       ph.playtime_minutes   AS corrected_total
FROM play_history ph
JOIN games g ON g.id = ph.game_id
JOIN game_platforms gp
  ON gp.game_id = ph.game_id AND gp.platform = ph.platform
JOIN play_history prev
  ON prev.game_id = ph.game_id
 AND prev.platform = ph.platform
 AND prev.snapshot_date = (
     SELECT MAX(p2.snapshot_date) FROM play_history p2
     WHERE p2.game_id = ph.game_id AND p2.platform = ph.platform
       AND p2.snapshot_date < ph.snapshot_date
 )
WHERE gp.last_played IS NOT NULL
  AND ph.playtime_minutes > prev.playtime_minutes
  AND gp.last_played < prev.snapshot_date
  {platform_clause}
ORDER BY (ph.playtime_minutes - prev.playtime_minutes) DESC
"""

_LEVEL_SHIFT_SQL = """
UPDATE play_history
SET playtime_minutes = playtime_minutes + :shift
WHERE game_id = :game_id
  AND platform = :platform
  AND snapshot_date <= :baseline_date
"""


async def find_corrections(platform: str | None) -> list[dict]:
    clause = "AND ph.platform = :platform" if platform else ""
    params = {"platform": platform} if platform else {}
    async with get_db() as db:
        rows = await db.execute_fetchall(
            _CORRECTIONS_SQL.format(platform_clause=clause), params
        )
    return [dict(row) for row in rows]


async def apply_corrections(corrections: list[dict]) -> int:
    shifted = 0
    async with get_db() as db:
        for c in corrections:
            await db.execute(
                _LEVEL_SHIFT_SQL,
                {
                    "shift": c["corrected_total"] - c["baseline_total"],
                    "game_id": c["game_id"],
                    "platform": c["platform"],
                    "baseline_date": c["baseline_date"],
                },
            )
            shifted += 1
        await db.commit()
    return shifted


def _report(corrections: list[dict]) -> None:
    if not corrections:
        print("No stored-total corrections found in play_history.")
        return

    width = max(len(c["name"]) for c in corrections)
    total = 0
    print(f"{'game':<{width}}  {'platform':<8}  {'last played':<11}  "
          f"{'baseline':>10}  {'corrected':>10}  {'phantom':>9}")
    print("-" * (width + 56))
    for c in corrections:
        shift = c["corrected_total"] - c["baseline_total"]
        total += shift
        print(
            f"{c['name']:<{width}}  {c['platform']:<8}  {c['last_played']:<11}  "
            f"{c['baseline_total']:>10}  {c['corrected_total']:>10}  {shift:>9}"
        )
    print("-" * (width + 56))
    print(
        f"{len(corrections)} correction(s); {total} phantom minutes "
        f"({round(total / 60, 1)} hours) currently read as play."
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--apply", action="store_true",
        help="write the repair (default is a dry run that changes nothing)",
    )
    parser.add_argument(
        "--platform", default=None,
        help="restrict to one platform (e.g. ps5); default is every platform",
    )
    args = parser.parse_args()

    if not os.getenv("DATABASE_URL"):
        print("DATABASE_URL is not set — using the default data/gamelib.db.")

    corrections = await find_corrections(args.platform)
    _report(corrections)

    if not corrections:
        return 0
    if not args.apply:
        print("\nDry run — nothing written. Re-run with --apply to repair.")
        return 0

    shifted = await apply_corrections(corrections)
    print(f"\nRepaired {shifted} series. Re-run without --apply to confirm it is clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
