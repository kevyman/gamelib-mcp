"""SQLite schema DDL for every migration target version.

Plain string constants only — no runtime dependencies. The migration chain in
the db package applies these; ``_V4_SCHEMA_DDL`` is a backward-compat alias for
the full v5 schema used to initialize fresh databases.
"""

_V1_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        appid            INTEGER UNIQUE,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        short_description TEXT,
        metacritic_score INTEGER,
        hltb_main        REAL,
        hltb_extra       REAL,
        hltb_complete    REAL,
        protondb_tier    TEXT,
        opencritic_score INTEGER,
        steam_review_score INTEGER,
        steam_review_desc  TEXT,
        store_enriched   INTEGER DEFAULT 0,
        store_enriched_at TEXT,
        store_cached_at  TEXT,
        hltb_cached_at   TEXT,
        metacritic_cached_at TEXT,
        protondb_cached_at TEXT,
        steamspy_cached_at TEXT,
        rtime_last_played INTEGER,
        is_farmed        INTEGER DEFAULT 0,
        library_updated_at TEXT
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
"""


_V2_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        short_description TEXT,
        metacritic_score INTEGER,
        hltb_main        REAL,
        hltb_extra       REAL,
        hltb_complete    REAL,
        opencritic_score INTEGER,
        hltb_cached_at   TEXT,
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


_V5_SCHEMA_DDL = """
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
        hltb_claimed_at  TEXT,
        igdb_cached_at   TEXT,
        igdb_claimed_at  TEXT,
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
        store_claimed_at    TEXT,
        protondb_cached_at  TEXT,
        protondb_claimed_at TEXT,
        steamspy_cached_at  TEXT,
        steamspy_claimed_at TEXT,
        rtime_last_played   INTEGER,
        library_updated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS game_platform_enrichment (
        game_platform_id       INTEGER PRIMARY KEY REFERENCES game_platforms(id) ON DELETE CASCADE,
        platform_release_date  TEXT,
        metacritic_score       INTEGER,
        metacritic_url         TEXT,
        metacritic_claimed_at  TEXT,
        opencritic_id          INTEGER,
        opencritic_url         TEXT,
        opencritic_score       INTEGER,
        opencritic_tier        TEXT,
        opencritic_percent_rec REAL,
        opencritic_num_reviews INTEGER,
        opencritic_cached_at   TEXT,
        opencritic_claimed_at  TEXT,
        metacritic_cached_at   TEXT
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
"""

# Alias used for fresh database initialization and final reconciliation.
# The DDL above already contains all v5 columns (opencritic_url,
# opencritic_num_reviews, metacritic_cached_at), so both names refer to the
# same full schema.
_V4_SCHEMA_DDL = _V5_SCHEMA_DDL  # backward-compat alias; kept for external references

# v6 introduces no structural change — it is a data-only migration that clears
# Metacritic user-score contamination and normalizes HLTB "no data" zeros to
# NULL — so its schema is identical to v5.
_V6_SCHEMA_DDL = _V5_SCHEMA_DDL
