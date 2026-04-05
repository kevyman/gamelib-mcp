# Platform-Aware Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add IGDB-anchored game identity resolution, platform-specific review scores (Metacritic/OpenCritic) per `game_platform` row, IGDB-sourced tags/genres for non-Steam games, and release date population.

**Architecture:** Schema V3 adds `game_platform_enrichment` (parallel to `steam_platform_data`) and drops `metacritic_score`/`opencritic_score` from `games`. IGDB resolves game identity at non-Steam sync time — matching by `igdb_id` rather than fuzzy name prevents edition/remake merging. Background enrichment gains two new phases for OpenCritic (API) and Metacritic (scraper) across all platforms.

**Tech Stack:** aiosqlite (SQLite migrations), httpx (IGDB/OpenCritic APIs + Metacritic scraping), BeautifulSoup4 (Metacritic HTML parsing), Twitch OAuth2 (IGDB auth)

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `gamelib_mcp/data/db.py` | Modify | V3 schema DDL, migration, new helpers |
| `gamelib_mcp/data/igdb.py` | Create | Twitch OAuth2, IGDB game search, `resolve_and_link_game` |
| `gamelib_mcp/data/opencritic.py` | Replace | Real OpenCritic API client writing to `game_platform_enrichment` |
| `gamelib_mcp/data/metacritic.py` | Create | Platform-aware Metacritic scraper |
| `gamelib_mcp/data/steam_store.py` | Modify | Add `release_date`, remove `metacritic_score` write to `games`, write metacritic to enrichment table |
| `gamelib_mcp/data/enrich_bg.py` | Modify | Add Phase 5 (OpenCritic) and Phase 6 (Metacritic) |
| `gamelib_mcp/data/nintendo.py` | Modify | Use `resolve_and_link_game` instead of fuzzy-only matching |
| `gamelib_mcp/data/psn.py` | Modify | Use `resolve_and_link_game` instead of fuzzy-only matching |
| `gamelib_mcp/data/epic.py` | Modify | Use `resolve_and_link_game` instead of fuzzy-only matching |
| `gamelib_mcp/data/gog.py` | Modify | Use `resolve_and_link_game` instead of fuzzy-only matching |
| `gamelib_mcp/tools/detail.py` | Modify | Expose `release_date`, read enrichment from platform rows |
| `.env.example` | Modify | Add `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` |

---

## Task 1: V3 Schema DDL

**Files:** Modify `gamelib_mcp/data/db.py`

- [ ] **Step 1: Bump SCHEMA_VERSION and add V3 DDL constant**

  In `db.py`, change `SCHEMA_VERSION = 2` to `SCHEMA_VERSION = 3` and add the `_V3_SCHEMA_DDL` constant after `_V2_SCHEMA_DDL`. The V3 DDL is the full schema (used for fresh installs and as the idempotent "ensure complete" step):

  ```python
  SCHEMA_VERSION = 3
  ```

  Add after `_V2_SCHEMA_DDL`:

  ```python
  _V3_SCHEMA_DDL = """
      CREATE TABLE IF NOT EXISTS games (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          igdb_id          INTEGER UNIQUE,
          name             TEXT NOT NULL,
          sort_name        TEXT,
          release_date     TEXT,
          genres           TEXT,
          tags             TEXT,
          short_description TEXT,
          hltb_main        REAL,
          hltb_extra       REAL,
          hltb_complete    REAL,
          hltb_cached_at   TEXT,
          igdb_cached_at   TEXT,
          is_farmed        INTEGER NOT NULL DEFAULT 0
      );

      CREATE TABLE IF NOT EXISTS game_platforms (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          game_id          INTEGER NOT NULL REFERENCES games(id),
          platform         TEXT NOT NULL,
          owned            INTEGER NOT NULL DEFAULT 1,
          playtime_minutes INTEGER,
          playtime_2weeks_minutes INTEGER,
          last_synced      TEXT,
          UNIQUE(game_id, platform)
      );

      CREATE TABLE IF NOT EXISTS game_platform_identifiers (
          id               INTEGER PRIMARY KEY AUTOINCREMENT,
          game_platform_id INTEGER NOT NULL REFERENCES game_platforms(id) ON DELETE CASCADE,
          identifier_type  TEXT NOT NULL,
          identifier_value TEXT NOT NULL,
          is_primary       INTEGER NOT NULL DEFAULT 1,
          last_seen_at     TEXT,
          UNIQUE(identifier_type, identifier_value)
      );

      CREATE TABLE IF NOT EXISTS steam_platform_data (
          game_platform_id    INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
          steam_review_score  INTEGER,
          steam_review_desc   TEXT,
          protondb_tier       TEXT,
          store_cached_at     TEXT,
          protondb_cached_at  TEXT,
          steamspy_cached_at  TEXT,
          rtime_last_played   INTEGER,
          library_updated_at  TEXT
      );

      CREATE TABLE IF NOT EXISTS game_platform_enrichment (
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

      CREATE TABLE IF NOT EXISTS ratings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          game_id INTEGER REFERENCES games(id),
          source TEXT NOT NULL,
          raw_score REAL,
          normalized_score REAL,
          review_text TEXT,
          synced_at TEXT NOT NULL,
          UNIQUE(game_id, source)
      );

      CREATE TABLE IF NOT EXISTS tag_affinity (
          tag TEXT PRIMARY KEY,
          affinity_score REAL,
          avg_score REAL,
          game_count INTEGER,
          updated_at TEXT
      );

      CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT
      );

      CREATE INDEX IF NOT EXISTS idx_game_platforms_game_id ON game_platforms(game_id);
      CREATE INDEX IF NOT EXISTS idx_game_platforms_platform ON game_platforms(platform);
      CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_platform_id
          ON game_platform_identifiers(game_platform_id);
      CREATE INDEX IF NOT EXISTS idx_game_platform_identifiers_lookup
          ON game_platform_identifiers(identifier_type, identifier_value);
  """
  ```

- [ ] **Step 2: Add v3 detection to `_detect_schema_state`**

  Replace the existing `_detect_schema_state` function:

  ```python
  async def _detect_schema_state(db: aiosqlite.Connection) -> str:
      tables = await _table_names(db)
      if "games" not in tables:
          return "fresh"

      game_cols = await _table_columns(db, "games")
      if "id" not in game_cols:
          return "legacy"

      if "game_platform_enrichment" in tables and "metacritic_score" not in game_cols:
          return "v3"

      if {
          "game_platform_identifiers",
          "steam_platform_data",
      }.issubset(tables) and "appid" not in game_cols:
          return "v2"

      return "v1"
  ```

