# Cross-Platform Game Library — Design Spec

**Date:** 2026-03-25
**Status:** Approved for implementation planning

---

## Goal

Expand gamelib-mcp from a Steam-only library into a unified cross-platform game collection manager. Primary use case: avoid duplicate purchases, rediscover forgotten games, and get recommendations that factor in which platform and hardware to use.

---

## Platforms

Supported in priority order:

| Platform | Library API | Playtime | Auth type |
|---|---|---|---|
| Nintendo Switch | nxapi (play history only — not full purchase library) | Yes | Session token via nxapi auth flow |
| PlayStation 5 | PSNAWP (Python) | Yes (PS5 only) | NPSSO cookie from browser |
| Epic Games Store | legendary CLI (Python) | No | OAuth2 via `legendary auth` |
| GOG | GOG OAuth2 API | No | OAuth2 refresh token |
| Xbox / Game Pass | xbox-webapi-python | Yes | OAuth2 via Azure app registration |
| Itch.io | Itch.io REST API | No | API key from account settings |

**Nintendo caveat:** nxapi exposes play history (launched titles + hours), not purchase records. Unplayed digital purchases and physical cartridges that haven't been inserted will not appear. This is a Nintendo platform limitation with no workaround.

---

## Hardware Preferences

User's preferred hardware, in descending order:

```
switch2, steam_deck, ps5
```

Stored as a JSON list in the `meta` table under key `hardware_preference`. Used by `get_recommendations` to suggest the best platform to play a given game on, based on which platforms the user owns it on ranked against this preference list.

---

## Database Schema

Complete replacement of the existing schema — no backwards compatibility required.

### `games` table (revised)

```sql
CREATE TABLE games (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    appid            INTEGER UNIQUE,          -- Steam only, NULL for non-Steam
    igdb_id          INTEGER UNIQUE,          -- nullable; column reserved for future IGDB enrichment, not populated in this implementation
    name             TEXT NOT NULL,
    sort_name        TEXT,
    release_date     TEXT,
    genres           TEXT,                    -- JSON array
    tags             TEXT,                    -- JSON array
    metacritic_score INTEGER,
    hltb_main        REAL,
    hltb_extra       REAL,
    hltb_complete    REAL,
    protondb_tier    TEXT,
    opencritic_score INTEGER,
    store_enriched   INTEGER DEFAULT 0,
    store_enriched_at TEXT
);
```

Dropped from original schema: `playtime_forever`, `playtime_2weeks` — playtime now lives entirely in `game_platforms`.

### `game_platforms` table (new)

```sql
CREATE TABLE game_platforms (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id          INTEGER NOT NULL REFERENCES games(id),
    platform         TEXT NOT NULL,           -- 'steam','switch','ps5','epic','gog','xbox','itchio'
    owned            INTEGER NOT NULL DEFAULT 1,
    playtime_minutes INTEGER,                 -- NULL if platform doesn't expose it
    last_synced      TEXT,
    UNIQUE(game_id, platform)
);
```

### `ratings` table

Foreign key updated to reference `games(id)` instead of `games(appid)`.

### `tag_affinity` table

No changes needed.

### `meta` table

No changes needed. New key added at runtime: `hardware_preference`.

---

## Migration

A one-shot script `gamelib_mcp/migrate.py`:

1. Creates the new schema in a fresh DB (or renames existing tables to `_old`)
2. Re-inserts all rows from `games_old` with new autoincrement `id`, preserving `appid` and all enrichment fields
3. Creates a `game_platforms` row per game: `platform='steam'`, `playtime_minutes` from old `playtime_forever`
4. Re-inserts `ratings` and `tag_affinity` with updated foreign keys
5. Drops old tables

Run once with: `python -m gamelib_mcp.migrate`

---

## New Data Layer Modules

One file per platform in `gamelib_mcp/data/`, all async, following existing patterns:

- `psn.py` — wraps PSNAWP; fetches trophy list as game library + PS5 playtime
- `epic.py` — invokes legendary subprocess; parses owned games JSON
- `gog.py` — GOG OAuth2 API; fetches owned games
- `nintendo.py` — wraps nxapi subprocess; fetches play history
- `xbox.py` — xbox-webapi-python; fetches owned games + playtime
- `itchio.py` — Itch.io REST API; fetches owned games

### Cross-Platform Deduplication

When syncing any non-Steam platform:

1. Fuzzy-match incoming game name against existing `games.name` rows (rapidfuzz `token_sort_ratio`, cutoff=85 — same as Backloggd integration)
2. **Match found** → insert/update `game_platforms` row on existing record
3. **No match** → insert new `games` row (`appid=NULL`), then insert `game_platforms` row
4. Steam wins on naming and metadata when both sources exist

---

## Credentials & Setup

All credentials in `.env`:

```
PSN_NPSSO=...
EPIC_LEGENDARY_PATH=...     # path to legendary config dir (after running legendary auth)
GOG_REFRESH_TOKEN=...
NINTENDO_SESSION_TOKEN=...
XBOX_CLIENT_ID=...
XBOX_CLIENT_SECRET=...
ITCHIO_API_KEY=...
HARDWARE_PREFERENCE=switch2,steam_deck,ps5
```

A setup script `python -m gamelib_mcp.setup_platform <platform>` handles the OAuth browser flows for GOG, Epic, and Xbox, writes the resulting tokens to `.env`, and handles token refresh at runtime. PSN (browser cookie extraction) and Nintendo (nxapi session token) are documented as manual one-time steps.

Any platform without credentials configured is silently skipped during sync.

---

## Tool Changes

### Updated tools

**`refresh_library`**
- Gains optional `platforms` parameter (default: all configured)
- Fans out to each platform sync module after Steam sync
- Reports per-platform results (games added, updated, skipped)

**`get_game_detail`**
- `playtime` field restructured:
```json
"playtime": {
  "total_minutes": 340,
  "by_platform": [
    { "platform": "steam",  "minutes": 220 },
    { "platform": "switch", "minutes": 120 }
  ]
}
```
- Platforms without playtime data are omitted from `by_platform`
- Adds `owned_on` field: list of platforms where the game is owned

**`get_library_stats`** / **`search_games`**
- Gain optional `platform` filter

**`get_recommendations`**
- Adds `suggested_platform` field per result: highest-ranked hardware preference among platforms the user owns the game on

### New tools

**`get_platform_breakdown`**
Returns:
- Total games per platform
- Total unique games across all platforms
- Overlap count (games owned on 2+ platforms — the "did I buy this twice?" list)

**`sync_platform`**
Syncs a single platform on demand: `sync_platform("ps5")`

---

## What's Explicitly Out of Scope

- Physical game collections (no barcode scanning or manual entry flows)
- Wishlists or unowned games from non-Steam platforms
- Game sharing / family library detection
- Price tracking or purchase recommendations
- Achievements / trophy sync (library only)
