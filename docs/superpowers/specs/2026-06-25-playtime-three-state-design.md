# Three-state playtime: played / unplayed / unknown

**Date:** 2026-06-25
**Status:** Approved (design)

## Problem

Games with no playtime data are reported to the AI as confirmed never-played
backlog. The user noticed this for manually-added games, but the root cause is
broader.

`add_game_to_platform` (`tools/platforms.py:124`) stores manual games with
`playtime_minutes = NULL`. Every consumer then rolls playtime up as:

```sql
COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes
```

The inner `COALESCE(..., 0)` collapses *unknown* (NULL) into *literal zero*, and
the shared classification —

```sql
unplayed := total_playtime_minutes = 0 OR is_farmed = 1
played   := total_playtime_minutes > 0 AND is_farmed = 0
```

— then buckets those games as confirmed backlog. This pollutes
`get_backlog_stats`, `list_library` (`filter=unplayed|played`), and
`find_hidden_gems` (`unplayed_only`).

### Scope is broader than manual games

NULL playtime is not unique to manual adds — it is how several syncs
legitimately represent "playtime unavailable":

- **GOG**: always `playtime_minutes=None` — no playtime API (`data/gog.py:231`)
- **Nintendo**: NULL on the VGCS ownership fallback; only nxapi-launched titles
  get real minutes (`data/nintendo.py:339`)
- **Epic**: NULL for all games whenever the playtime endpoint is unavailable /
  auth-stale (`data/epic.py:351`)
- **Steam / PS5**: real minutes; Steam includes a genuine `0` for never-launched

So the fix must treat "playtime unknown" as a first-class state for *any* game
with no playtime from any source, not just manual adds.

## Decisions

1. **Three states, not two:** `played` / `unplayed` / `unknown`. `unknown` is
   excluded from both played and unplayed/backlog counts and surfaced as its own
   bucket. *(Q1: A)*
2. **Scope = any game with no playtime data from any source**, detected via a
   NULL-aware rollup. No "manual" marker needed; generalizes to GOG/Nintendo/
   Epic-outage. *(Q2: A)*
3. **Recommendations stay inclusive:** `find_hidden_gems(unplayed_only=True)`
   means "not confirmed-played" — keeps `unplayed` *and* `unknown`, excludes only
   `played`. *(Q3: A)*
4. **Explicit representation:** add a `play_state` enum to outputs and emit
   `playtime_hours: null` (not `0.0`) when unknown. *(Q4: A)*

## Core mechanism

Drop the playtime-erasing inner coalesce in each rollup CTE:

```sql
-- before
COALESCE(SUM(COALESCE(gp.playtime_minutes, 0)), 0) AS total_playtime_minutes
-- after  (NULL when every contributing source is NULL)
SUM(gp.playtime_minutes) AS total_playtime_minutes
```

Derive state once via a **shared SQL CASE constant** that each module embeds in
its own CTE. This keeps classification DRY and consistent while respecting the
existing rule in `tools/common.py` that the per-module rollup CTEs are
intentionally *not* merged.

```sql
CASE
    WHEN is_farmed = 1                  THEN 'unplayed'   -- farmed stays backlog
    WHEN total_playtime_minutes IS NULL THEN 'unknown'
    WHEN total_playtime_minutes = 0     THEN 'unplayed'
    ELSE 'played'
END AS play_state
```

Semantics:

- `0` — a source authoritatively reported zero (e.g. Steam never-launched) →
  **unplayed**
- `NULL` — no source has the data (GOG, manual, Nintendo VGCS, Epic outage) →
  **unknown**
- Steam(0) + GOG(NULL) → `SUM = 0` → **unplayed** (correct: Steam knows)
- GOG(NULL) + manual(NULL) → `SUM = NULL` → **unknown**
- Game with zero platform rows → `SUM = NULL` → **unknown** (honest)

The 2-week *recent-pace* total keeps `COALESCE(..., 0)` — NULL→0 is fine for a
pace metric and is never used for state classification.

A single shared definition (the NULL-aware total expression + the `play_state`
CASE) lives in one place (e.g. `tools/common.py` or the `db` package) as string
constants and is interpolated into the existing CTEs in `stats.py`,
`library.py`, and `discover.py`.

## Per-consumer behavior

### `get_backlog_stats` (`tools/stats.py`)
- Three counts: `played`, `unplayed`, **`unknown_playtime`** (new).
- All backlog metrics computed over `play_state = 'unplayed'` **only** — unknown
  excluded: `unplayed_with_hltb`, `backlog_hours_hltb`, `years_to_clear_backlog`,
  `best_unplayed_metacritic|opencritic|personal`, `most_played_genre_in_backlog`.
- Percentages become three buckets over `total_library`: `played_pct`,
  `unplayed_pct`, **`unknown_pct`**. `unplayed_pct` is no longer derived as
  `100 - played_pct`.

### `list_library` (`tools/library.py`)
- Add `"unknown"` to `VALID_FILTERS`.
- `filter=unplayed` → `play_state='unplayed'` (excludes unknown);
  `filter=played` → `play_state='played'`; `filter=unknown` →
  `play_state='unknown'`; `filter=all` and `filter=farmed` unchanged.
- Library stats summary gains an `unknown` count.

### `find_hidden_gems` (`tools/discover.py`)
- `unplayed_only=True` → keep `unplayed` **and** `unknown`, exclude only
  `played`.

### `get_game_detail` (`tools/detail.py`)
- Emit `play_state`; `playtime_hours` is `null` when unknown.

### admin farming detection (`tools/admin.py:281`)
- Per-platform `playtime_minutes > 0` already excludes NULL rows. No change;
  verify in tests.

## Output shape

- `GameSummary` (`tools/models.py`): add `play_state: str | None`
  (`played`|`unplayed`|`unknown`); `playtime_hours` emitted as `null` instead of
  `0.0` when unknown.
- `LibraryStatsResponse`: add `unknown: int`.
- `get_backlog_stats` response: add `unknown_playtime` and `unknown_pct`;
  document that `unplayed` and the backlog metrics now exclude unknown.

## Testing (TDD — write failing tests first)

Fixtures:

- GOG-only (NULL) → `unknown`
- Steam `0` → `unplayed`
- Steam `>0` → `played`
- Steam(0) + GOG(NULL) → `unplayed`
- GOG(NULL) + manual(NULL) → `unknown`

Assertions:

- `get_backlog_stats`: three counts present; `played + unplayed + unknown ==
  total_library`; pct buckets sum to 100 (±rounding); backlog metrics exclude
  unknown.
- `list_library`: each of `unplayed` / `played` / `unknown` / `all` returns the
  right set; stats summary `unknown` count correct.
- `find_hidden_gems(unplayed_only=True)`: includes unknown games, excludes
  played.
- `get_game_detail`: unknown game returns `playtime_hours = null` and
  `play_state = "unknown"`.

Touch points: `tests/test_tools_stats.py`, `tests/test_tools_library.py`,
`tests/test_tools_discover.py`, `tests/test_tools_detail.py`.

## Non-goals / explicit decisions

- **No schema change, no data migration** — existing NULLs are already correct;
  the bug was purely at rollup time.
- **`-1` sentinel rejected** — NULL is the correct SQL "unknown"; a sentinel
  would re-introduce the same coercion risk across every query.
- Farmed semantics unchanged (farmed = unplayed bucket).