- [ ] **Step 3: Add `_migrate_v2_to_v3` function**

  Add after `_migrate_v1_to_v2`:

  ```python
  async def _migrate_v2_to_v3(db: aiosqlite.Connection, progress: _Progress | None) -> None:
      if progress is not None:
          progress("Migrating to v3: platform-specific enrichment schema.")

      await db.execute("PRAGMA foreign_keys=OFF")
      db.row_factory = aiosqlite.Row

      # 1. Create game_platform_enrichment table
      await db.execute("""
          CREATE TABLE IF NOT EXISTS game_platform_enrichment (
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
          )
      """)

      # 2. Migrate metacritic_score from games → game_platform_enrichment (Steam rows only)
      game_cols = await _table_columns(db, "games")
      if "metacritic_score" in game_cols:
          await db.execute(
              """INSERT OR IGNORE INTO game_platform_enrichment (game_platform_id, metacritic_score)
                 SELECT gp.id, g.metacritic_score
                 FROM games g
                 JOIN game_platforms gp ON gp.game_id = g.id AND gp.platform = 'steam'
                 WHERE g.metacritic_score IS NOT NULL"""
          )

      # 3. Rebuild games table: drop metacritic_score, opencritic_score; add igdb_cached_at
      await db.execute("ALTER TABLE games RENAME TO games_v2_old")
      await db.execute("""
          CREATE TABLE games (
              id               INTEGER PRIMARY KEY AUTOINCREMENT,
              igdb_id          INTEGER UNIQUE,
              name             TEXT NOT NULL,
              sort_name        TEXT,
              release_date     TEXT,
              genres           TEXT,
              tags             TEXT,
              short_description TEXT,
              hltb_main        REAL,
              hltb_extra       REAL,
              hltb_complete    REAL,
              hltb_cached_at   TEXT,
              igdb_cached_at   TEXT,
              is_farmed        INTEGER NOT NULL DEFAULT 0
          )
      """)

      old_cols = await _table_columns(db, "games_v2_old")
      keep = [c for c in [
          "id", "igdb_id", "name", "sort_name", "release_date",
          "genres", "tags", "short_description",
          "hltb_main", "hltb_extra", "hltb_complete", "hltb_cached_at", "is_farmed",
      ] if c in old_cols]
      cols_sql = ", ".join(keep)
      await db.execute(f"INSERT INTO games ({cols_sql}) SELECT {cols_sql} FROM games_v2_old")
      await db.execute("DROP TABLE games_v2_old")

      await _set_user_version(db, 3)
      await db.commit()
      await db.execute("PRAGMA foreign_keys=ON")
  ```

- [ ] **Step 4: Wire migration into `_run_migrations`**

  In `_run_migrations`, make two changes:

  a) Add v3 detection in the `version == 0` block:
  ```python
  if version == 0:
      if detected_state == "legacy":
          _emit(progress, "Applying migration step v0 -> v1.", applied_steps)
          await _migrate_legacy_to_v1(db, progress=None)
          version = 1
      elif detected_state == "v1":
          await _set_user_version(db, 1)
          await db.commit()
          version = 1
          _emit(progress, "Recorded existing schema as v1.", applied_steps)
      elif detected_state == "v2":
          await _set_user_version(db, 2)
          await db.commit()
          version = 2
          _emit(progress, "Recorded existing schema as v2.", applied_steps)
      elif detected_state == "v3":
          await _set_user_version(db, 3)
          await db.commit()
          version = 3
          _emit(progress, "Recorded existing schema as v3.", applied_steps)
  ```

  b) Add v2→v3 migration step after the existing v1→v2 block:
  ```python
  if version == 2:
      _emit(progress, "Applying migration step v2 -> v3.", applied_steps)
      await _migrate_v2_to_v3(db, progress=None)
      version = 3
  ```

  c) Change the final idempotent DDL call from `_V2_SCHEMA_DDL` to `_V3_SCHEMA_DDL`:
  ```python
  await db.executescript(_V3_SCHEMA_DDL)
  ```

  d) In the `detected_state == "fresh"` block, change `_V2_SCHEMA_DDL` to `_V3_SCHEMA_DDL`:
  ```python
  if detected_state == "fresh":
      await db.executescript(_V3_SCHEMA_DDL)
      await _set_user_version(db, SCHEMA_VERSION)
      await db.commit()
      _emit(progress, "Initialized fresh database at schema v3.", applied_steps)
      return MigrationResult(...)
  ```

- [ ] **Step 5: Verify migration runs cleanly**

  ```bash
  cd /home/john/code/gamelib-mcp
  python -c "import asyncio; from gamelib_mcp.data.db import init_db; asyncio.run(init_db()); print('OK')"
  ```

  Expected: `OK` with no errors. Check the DB schema:
  ```bash
  sqlite3 steam.db ".schema game_platform_enrichment"
  ```
  Expected: the CREATE TABLE statement with all enrichment columns.

- [ ] **Step 6: Commit**

  ```bash
  git add gamelib_mcp/data/db.py
  git commit -m "feat: schema v3 — game_platform_enrichment table, drop metacritic from games"
  ```

---

## Task 2: DB Helper Functions

**Files:** Modify `gamelib_mcp/data/db.py`

- [ ] **Step 1: Add `upsert_game_platform_enrichment`**

  Add after `upsert_steam_platform_data`:

  ```python
  async def upsert_game_platform_enrichment(game_platform_id: int, **fields) -> None:
      if not fields:
          return
      columns = ", ".join(["game_platform_id", *fields.keys()])
      placeholders = ", ".join("?" for _ in range(len(fields) + 1))
      updates = ", ".join(f"{column} = excluded.{column}" for column in fields)
      async with get_db() as db:
          await db.execute(
              f"""INSERT INTO game_platform_enrichment ({columns})
                  VALUES ({placeholders})
                  ON CONFLICT(game_platform_id) DO UPDATE SET {updates}""",
              (game_platform_id, *fields.values()),
          )
          await db.commit()
  ```

- [ ] **Step 2: Add `get_game_by_igdb_id`**

  Add after `get_game_by_appid`:

  ```python
  async def get_game_by_igdb_id(igdb_id: int) -> aiosqlite.Row | None:
      async with get_db() as db:
          return await db.execute_fetchone(
              "SELECT * FROM games WHERE igdb_id = ?", (igdb_id,)
          )
  ```

