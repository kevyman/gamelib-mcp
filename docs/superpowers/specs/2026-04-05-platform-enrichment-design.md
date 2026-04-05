# Platform-Aware Enrichment Design

**Date:** 2026-04-05  
**Branch:** feat/platform-tools  
**Status:** Approved for implementation

## Problem

The current enrichment model treats a game as a single entity regardless of platform or edition:

- `metacritic_score` and `opencritic_score` live on the `games` table — always the Steam/PC value, never populated for non-Steam platforms
- Fuzzy name matching at sync time can merge different editions into the same `games` row (e.g., "Resident Evil 4" original and "Resident Evil 4 Remake" could collapse together)
- `release_date` exists in the schema but is never populated
- Non-Steam games (Nintendo, PSN, GOG, Epic) get no review enrichment at all
- Non-Steam games also have no `tags` or `genres` — they're invisible to tag affinity scoring, `find_games_by_vibe`, and recommendations

## Goals

1. Store platform-specific review scores (Metacritic, OpenCritic) per platform row, not per game
2. Use IGDB as the canonical identity resolver so remakes, remasters, and distinct editions create separate `games` rows
3. Populate `release_date` (canonical) and per-platform release dates
4. Populate `tags` and `genres` for non-Steam games via IGDB so they participate in recommendations and vibe search
5. Run review enrichment for all platforms in the background pipeline

## Non-Goals

- Backfilling tags for existing Steam games (they already have them from Steam Store / SteamSpy)
- Changing how Steam-specific enrichment works (store data, ProtonDB, SteamSpy)
- Changing tag affinity or recommendations logic
- Differentiating OpenCritic scores by platform (their API is cross-platform aggregate — still more useful than PC-only Metacritic)

---

## Schema: V3 Migration

**`SCHEMA_VERSION` bumps from 2 → 3.**

### `games` table changes

Remove `metacritic_score` and `opencritic_score` (moving to `game_platform_enrichment`). Add `igdb_cached_at` to track when IGDB metadata was last resolved. `igdb_id` and `release_date` already exist but were never populated — we now populate both.

### New table: `game_platform_enrichment`

One row per `game_platforms` row. Parallel to `steam_platform_data`.

```sql
CREATE TABLE game_platform_enrichment (
    game_platform_id      INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
    platform_release_date TEXT,
    metacritic_score      INTEGER,
    metacritic_url        TEXT,
    opencritic_id         INTEGER,
    opencritic_score      INTEGER,
    opencritic_tier       TEXT,
    opencritic_percent_rec REAL,
    metacritic_cached_at  TEXT,
    opencritic_cached_at  TEXT
);
```

`opencritic_tier` values: "Mighty", "Strong", "Fair", "Weak" (from OpenCritic API).

### V2 → V3 migration steps

1. Create `game_platform_enrichment` table
2. Copy existing `metacritic_score` values from `games` into `game_platform_enrichment` for the corresponding Steam `game_platform` row (preserves data already fetched)
3. Add `igdb_cached_at TEXT` column to `games`
4. Drop `metacritic_score` and `opencritic_score` from `games` (SQLite: recreate table)

---

## IGDB Identity Resolution

### Purpose

IGDB's `category` field distinguishes main games (0), remakes (8), remasters (9), ports (11), expanded games (10), etc. By anchoring game identity to `igdb_id` rather than fuzzy name matching, we ensure:

- "Resident Evil 4" (2005, category=0) and "Resident Evil 4 Remake" (2023, category=8) → separate `games` rows
- "Final Fantasy VII" (1997) and "Final Fantasy VII Remake" (2020) → separate `games` rows
- "Elden Ring" on PS5 and "Elden Ring" on Steam → same `games` row (same `igdb_id`, different `game_platforms`)

### New module: `data/igdb.py`

Handles OAuth2 token refresh (Twitch credentials) and game search.

**New env vars required:**
- `TWITCH_CLIENT_ID`
- `TWITCH_CLIENT_SECRET`

**IGDB platform ID mapping:**

| Our platform value | IGDB platform ID |
|--------------------|-----------------|
| `steam`, `epic`, `gog` | 6 (PC Windows) |
| `psn` (PS5) | 167 |
| `psn` (PS4) | 48 |
| `switch2` | 130 (Switch; Switch 2 not yet in IGDB — search without platform filter) |

### Resolution logic at sync time

Applied for non-Steam platforms (Nintendo, PSN, GOG, Epic) when processing each game entry. Steam games already have a canonical `appid` and do not need IGDB resolution at sync time.

1. Search IGDB: `POST /games` with title + platform filter
2. Take best name match from results
3. **`igdb_id` already in our `games` table** → link this platform entry to that existing `games` row (same game, different platform — no new row)
4. **`igdb_id` is new** → create a new `games` row even if the name fuzzy-matches an existing game
5. **No IGDB result** → fall back to existing fuzzy name matching (covers obscure titles absent from IGDB)

Store `first_release_date` (Unix timestamp → ISO date string) into `games.release_date` if not already set. Record `igdb_cached_at`.

### Tags and genres from IGDB