- [ ] **Step 3: Update `get_steam_platform_row_by_appid` to remove `metacritic_score`**

  The column no longer exists on `games`. In `get_steam_platform_row_by_appid`, remove `g.metacritic_score,` from the SELECT list. The full updated SELECT:

  ```python
  async def get_steam_platform_row_by_appid(appid: int) -> aiosqlite.Row | None:
      async with get_db() as db:
          return await db.execute_fetchone(
              """SELECT gp.id AS game_platform_id,
                        gp.game_id,
                        gp.platform,
                        gp.owned,
                        gp.playtime_minutes,
                        gp.playtime_2weeks_minutes,
                        gp.last_synced,
                        g.name,
                        g.genres,
                        g.tags,
                        g.short_description,
                        g.release_date,
                        g.hltb_main,
                        g.hltb_extra,
                        g.hltb_complete,
                        g.hltb_cached_at,
                        g.is_farmed,
                        spd.steam_review_score,
                        spd.steam_review_desc,
                        spd.protondb_tier,
                        spd.store_cached_at,
                        spd.protondb_cached_at,
                        spd.steamspy_cached_at,
                        spd.rtime_last_played,
                        spd.library_updated_at,
                        gpe.metacritic_score,
                        gpe.metacritic_url,
                        gpe.opencritic_score,
                        gpe.opencritic_tier,
                        gpe.opencritic_percent_rec,
                        gpe.platform_release_date
                 FROM game_platform_identifiers gpi
                 JOIN game_platforms gp ON gp.id = gpi.game_platform_id
                 JOIN games g ON g.id = gp.game_id
                 LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
                 LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                 WHERE gpi.identifier_type = ? AND gpi.identifier_value = ?
                 LIMIT 1""",
              (STEAM_APP_ID, str(appid)),
          )
  ```

- [ ] **Step 4: Update `load_platforms_for_games` to JOIN `game_platform_enrichment`**

  In `load_platforms_for_games`, add `gpe.*` to the SELECT and the JOIN. Replace the existing query:

  ```python
  rows = await db.execute_fetchall(
      f"""SELECT gp.id AS game_platform_id,
                 gp.game_id,
                 gp.platform,
                 gp.owned,
                 gp.playtime_minutes,
                 gp.playtime_2weeks_minutes,
                 gp.last_synced,
                 gpi.identifier_type,
                 gpi.identifier_value,
                 gpi.is_primary,
                 spd.steam_review_score,
                 spd.steam_review_desc,
                 spd.protondb_tier,
                 spd.rtime_last_played,
                 spd.library_updated_at,
                 gpe.platform_release_date,
                 gpe.metacritic_score,
                 gpe.metacritic_url,
                 gpe.opencritic_score,
                 gpe.opencritic_tier,
                 gpe.opencritic_percent_rec
          FROM game_platforms gp
          LEFT JOIN game_platform_identifiers gpi ON gpi.game_platform_id = gp.id
          LEFT JOIN steam_platform_data spd ON spd.game_platform_id = gp.id
          LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
          WHERE gp.game_id IN ({placeholders})
          ORDER BY gp.game_id, gp.platform, gp.id, gpi.is_primary DESC, gpi.identifier_type""",
      ids,
  )
  ```

- [ ] **Step 5: Update `_platform_dict` to include enrichment fields**

  In `_platform_dict`, add enrichment fields directly to the platform dict (after the existing `provider_data` assignment):

  ```python
  def _platform_dict(row: aiosqlite.Row) -> dict:
      playtime_minutes = row["playtime_minutes"]
      playtime_2weeks_minutes = row["playtime_2weeks_minutes"]
      platform = {
          "platform": row["platform"],
          "owned": bool(row["owned"]),
          "playtime_minutes": playtime_minutes,
          "playtime_hours": round((playtime_minutes or 0) / 60, 1),
          "playtime_2weeks_minutes": playtime_2weeks_minutes,
          "playtime_2weeks_hours": round((playtime_2weeks_minutes or 0) / 60, 1),
          "last_synced": row["last_synced"],
          "identifiers": {},
          "provider_data": {},
          "platform_release_date": row["platform_release_date"],
          "metacritic_score": row["metacritic_score"],
          "metacritic_url": row["metacritic_url"],
          "opencritic_score": row["opencritic_score"],
          "opencritic_tier": row["opencritic_tier"],
          "opencritic_percent_rec": row["opencritic_percent_rec"],
      }

      if row["platform"] == STEAM_PLATFORM:
          last_played = row["rtime_last_played"]
          platform["provider_data"] = {
              "steam_review_score": row["steam_review_score"],
              "steam_review_desc": row["steam_review_desc"],
              "protondb_tier": row["protondb_tier"],
              "last_played_date": (
                  datetime.fromtimestamp(last_played, tz=timezone.utc).date().isoformat()
                  if last_played
                  else None
              ),
              "library_updated_at": row["library_updated_at"],
          }

      return platform
  ```

  Note: `row["platform_release_date"]` etc. will return `None` when `game_platform_enrichment` has no row yet — that's fine.

- [ ] **Step 6: Verify the server starts and `load_platforms_for_games` doesn't crash**

  ```bash
  python -m gamelib_mcp.main &
  sleep 3
  curl -s http://localhost:8000/health
  kill %1
  ```

  Expected: JSON health response with no errors in server output.

- [ ] **Step 7: Commit**

  ```bash
  git add gamelib_mcp/data/db.py
  git commit -m "feat: db helpers for game_platform_enrichment; update platform loading queries"
  ```

---

## Task 3: IGDB Module

**Files:** Create `gamelib_mcp/data/igdb.py`

- [ ] **Step 1: Create the module with token management and game search**

  ```python
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

      token = await _get_token()

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
              # New igdb_id — create a fresh row, bypassing fuzzy matching
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
  ```

- [ ] **Step 2: Quick smoke test**

  ```bash
  python -c "
  import asyncio, os
  os.environ.setdefault('TWITCH_CLIENT_ID', os.getenv('TWITCH_CLIENT_ID', ''))
  from gamelib_mcp.data.igdb import resolve_game, IGDB_PLATFORM_PC
  result = asyncio.run(resolve_game('Elden Ring', IGDB_PLATFORM_PC))
  print(result)
  "
  ```

  Expected (with valid credentials): `IGDBGame(igdb_id=..., name='Elden Ring', ...)` with genres/tags populated.
  Expected (without credentials): `None` — graceful skip.

- [ ] **Step 3: Commit**

  ```bash
  git add gamelib_mcp/data/igdb.py
  git commit -m "feat: IGDB module — OAuth2 token, game search, resolve_and_link_game"
  ```

---

## Task 4: Steam Store — Release Date + Remove Metacritic Side-Effect

**Files:** Modify `gamelib_mcp/data/steam_store.py`

- [ ] **Step 1: Add `release_date` and `metacritic_url` to the store enrichment**

  Replace the `enrich_game` function's DB update block. Change the `UPDATE games SET` statement and add metacritic to `game_platform_enrichment`:

  ```python
  async with get_db() as db:
      if store_data is not None:
          steam_tags = _extract_tags(store_data)
          genres = json.dumps([g["description"] for g in store_data.get("genres", [])])
          short_desc = store_data.get("short_description", "")

          # Parse release date from "8 Nov, 2022" or "Q4 2022" formats
          raw_date = (store_data.get("release_date") or {}).get("date", "")
          release_date = _parse_steam_date(raw_date)

          await db.execute(
              """UPDATE games SET
                  genres = ?,
                  tags = ?,
                  short_description = ?,
                  release_date = COALESCE(release_date, ?)
              WHERE id = ?""",
              (genres, steam_tags, short_desc, release_date, row["game_id"]),
          )
      await db.commit()
  ```

  Note: `COALESCE(release_date, ?)` only fills in the date if it's currently null — preserving any existing value.

- [ ] **Step 2: Add `_parse_steam_date` helper**

  Add at the bottom of `steam_store.py`:

  ```python
  def _parse_steam_date(raw: str) -> str | None:
      """Parse Steam's release date string (e.g. '8 Nov, 2022') to ISO format, best-effort."""
      if not raw:
          return None
      import re
      # Try "D Mon, YYYY" or "D Mon YYYY"
      m = re.match(r"(\d{1,2})\s+([A-Za-z]+)[,\s]+(\d{4})", raw)
      if m:
          months = {
              "jan": "01", "feb": "02", "mar": "03", "apr": "04",
              "may": "05", "jun": "06", "jul": "07", "aug": "08",
              "sep": "09", "oct": "10", "nov": "11", "dec": "12",
          }
          month = months.get(m.group(2).lower()[:3])
          if month:
              return f"{m.group(3)}-{month}-{int(m.group(1)):02d}"
      # Try bare year
      m = re.match(r"^(\d{4})$", raw.strip())
      if m:
          return f"{m.group(1)}-01-01"
      return None
  ```

- [ ] **Step 3: Write metacritic to `game_platform_enrichment` instead of `games`**

  After the `await db.commit()` block, update the steam_fields section to also write metacritic to enrichment:

  ```python
  steam_fields = {"store_cached_at": now}
  if "review_score" in review_summary:
      steam_fields["steam_review_score"] = review_summary["review_score"]
  if "review_score_desc" in review_summary:
      steam_fields["steam_review_desc"] = review_summary["review_score_desc"]
  await upsert_steam_platform_data(row["game_platform_id"], **steam_fields)

  # Write metacritic to game_platform_enrichment (Steam Store gives us this for free)
  if store_data is not None:
      metacritic = store_data.get("metacritic") or {}
      metacritic_score = metacritic.get("score")
      metacritic_url = metacritic.get("url")
      if metacritic_score is not None:
          from .db import upsert_game_platform_enrichment
          enrichment_fields: dict = {
              "metacritic_score": metacritic_score,
              "metacritic_cached_at": now,
          }
          if metacritic_url:
              enrichment_fields["metacritic_url"] = metacritic_url
          await upsert_game_platform_enrichment(row["game_platform_id"], **enrichment_fields)
  ```

  Also update the `filters` param in `fetch_store()` to include `release_date`:
  ```python
  params={"appids": appid, "filters": "basic,genres,categories,short_description,metacritic,release_date"},
  ```

- [ ] **Step 4: Verify enrichment still works for a Steam game**

  ```bash
  python -c "
  import asyncio
  from gamelib_mcp.data.steam_store import enrich_game
  # Use any appid from your library
  result = asyncio.run(enrich_game(1091500))  # Cyberpunk 2077
  print('release_date:', result.get('release_date') if result else 'no result')
  print('metacritic_score:', result.get('metacritic_score') if result else 'no result')
  "
  ```

  Expected: `release_date: 2020-12-10` (or similar), `metacritic_score: 86` (or similar)

- [ ] **Step 5: Commit**

  ```bash
  git add gamelib_mcp/data/steam_store.py
  git commit -m "feat: steam store enrichment writes release_date and metacritic to platform row"
  ```

---

## Task 5: IGDB Integration in Non-Steam Syncs

**Files:** Modify `gamelib_mcp/data/nintendo.py`, `psn.py`, `epic.py`, `gog.py`

Each sync file replaces its fuzzy-matching identity resolution with `resolve_and_link_game`. The pattern is identical in all four files.

The pattern for all four files is identical: unpack `(game_id, igdb_game)` from `resolve_and_link_game`, then after `upsert_game_platform` write the platform-specific release date from the IGDB result into `game_platform_enrichment`.

- [ ] **Step 1: Update `nintendo.py`**

  Add imports at the top:
  ```python
  from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB
  from gamelib_mcp.data.db import upsert_game_platform_enrichment
  ```

  In `sync_nintendo`, replace the per-entry identity resolution block. Find:
  ```python
  existing = await find_game_by_name_fuzzy(name, candidates=candidates)
  if existing:
      game_id = existing["id"]
      matched += 1
  else:
      game_id = await upsert_game(appid=None, name=name)
      candidates[game_id] = name
      added += 1
  ```

  Replace with:
  ```python
  igdb_platform_id = PLATFORM_TO_IGDB.get(PLATFORM)
  game_id, igdb_game = await resolve_and_link_game(name, igdb_platform_id, candidates)
  if game_id in candidates:
      matched += 1
  else:
      candidates[game_id] = name
      added += 1
  ```

  Then find the `upsert_game_platform` call and add the platform_release_date write after it:
  ```python
  platform_id = await upsert_game_platform(
      game_id=game_id,
      platform=PLATFORM,
      playtime_minutes=entry["playtime_minutes"],
      owned=1,
  )

  # Write per-platform release date from IGDB if available
  if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
      await upsert_game_platform_enrichment(
          platform_id,
          platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
      )
  ```

  Remove `find_game_by_name_fuzzy` and `upsert_game` from the imports (called inside `resolve_and_link_game`). Keep `load_fuzzy_candidates`, `upsert_game_platform`, `upsert_game_platform_identifier`.