When resolving IGDB for a game that has no `tags` or `genres` yet (all non-Steam games, and any Steam game where store enrichment didn't populate them), fetch and store:

- `genres` → IGDB `genres.name` values → stored as JSON array in `games.genres` (same format as Steam)
- `tags` → union of IGDB `themes.name` + `keywords.name` → stored as JSON array in `games.tags`

**IGDB query fields to request:**
```
fields id, name, category, first_release_date, parent_game, platforms,
       release_dates.platform, release_dates.date,
       genres.name, themes.name, keywords.name;
```

This is a single API call per game — no additional requests beyond the identity resolution call. Steam games skip this: their tags come from Steam Store + SteamSpy which are richer and more tailored. Only write to `games.tags` / `games.genres` if the columns are currently null.

### Per-platform release dates

When resolving IGDB, also fetch `release_dates` for the matched game and store the platform-specific date into `game_platform_enrichment.platform_release_date`.

Steam games: `release_date` populated from `store_data["release_date"]["date"]` during existing store enrichment (field is returned today, just not stored).

---

## Review Enrichment

### `data/opencritic.py` (replaces current stub)

Current `opencritic.py` is a dead stub that just reads `metacritic_score` from the DB. Replace entirely.

**Endpoints:**
- `GET https://api.opencritic.com/api/game/search?criteria={name}` — find game
- `GET https://api.opencritic.com/api/game/{id}` — fetch scores

**No API key required.**

**Fields stored** (in `game_platform_enrichment`):
- `opencritic_id`, `opencritic_score`, `opencritic_tier`, `opencritic_percent_rec`, `opencritic_cached_at`

**Cache:** 30 days. Score is a cross-platform aggregate — stored on each platform row for the game so any platform's detail view can show it.

### `data/metacritic.py` (new scraper)

Scrapes `https://www.metacritic.com/game/{slug}/` for Metascore. Platform-specific: the slug and path differ per platform.

**Platform slug mapping:**

| Our platform value | Metacritic path segment |
|--------------------|-----------------------|
| `steam`, `epic`, `gog` | `pc` |
| `psn` | `playstation-5` (default; see note below) |
| `switch2` | `switch` |

> **Note:** PSN currently uses a single `"psn"` platform value for both PS4 and PS5 titles. The metacritic scraper will default to `playstation-5`. If a title is not found there, it should fall back to `playstation-4`. IGDB resolution at sync time can provide the exact platform ID to disambiguate — this should be used to set a `platform_generation` hint on the `game_platform` row, or the metacritic scraper should try PS5 first then PS4.

**Fields stored:** `metacritic_score`, `metacritic_url`, `metacritic_cached_at`

**Cache:** 30 days. Failures are silent (leave `metacritic_cached_at` null so the background job retries next cycle).

### `data/steam_store.py` changes

Remove the metacritic side-effect from `enrich_game()` — stop writing `metacritic_score` to `games`. The new `metacritic.py` module handles it for the Steam platform row via the background pipeline.

---

## Background Enrichment Pipeline

`enrich_bg.py` gains two new phases after the existing four (store, HLTB, ProtonDB, SteamSpy):

**Phase 5 — OpenCritic:** All `game_platform` rows where `game_platform_enrichment.opencritic_cached_at IS NULL`, ordered by playtime descending. Rate limit: 1.0s between calls.

**Phase 6 — Metacritic:** All `game_platform` rows where `game_platform_enrichment.metacritic_cached_at IS NULL`, ordered by playtime descending. Rate limit: 2.0s between calls (scraping).

Both phases process all platforms, not just Steam.

---

## `get_game_detail` Output Changes

Per-platform objects in the `platforms` list gain new fields populated from `game_platform_enrichment`:

```json
{
  "platform": "psn",
  "playtime_hours": 24.3,
  "platform_release_date": "2023-03-24",
  "metacritic_score": 93,
  "metacritic_url": "https://www.metacritic.com/game/resident-evil-4/playstation-5/",
  "opencritic_score": 91,
  "opencritic_tier": "Mighty",
  "opencritic_percent_rec": 96.0
}
```

Game-level `metacritic_score` and `opencritic_score` fields are removed from the top-level response. `release_date` is added at the top level (canonical earliest release).

---

## File Changes Summary

| File | Change |
|------|--------|
| `data/db.py` | V3 schema + migration; new `upsert_game_platform_enrichment` helper |
| `data/igdb.py` | New — OAuth2 + game search + tags/genres/release dates |
| `data/opencritic.py` | Replace stub with real OpenCritic API client |
| `data/metacritic.py` | New — platform-aware Metacritic scraper |
| `data/steam_store.py` | Remove metacritic side-effect; store `release_date` |
| `data/enrich_bg.py` | Add Phase 5 (OpenCritic) and Phase 6 (Metacritic) |
| `data/nintendo.py` | Add IGDB resolution at sync time |
| `data/psn.py` | Add IGDB resolution at sync time |
| `data/epic.py` | Add IGDB resolution at sync time |
| `data/gog.py` | Add IGDB resolution at sync time |
| `tools/detail.py` | Read enrichment from `game_platform_enrichment`; expose `release_date` |
| `.env.example` | Add `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` |