- [ ] **Step 2: Update `psn.py`**

  Add imports:
  ```python
  from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB
  from gamelib_mcp.data.db import upsert_game_platform_enrichment
  ```

  In `sync_psn`, replace:
  ```python
  existing = await find_game_by_name_fuzzy(name, candidates=candidates)
  if existing:
      game_id = existing["id"]
      matched += 1
  else:
      game_id = await upsert_game(appid=None, name=name)
      candidates[game_id] = name
      added += 1
  ```

  With:
  ```python
  igdb_platform_id = PLATFORM_TO_IGDB.get("ps5")
  game_id, igdb_game = await resolve_and_link_game(name, igdb_platform_id, candidates)
  if game_id in candidates:
      matched += 1
  else:
      candidates[game_id] = name
      added += 1
  ```

  After `upsert_game_platform`:
  ```python
  platform_id = await upsert_game_platform(
      game_id=game_id,
      platform="ps5",
      playtime_minutes=entry["playtime_minutes"],
      owned=1,
  )

  if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
      await upsert_game_platform_enrichment(
          platform_id,
          platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
      )
  ```

  Remove unused `find_game_by_name_fuzzy` and `upsert_game` imports.

- [ ] **Step 3: Update `epic.py`**

  Add imports:
  ```python
  from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB
  from gamelib_mcp.data.db import upsert_game_platform_enrichment
  ```

  In the sync loop (around line 266), replace the fuzzy + upsert block:
  ```python
  existing = await find_game_by_name_fuzzy(title, candidates=candidates)
  if existing:
      game_id = existing["id"]
      matched += 1
  else:
      game_id = await upsert_game(appid=None, name=title)
      candidates[game_id] = title
      added += 1
  ```

  With:
  ```python
  igdb_platform_id = PLATFORM_TO_IGDB.get("epic")
  game_id, igdb_game = await resolve_and_link_game(title, igdb_platform_id, candidates)
  if game_id in candidates:
      matched += 1
  else:
      candidates[game_id] = title
      added += 1
  ```

  After `upsert_game_platform`:
  ```python
  platform_id = await upsert_game_platform(
      game_id=game_id,
      platform="epic",
      playtime_minutes=entry.get("playtime_minutes"),
      owned=1,
  )

  if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
      await upsert_game_platform_enrichment(
          platform_id,
          platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
      )
  ```

  Remove unused `find_game_by_name_fuzzy` and `upsert_game` imports.

- [ ] **Step 4: Update `gog.py`**

  Add imports:
  ```python
  from gamelib_mcp.data.igdb import resolve_and_link_game, PLATFORM_TO_IGDB
  from gamelib_mcp.data.db import upsert_game_platform_enrichment
  ```

  In the sync loop (around line 136), replace the fuzzy + upsert block:
  ```python
  existing = await find_game_by_name_fuzzy(title, candidates=candidates)
  if existing:
      game_id = existing["id"]
      matched += 1
  else:
      game_id = await upsert_game(appid=None, name=title)
      candidates[game_id] = title
      added += 1
  ```

  With:
  ```python
  igdb_platform_id = PLATFORM_TO_IGDB.get("gog")
  game_id, igdb_game = await resolve_and_link_game(title, igdb_platform_id, candidates)
  if game_id in candidates:
      matched += 1
  else:
      candidates[game_id] = title
      added += 1
  ```

  After `upsert_game_platform`:
  ```python
  platform_id = await upsert_game_platform(
      game_id=game_id,
      platform="gog",
      playtime_minutes=None,
      owned=1,
  )

  if igdb_game is not None and igdb_platform_id in igdb_game.platform_release_dates:
      await upsert_game_platform_enrichment(
          platform_id,
          platform_release_date=igdb_game.platform_release_dates[igdb_platform_id],
      )
  ```

  Remove unused `find_game_by_name_fuzzy` and `upsert_game` imports.

- [ ] **Step 5: Verify the server starts without import errors**

  ```bash
  python -c "
  from gamelib_mcp.data import nintendo, psn, epic, gog
  print('imports OK')
  "
  ```

  Expected: `imports OK`

- [ ] **Step 6: Commit**

  ```bash
  git add gamelib_mcp/data/nintendo.py gamelib_mcp/data/psn.py \
          gamelib_mcp/data/epic.py gamelib_mcp/data/gog.py
  git commit -m "feat: use IGDB identity resolution in Nintendo/PSN/Epic/GOG syncs"
  ```

---

## Task 6: OpenCritic Module

**Files:** Replace `gamelib_mcp/data/opencritic.py`

- [ ] **Step 1: Rewrite `opencritic.py`**

  The current file is a dead stub (reads `metacritic_score` from DB). Replace entirely:

  ```python
  """OpenCritic API client — cross-platform review scores cached in game_platform_enrichment."""

  import logging
  from datetime import datetime, timezone

  import httpx

  from .db import get_db, upsert_game_platform_enrichment

  logger = logging.getLogger(__name__)

  OPENCRITIC_CACHE_DAYS = 30
  _SEARCH_URL = "https://api.opencritic.com/api/game/search"
  _GAME_URL = "https://api.opencritic.com/api/game/{id}"


  def _is_fresh(cached_at: str | None) -> bool:
      if not cached_at or cached_at == "FAILED":
          return cached_at == "FAILED"  # FAILED = treat as fresh (don't retry forever)
      try:
          dt = datetime.fromisoformat(cached_at)
          return (datetime.now(timezone.utc) - dt).total_seconds() < OPENCRITIC_CACHE_DAYS * 86400
      except ValueError:
          return False


  async def enrich_opencritic(game_platform_id: int, game_name: str) -> dict | None:
      """
      Fetch OpenCritic score for game_name and cache in game_platform_enrichment.
      Returns enrichment dict or None on failure.
      No API key required.
      """
      async with get_db() as db:
          row = await db.execute_fetchone(
              "SELECT opencritic_cached_at FROM game_platform_enrichment WHERE game_platform_id = ?",
              (game_platform_id,),
          )
      cached_at = row["opencritic_cached_at"] if row else None
      if _is_fresh(cached_at):
          return None

      now = datetime.now(timezone.utc).isoformat()

      oc_id = await _search_game(game_name)
      if oc_id is None:
          await upsert_game_platform_enrichment(game_platform_id, opencritic_cached_at="FAILED")
          return None

      data = await _fetch_game(oc_id)
      if data is None:
          await upsert_game_platform_enrichment(game_platform_id, opencritic_cached_at="FAILED")
          return None

      score = data.get("topCriticScore")
      tier = data.get("tier")
      percent_rec = data.get("percentRecommended")

      # topCriticScore can be -1 when no reviews yet
      if score is not None and score < 0:
          score = None

      fields = {
          "opencritic_id": oc_id,
          "opencritic_score": score,
          "opencritic_tier": tier,
          "opencritic_percent_rec": percent_rec,
          "opencritic_cached_at": now,
      }
      await upsert_game_platform_enrichment(game_platform_id, **fields)
      return fields


  async def _search_game(name: str) -> int | None:
      try:
          async with httpx.AsyncClient(timeout=10) as client:
              resp = await client.get(_SEARCH_URL, params={"criteria": name})
              resp.raise_for_status()
              results = resp.json()
      except Exception as exc:
          logger.debug("OpenCritic search failed for %r: %s", name, exc)
          return None

      if not results:
          return None

      # Pick best name match
      from .db import extract_best_fuzzy_key
      choices = {item["id"]: item["name"] for item in results if "id" in item and "name" in item}
      best_id = extract_best_fuzzy_key(name, choices, cutoff=70)
      return best_id if best_id is not None else results[0].get("id")


  async def _fetch_game(oc_id: int) -> dict | None:
      try:
          async with httpx.AsyncClient(timeout=10) as client:
              resp = await client.get(_GAME_URL.format(id=oc_id))
              resp.raise_for_status()
              return resp.json()
      except Exception as exc:
          logger.debug("OpenCritic game fetch failed for id %d: %s", oc_id, exc)
          return None
  ```

- [ ] **Step 2: Update `tools/detail.py` import** (old import `from ..data.opencritic import get_metacritic` must be removed)

  In `tools/detail.py`, remove:
  ```python
  from ..data.opencritic import get_metacritic
  ```
  And remove the call `await get_metacritic(game_id)` — we'll update that tool fully in Task 9.

- [ ] **Step 3: Verify import works**

  ```bash
  python -c "from gamelib_mcp.data.opencritic import enrich_opencritic; print('OK')"
  ```

  Expected: `OK`

- [ ] **Step 4: Commit**

  ```bash
  git add gamelib_mcp/data/opencritic.py gamelib_mcp/tools/detail.py
  git commit -m "feat: OpenCritic API client writing to game_platform_enrichment"
  ```

---

## Task 7: Metacritic Scraper

**Files:** Create `gamelib_mcp/data/metacritic.py`

- [ ] **Step 1: Create the module**

  ```python
  """Platform-aware Metacritic scraper — writes to game_platform_enrichment."""

  import logging
  import re
  from datetime import datetime, timezone

  import httpx
  from bs4 import BeautifulSoup

  from .db import upsert_game_platform_enrichment

  logger = logging.getLogger(__name__)

  METACRITIC_CACHE_DAYS = 30

  # Our platform value → Metacritic URL path segment
  _PLATFORM_SLUG: dict[str, str] = {
      "steam": "pc",
      "epic": "pc",
      "gog": "pc",
      "ps5": "playstation-5",
      "switch2": "switch",
  }

  _SEARCH_URL = "https://www.metacritic.com/search/{query}/"
  _GAME_URL = "https://www.metacritic.com/game/{slug}/"

  _HEADERS = {
      "User-Agent": (
          "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
      ),
      "Accept": "text/html,application/xhtml+xml",
      "Accept-Language": "en-US,en;q=0.9",
  }


  def _is_fresh(cached_at: str | None) -> bool:
      if not cached_at:
          return False
      if cached_at == "FAILED":
          return True  # don't retry; background job will skip
      try:
          dt = datetime.fromisoformat(cached_at)
          return (datetime.now(timezone.utc) - dt).total_seconds() < METACRITIC_CACHE_DAYS * 86400
      except ValueError:
          return False


  def _to_slug(name: str) -> str:
      """Convert game name to Metacritic URL slug."""
      slug = name.lower()
      slug = re.sub(r"[^a-z0-9\s-]", "", slug)
      slug = re.sub(r"\s+", "-", slug.strip())
      slug = re.sub(r"-+", "-", slug)
      return slug


  async def _fetch_score_from_url(url: str) -> tuple[int | None, str]:
      """
      Fetch a Metacritic game page and extract the Metascore.
      Returns (score, final_url). Score is None if not found.
      """
      try:
          async with httpx.AsyncClient(
              timeout=15,
              follow_redirects=True,
              headers=_HEADERS,
          ) as client:
              resp = await client.get(url)
              if resp.status_code == 404:
                  return None, url
              resp.raise_for_status()
              html = resp.text
              final_url = str(resp.url)
      except Exception as exc:
          logger.debug("Metacritic fetch failed for %s: %s", url, exc)
          return None, url

      soup = BeautifulSoup(html, "html.parser")

      # Try JSON-LD structured data first (more reliable than HTML scraping)
      for script in soup.find_all("script", type="application/ld+json"):
          try:
              import json
              data = json.loads(script.string or "")
              # aggregateRating.ratingValue appears on game pages
              rating = (data.get("aggregateRating") or {}).get("ratingValue")
              if rating is not None:
                  return int(float(rating)), final_url
          except Exception:
              continue

      # Fallback: look for score in common Metacritic CSS classes
      for selector in [
          '[data-testid="score-meta-critic"]',
          ".c-siteReviewScore",
          ".metascore_w",
      ]:
          el = soup.select_one(selector)
          if el:
              text = el.get_text(strip=True)
              m = re.search(r"\d+", text)
              if m:
                  score = int(m.group())
                  if 0 < score <= 100:
                      return score, final_url

      return None, final_url


  async def enrich_metacritic(
      game_platform_id: int,
      game_name: str,
      platform: str,
  ) -> dict | None:
      """
      Scrape Metacritic score for game_name on platform and cache in game_platform_enrichment.
      Tries PS5 then PS4 for PSN titles. Returns enrichment dict or None.
      """
      from .db import get_db

      async with get_db() as db:
          row = await db.execute_fetchone(
              "SELECT metacritic_cached_at FROM game_platform_enrichment WHERE game_platform_id = ?",
              (game_platform_id,),
          )
      cached_at = row["metacritic_cached_at"] if row else None
      if _is_fresh(cached_at):
          return None

      now = datetime.now(timezone.utc).isoformat()
      slug = _to_slug(game_name)

      platforms_to_try = [_PLATFORM_SLUG.get(platform, "pc")]
      # For ps5 platform, also try ps4 as fallback
      if platform == "ps5" and "playstation-4" not in platforms_to_try:
          platforms_to_try.append("playstation-4")

      score: int | None = None
      final_url = ""

      for plat_slug in platforms_to_try:
          url = _GAME_URL.format(slug=slug)
          candidate_score, candidate_url = await _fetch_score_from_url(url)
          if candidate_score is not None:
              score = candidate_score
              final_url = candidate_url
              break

      if score is None:
          await upsert_game_platform_enrichment(
              game_platform_id, metacritic_cached_at="FAILED"
          )
          return None

      fields = {
          "metacritic_score": score,
          "metacritic_url": final_url,
          "metacritic_cached_at": now,
      }
      await upsert_game_platform_enrichment(game_platform_id, **fields)
      return fields
  ```

- [ ] **Step 2: Verify the import works**

  ```bash
  python -c "from gamelib_mcp.data.metacritic import enrich_metacritic; print('OK')"
  ```

  Expected: `OK`

- [ ] **Step 3: Commit**

  ```bash
  git add gamelib_mcp/data/metacritic.py
  git commit -m "feat: platform-aware Metacritic scraper writing to game_platform_enrichment"
  ```

---

## Task 8: Background Enrichment Phases 5 & 6

**Files:** Modify `gamelib_mcp/data/enrich_bg.py`

- [ ] **Step 1: Add OpenCritic and Metacritic imports**

  Add to the top of `enrich_bg.py`:
  ```python
  from .opencritic import enrich_opencritic
  from .metacritic import enrich_metacritic
  ```

  Add rate limit constants:
  ```python
  _OPENCRITIC_DELAY = 1.0
  _METACRITIC_DELAY = 2.0  # scraping — be polite
  ```

- [ ] **Step 2: Add Phase 5 and Phase 6 calls in `background_enrich`**

  Update `background_enrich` to call two new phases:

  ```python
  async def background_enrich() -> None:
      logger.info("Background enrichment started")

      store_count = await _enrich_store()
      logger.info("Background enrichment — store phase done: %d games enriched", store_count)

      hltb_count = await _enrich_hltb()
      logger.info("Background enrichment — HLTB phase done: %d games enriched", hltb_count)

      proton_count = await _enrich_protondb()
      logger.info("Background enrichment — ProtonDB phase done: %d games enriched", proton_count)

      steamspy_count = await _enrich_steamspy()
      logger.info("Background enrichment — SteamSpy phase done: %d games enriched", steamspy_count)

      opencritic_count = await _enrich_opencritic()
      logger.info("Background enrichment — OpenCritic phase done: %d rows enriched", opencritic_count)

      metacritic_count = await _enrich_metacritic()
      logger.info("Background enrichment — Metacritic phase done: %d rows enriched", metacritic_count)

      logger.info(
          "Background enrichment complete — store=%d hltb=%d protondb=%d steamspy=%d opencritic=%d metacritic=%d",
          store_count, hltb_count, proton_count, steamspy_count, opencritic_count, metacritic_count,
      )
  ```

- [ ] **Step 3: Implement `_enrich_opencritic`**

  Add after `_enrich_steamspy`:

  ```python
  async def _enrich_opencritic() -> int:
      """Fetch OpenCritic scores for all platform rows missing opencritic data."""
      count = 0
      while True:
          async with get_db() as db:
              rows = await db.execute_fetchall(
                  """SELECT gp.id AS game_platform_id, g.name
                     FROM game_platforms gp
                     JOIN games g ON g.id = gp.game_id
                     LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                     WHERE (gpe.opencritic_cached_at IS NULL)
                       AND g.is_farmed = 0
                     ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                     LIMIT 50"""
              )

          if not rows:
              break

          for row in rows:
              try:
                  await enrich_opencritic(row["game_platform_id"], row["name"])
                  count += 1
              except Exception as e:
                  logger.debug("OpenCritic enrich failed for %s: %s", row["name"], e)
              await asyncio.sleep(_OPENCRITIC_DELAY)

      return count
  ```

- [ ] **Step 4: Implement `_enrich_metacritic`**

  Add after `_enrich_opencritic`:

  ```python
  async def _enrich_metacritic() -> int:
      """Scrape Metacritic scores for all platform rows missing metacritic data."""
      count = 0
      while True:
          async with get_db() as db:
              rows = await db.execute_fetchall(
                  """SELECT gp.id AS game_platform_id, gp.platform, g.name
                     FROM game_platforms gp
                     JOIN games g ON g.id = gp.game_id
                     LEFT JOIN game_platform_enrichment gpe ON gpe.game_platform_id = gp.id
                     WHERE (gpe.metacritic_cached_at IS NULL)
                       AND g.is_farmed = 0
                     ORDER BY COALESCE(gp.playtime_minutes, 0) DESC
                     LIMIT 50"""
              )

          if not rows:
              break

          for row in rows:
              try:
                  await enrich_metacritic(
                      row["game_platform_id"],
                      row["name"],
                      row["platform"],
                  )
                  count += 1
              except Exception as e:
                  logger.debug("Metacritic enrich failed for %s: %s", row["name"], e)
              await asyncio.sleep(_METACRITIC_DELAY)

      return count
  ```

- [ ] **Step 5: Verify the module imports cleanly**

  ```bash
  python -c "from gamelib_mcp.data.enrich_bg import background_enrich; print('OK')"
  ```

  Expected: `OK`

- [ ] **Step 6: Commit**

  ```bash
  git add gamelib_mcp/data/enrich_bg.py
  git commit -m "feat: background enrichment phases 5 (OpenCritic) and 6 (Metacritic)"
  ```

---

## Task 9: Update `get_game_detail` Output

**Files:** Modify `gamelib_mcp/tools/detail.py`

- [ ] **Step 1: Rewrite `tools/detail.py`**

  The old file called `get_metacritic` (now removed) and reads `metacritic_score` from the top-level game row (no longer there). Replace the whole file:

  ```python
  """get_game_detail: full info for one game, with platform-aware output."""

  from ..data.db import (
      get_db,
      get_game_by_appid,
      get_steam_appid_for_game,
      load_platforms_for_games,
  )
  from ..data.hltb import get_hltb
  from ..data.protondb import get_protondb
  from ..data.steam_store import enrich_game
  from ..utils import _parse_json


  async def get_game_detail(
      name: str | None = None,
      appid: int | None = None,
      game_id: int | None = None,
  ) -> dict:
      """
      Return full detail for a game, triggering lazy enrichment.
      Accepts game_id, Steam appid, or a partial name.
      """
      async with get_db() as db:
          if game_id is not None:
              row = await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
          elif appid is not None:
              row = await get_game_by_appid(appid)
          elif name is not None:
              row = await db.execute_fetchone(
                  "SELECT * FROM games WHERE lower(name) LIKE lower(?) LIMIT 1",
                  (f"%{name}%",),
              )
          else:
              return {"error": "Provide game_id, name, or appid"}

      if row is None:
          return {"error": "Game not found in library"}

      game_id = row["id"]
      game_name = row["name"]
      steam_appid = await get_steam_appid_for_game(game_id)

      # Trigger lazy enrichment for Steam games
      if steam_appid is not None:
          await enrich_game(steam_appid)
          await get_protondb(steam_appid)
      await get_hltb(game_id, game_name)

      async with get_db() as db:
          row = await db.execute_fetchone("SELECT * FROM games WHERE id = ?", (game_id,))
          rating = await db.execute_fetchone(
              """SELECT source, raw_score, normalized_score, review_text
                 FROM ratings
                 WHERE game_id = ?
                 ORDER BY source
                 LIMIT 1""",
              (game_id,),
          )

      platforms = (await load_platforms_for_games([game_id])).get(game_id, [])
      steam_platform = next((p for p in platforms if p["platform"] == "steam"), None)
      steam_data = steam_platform["provider_data"] if steam_platform else {}

      total_playtime_minutes = sum(p["playtime_minutes"] or 0 for p in platforms)
      total_playtime_2weeks_minutes = sum(p["playtime_2weeks_minutes"] or 0 for p in platforms)

      result = {
          "game_id": row["id"],
          "appid": steam_appid,
          "name": row["name"],
          "release_date": row["release_date"],
          "platforms": platforms,
          "playtime_hours": round(total_playtime_minutes / 60, 1) if total_playtime_minutes else 0,
          "playtime_2weeks_hours": (
              round(total_playtime_2weeks_minutes / 60, 1)
              if total_playtime_2weeks_minutes
              else 0
          ),
          "last_played_date": steam_data.get("last_played_date"),
          "is_farmed": bool(row["is_farmed"]),
          "genres": _parse_json(row["genres"]),
          "tags": _parse_json(row["tags"]),
          "short_description": row["short_description"],
          "steam_review_score": steam_data.get("steam_review_score"),
          "steam_review_desc": steam_data.get("steam_review_desc"),
          "hltb_main": row["hltb_main"],
          "hltb_extra": row["hltb_extra"],
          "hltb_complete": row["hltb_complete"],
          "protondb_tier": steam_data.get("protondb_tier"),
      }

      if rating:
          result["my_rating"] = {
              "source": rating["source"],
              "raw_score": rating["raw_score"],
              "normalized_score": rating["normalized_score"],
              "review_text": rating["review_text"],
          }

      return result
  ```

  Note: `metacritic_score` and `opencritic_score` are now on each platform dict (added in Task 2 via `_platform_dict`), not at the top level. The AI consumer can read them from `result["platforms"][n]["metacritic_score"]` etc.

- [ ] **Step 2: Verify the tool runs end-to-end**

  ```bash
  python -c "
  import asyncio
  from gamelib_mcp.tools.detail import get_game_detail
  result = asyncio.run(get_game_detail(name='Elden Ring'))
  print('release_date:', result.get('release_date'))
  for p in result.get('platforms', []):
      print(p['platform'], '- metacritic:', p.get('metacritic_score'), '- opencritic:', p.get('opencritic_score'))
  "
  ```

  Expected: release_date is populated (or None for old entries), platform rows show metacritic/opencritic (or None until background enrichment runs).

- [ ] **Step 3: Commit**

  ```bash
  git add gamelib_mcp/tools/detail.py
  git commit -m "feat: get_game_detail exposes release_date and per-platform review scores"
  ```

---

## Task 10: Environment Configuration

**Files:** Modify `.env.example`

- [ ] **Step 1: Add IGDB credentials to `.env.example`**

  Add after `STEAM_API_KEY`:
  ```
  # IGDB (Twitch) credentials — for game identity resolution and metadata enrichment
  # Get from https://dev.twitch.tv/console (create an application, category = Website Integration)
  TWITCH_CLIENT_ID=
  TWITCH_CLIENT_SECRET=
  ```

- [ ] **Step 2: Commit**

  ```bash
  git add .env.example
  git commit -m "docs: add TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET to .env.example"
  ```

---

## Task 11: End-to-End Verification

- [ ] **Step 1: Start the server and check health**

  ```bash
  python -m gamelib_mcp.main &
  sleep 5
  curl -s http://localhost:8000/health | python -m json.tool
  kill %1
  ```

  Expected: JSON with `status: ok` (or equivalent), no Python exceptions in server output.

- [ ] **Step 2: Verify schema is correct**

  ```bash
  sqlite3 steam.db "
  .schema game_platform_enrichment
  SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;
  PRAGMA user_version;
  "
  ```

  Expected:
  - `game_platform_enrichment` table exists with all columns
  - `PRAGMA user_version` returns `3`
  - All expected tables present

- [ ] **Step 3: Verify a Steam game shows release_date and enrichment fields**

  ```bash
  python -c "
  import asyncio
  from gamelib_mcp.tools.detail import get_game_detail
  r = asyncio.run(get_game_detail(name='Elden Ring'))
  import json; print(json.dumps({k: v for k, v in r.items() if k != 'tags'}, indent=2))
  "
  ```

  Expected: `release_date` field present at top level; each platform dict contains `metacritic_score`, `opencritic_score`, `platform_release_date` (may be null until background enrichment runs).

- [ ] **Step 4: Final commit**

  ```bash
  git add -A
  git status  # confirm nothing unexpected
  git commit -m "feat: platform-aware enrichment — IGDB identity, per-platform review scores, release dates"
  ```
