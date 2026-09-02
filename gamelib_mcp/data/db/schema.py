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


_V7_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        name_normalized  TEXT,
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
    CREATE INDEX IF NOT EXISTS idx_games_name_normalized ON games(name_normalized);
    CREATE INDEX IF NOT EXISTS idx_ratings_game_id ON ratings(game_id);
"""


_V8_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        name_normalized  TEXT,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        features         TEXT,
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
    CREATE INDEX IF NOT EXISTS idx_games_name_normalized ON games(name_normalized);
    CREATE INDEX IF NOT EXISTS idx_ratings_game_id ON ratings(game_id);
"""


# v9 adds games.manual_overrides: a JSON array of column names set via the
# update_game tool. Background sync/enrichment must not clobber these columns.
#
# v10 (defined below) appends normalized series tables (game_series +
# game_series_membership) for IGDB collections/franchises.
_V9_SCHEMA_DDL = """
    CREATE TABLE IF NOT EXISTS games (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id          INTEGER UNIQUE,
        name             TEXT NOT NULL,
        name_normalized  TEXT,
        sort_name        TEXT,
        release_date     TEXT,
        genres           TEXT,
        tags             TEXT,
        features         TEXT,
        short_description TEXT,
        hltb_main        REAL,
        hltb_extra       REAL,
        hltb_complete    REAL,
        hltb_cached_at   TEXT,
        hltb_claimed_at  TEXT,
        igdb_cached_at   TEXT,
        igdb_claimed_at  TEXT,
        is_farmed        INTEGER NOT NULL DEFAULT 0,
        manual_overrides TEXT
    );

    CREATE TABLE IF NOT EXISTS game_platforms (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id          INTEGER NOT NULL REFERENCES games(id),
        platform         TEXT NOT NULL,
        owned            INTEGER NOT NULL DEFAULT 1,
        playtime_minutes INTEGER,
        playtime_2weeks_minutes INTEGER,
        last_played      TEXT,
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
    CREATE INDEX IF NOT EXISTS idx_games_name_normalized ON games(name_normalized);
    CREATE INDEX IF NOT EXISTS idx_ratings_game_id ON ratings(game_id);
"""


# v10 adds normalized series tracking (IGDB collections + franchises) with a
# many-to-many membership junction.
_V10_SCHEMA_DDL = _V9_SCHEMA_DDL + """
    CREATE TABLE IF NOT EXISTS game_series (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        igdb_id INTEGER,
        kind    TEXT NOT NULL,
        name    TEXT NOT NULL,
        UNIQUE(kind, igdb_id)
    );

    CREATE TABLE IF NOT EXISTS game_series_membership (
        game_id   INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        series_id INTEGER NOT NULL REFERENCES game_series(id) ON DELETE CASCADE,
        PRIMARY KEY (game_id, series_id)
    );

    CREATE INDEX IF NOT EXISTS idx_gsm_game ON game_series_membership(game_id);
    CREATE INDEX IF NOT EXISTS idx_gsm_series ON game_series_membership(series_id);
"""


# v11 adds content relationship metadata for DLC/expansions/editions and a
# normalized alias table for package/storefront names that should resolve to a
# canonical parent game.
_V11_SCHEMA_DDL = _V10_SCHEMA_DDL.replace(
    "        is_farmed        INTEGER NOT NULL DEFAULT 0,\n"
    "        manual_overrides TEXT",
    "        is_farmed        INTEGER NOT NULL DEFAULT 0,\n"
    "        content_type     TEXT NOT NULL DEFAULT 'base_game',\n"
    "        parent_game_id   INTEGER REFERENCES games(id) ON DELETE SET NULL,\n"
    "        is_primary_library_item INTEGER NOT NULL DEFAULT 1,\n"
    "        manual_overrides TEXT",
) + """
    CREATE TABLE IF NOT EXISTS game_aliases (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        alias            TEXT NOT NULL,
        alias_normalized TEXT NOT NULL,
        alias_type       TEXT NOT NULL,
        source           TEXT,
        source_key       TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_games_parent_game_id ON games(parent_game_id);
    CREATE INDEX IF NOT EXISTS idx_games_primary_library_item ON games(is_primary_library_item);
    CREATE INDEX IF NOT EXISTS idx_game_aliases_game_id ON game_aliases(game_id);
    CREATE INDEX IF NOT EXISTS idx_game_aliases_normalized ON game_aliases(alias_normalized);
    CREATE UNIQUE INDEX IF NOT EXISTS idx_game_aliases_unique
        ON game_aliases(game_id, alias_normalized, alias_type, COALESCE(source, ''), COALESCE(source_key, ''));
"""


# v12 adds nintendo_play_summary: per-(device, application, period) playtime from
# the Nintendo Switch Parental Controls API. This is the source of the switch2
# *playtime* total — VGCS ownership sync provides ownership, Parental Controls
# provides minutes. period_type 'day' (finalized daily summaries) is the v1
# source of truth; 'month' is reserved for later backfill without a new
# migration. Additive table only — no existing-data migration needed.
_V12_SCHEMA_DDL = _V11_SCHEMA_DDL + """
    CREATE TABLE IF NOT EXISTS nintendo_play_summary (
        device_id        TEXT NOT NULL,
        application_id   TEXT NOT NULL,
        period_type      TEXT NOT NULL,
        period_key       TEXT NOT NULL,
        playtime_minutes INTEGER NOT NULL,
        app_name         TEXT,
        updated_at       TEXT,
        PRIMARY KEY (device_id, application_id, period_type, period_key)
    );

    CREATE INDEX IF NOT EXISTS idx_nps_app ON nintendo_play_summary(application_id);
"""


# v16 adds game_wishlist: "want to play" tracking, deliberately kept OUT of
# game_platforms. That table's rows mean "a real relationship with this
# platform exists" (owned, or a manual stub); a wishlist item may not be owned
# anywhere yet, so overloading owned=0 there would blur that invariant and risk
# a sync accidentally un-owning a row. A wishlist row is deleted once ownership
# sync confirms the game is actually owned on that platform (see
# clear_fulfilled_wishlist_entries) — the same "purchase clears the wishlist"
# behavior storefronts like Steam implement natively.
_V16_SCHEMA_DDL = _V12_SCHEMA_DDL + """
    CREATE TABLE IF NOT EXISTS game_wishlist (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        platform      TEXT NOT NULL,
        wishlisted_at TEXT NOT NULL,
        source        TEXT,
        UNIQUE(game_id, platform)
    );

    CREATE INDEX IF NOT EXISTS idx_game_wishlist_game_id ON game_wishlist(game_id);
"""

# v17 adds scrape_config: versioned, DB-backed overrides for the declarative
# scrape descriptors in data/scrape_config.py (URL templates, selectors, JSON
# paths, TTLs). Rows are append-only versions per provider; at most one row per
# provider is 'active' at a time, and code-level defaults always remain the
# implicit version 0 (an empty table means "run on defaults"). status values:
# active | pending (awaiting approve_scrape_config) | superseded (replaced by a
# newer active) | rolled_back. config_json holds the (possibly partial)
# override dict; validation_report records the check results that admitted it.
_V17_SCHEMA_DDL = _V16_SCHEMA_DDL + """
    CREATE TABLE IF NOT EXISTS scrape_config (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        provider          TEXT NOT NULL,
        version           INTEGER NOT NULL,
        config_json       TEXT NOT NULL,
        status            TEXT NOT NULL CHECK (status IN ('active', 'pending', 'superseded', 'rolled_back')),
        source            TEXT NOT NULL DEFAULT 'manual',
        note              TEXT,
        validation_report TEXT,
        created_at        TEXT NOT NULL,
        UNIQUE(provider, version)
    );

    CREATE INDEX IF NOT EXISTS idx_scrape_config_provider_status
        ON scrape_config(provider, status);
"""

# v18 adds game_prices (current-price cache per game+platform+shop, refreshed
# by get_wishlist_deals / the price sync — rows are overwritten, not appended;
# ITAD is the historical source of record so we don't keep history here) and
# game_wishlist.store_identifier (the store's own id captured at wishlist-sync
# time, e.g. a Steam appid — needed because an unowned wishlist item has no
# game_platforms row to carry a game_platform_identifiers entry).
_V18_SCHEMA_DDL = _V17_SCHEMA_DDL.replace(
    "        source        TEXT,",
    "        source        TEXT,\n        store_identifier TEXT,",
) + """
    CREATE TABLE IF NOT EXISTS game_prices (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id       INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        platform      TEXT NOT NULL,
        shop          TEXT NOT NULL,
        price         REAL,
        regular_price REAL,
        cut_pct       INTEGER,
        currency      TEXT,
        deal_url      TEXT,
        fetched_at    TEXT NOT NULL,
        UNIQUE(game_id, platform, shop)
    );

    CREATE INDEX IF NOT EXISTS idx_game_prices_game_id ON game_prices(game_id);
"""

# v19 adds games.igdb_platforms: the full set of IGDB platform ids a game is
# released on (JSON int array; NULL = not yet fetched), written by IGDB
# enrichment regardless of ownership. Powers cross-platform availability in
# get_wishlist_deals ("this Steam wishlist item also has a Switch release").
_V19_SCHEMA_DDL = _V18_SCHEMA_DDL.replace(
    "        igdb_cached_at   TEXT,",
    "        igdb_cached_at   TEXT,\n        igdb_platforms   TEXT,",
)

# v20 adds games.completion_status: user-declared play status. NULL means
# "unset — infer from playtime as before" (see tools/common.py PLAY_STATE_SQL).
# It is user-set only (update_game): no sync or enrichment writer touches it,
# so unlike other games columns it needs no manual_overrides guard to survive
# syncs — the override entry it still gets from update_game is just bookkeeping.
_V20_SCHEMA_DDL = _V19_SCHEMA_DDL.replace(
    "        is_farmed        INTEGER NOT NULL DEFAULT 0,",
    "        is_farmed        INTEGER NOT NULL DEFAULT 0,\n"
    "        completion_status TEXT CHECK (completion_status IN ('playing', 'completed', 'abandoned')),",
)

# v21 adds play_history: cumulative per-(game, platform) playtime snapshots,
# at most one row per UTC day, written after each platform sync only when the
# total changed. Deltas ("what did I play this month") are derived at read
# time; switch2 windows are served from nintendo_play_summary's real daily
# rows instead (see data/db/history.py). Forward-only, like
# nintendo_play_summary — there is no retroactive source to backfill from.
_V21_SCHEMA_DDL = _V20_SCHEMA_DDL + """
    CREATE TABLE IF NOT EXISTS play_history (
        game_id          INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        platform         TEXT NOT NULL,
        snapshot_date    TEXT NOT NULL,
        playtime_minutes INTEGER NOT NULL,
        PRIMARY KEY (game_id, platform, snapshot_date)
    );

    CREATE INDEX IF NOT EXISTS idx_play_history_date ON play_history(snapshot_date);
"""

# v22 widens the games.completion_status CHECK to add 'evergreen' (endless
# games with no completion concept: Rocket League, Tabletop Simulator, MMOs,
# sandboxes). SQLite cannot ALTER a CHECK constraint in place; existing rows
# whose games table was rebuilt fresh (and therefore still carries the old
# CHECK — see _migrate_v22_to_v23) need a table rebuild to accept the new
# value. DBs that only ever ran the v20->v21 ALTER TABLE ADD COLUMN path have
# no CHECK at all and need no repair.
_V22_SCHEMA_DDL = _V21_SCHEMA_DDL.replace(
    "completion_status TEXT CHECK (completion_status IN ('playing', 'completed', 'abandoned')),",
    "completion_status TEXT CHECK (completion_status IN "
    "('playing', 'completed', 'abandoned', 'evergreen')),",
)

# v25 adds games.cover_image_id: the IGDB cover image slug (an images.igdb.com
# URL path segment, e.g. "co1wyy"), written during IGDB enrichment. NULL means
# "not fetched yet". Read-side cover URLs fall back to the Steam library
# capsule by appid (tools/common.py cover_url), so Steam games render covers
# even before this backfills; only non-Steam games strictly depend on it.
# (v23 and v24 were data-only migrations — no DDL constants exist for them.)
_V25_SCHEMA_DDL = _V22_SCHEMA_DDL.replace(
    "        igdb_platforms   TEXT,",
    "        igdb_platforms   TEXT,\n        cover_image_id   TEXT,",
)

# v29 adds per-ownership acquisition tracking to game_platforms: when and how
# a game was acquired on that platform, what was paid, and (for multi-game
# bundles) which bundle it came from. price_paid holds the per-game allocation
# of a bundle's total; bundle_name groups the members. No sync writer ever
# touches these columns (all sync SQL enumerates its columns explicitly) —
# they are supplied only by set_acquisition / set_acquisitions_batch /
# import_purchases / add_game_to_platform.
# (v26-v28 were data-only migrations — no DDL constants exist for them.)
_V29_SCHEMA_DDL = _V25_SCHEMA_DDL.replace(
    "        last_synced      TEXT,\n        UNIQUE(game_id, platform)",
    "        last_synced      TEXT,\n"
    "        acquired_at      TEXT,\n"
    "        price_paid       REAL,\n"
    "        price_currency   TEXT,\n"
    "        purchase_source  TEXT,\n"
    "        bundle_name      TEXT,\n"
    "        UNIQUE(game_id, platform)",
)

# v31 adds game_platforms.manual_overrides: a JSON array of column names on this
# ownership row that were set by hand (via set_playtime) and must survive future
# platform syncs. Mirrors games.manual_overrides but is keyed per game_platforms
# row. Unlike the acquisition columns above (which no sync writer references),
# playtime/last_played ARE written by sync, so the sync write paths
# (upsert_game_platform, bulk_upsert_steam_library) consult this column and skip
# the protected columns. NULL means "nothing pinned".
# (v30 was a data-only migration — no DDL constant exists for it.)
_V31_SCHEMA_DDL = _V29_SCHEMA_DDL.replace(
    "        bundle_name      TEXT,\n        UNIQUE(game_id, platform)",
    "        bundle_name      TEXT,\n"
    "        manual_overrides TEXT,\n"
    "        UNIQUE(game_id, platform)",
)

# v32 adds game_platforms.delisted: ownership confirmed via the account's
# license list (Steam dynamicstore userdata) for an app the public owned-games
# API no longer returns — typically a retired/delisted store app (e.g. Burnout
# Paradise: The Ultimate Box). Rows are minted by the Steam license audit with
# delisted=1; a later appearance in GetOwnedGames clears the flag (delistings
# are reversed: GTA IV's Complete Edition superseded the retired standalone).
# Column is platform-agnostic by design — other stores retire titles too.
_V32_SCHEMA_DDL = _V31_SCHEMA_DDL.replace(
    "        manual_overrides TEXT,\n        UNIQUE(game_id, platform)",
    "        manual_overrides TEXT,\n"
    "        delisted         INTEGER NOT NULL DEFAULT 0,\n"
    "        UNIQUE(game_id, platform)",
)

# v33 adds query_log: an audit trail of every query_library() call (success or
# error), written by tools/query.py through the normal RW connection path —
# the read-only query connection itself can never write here. No index beyond
# the PK; this is a log, not a lookup table.
_V33_SCHEMA_DDL = (
    _V32_SCHEMA_DDL
    + """
    CREATE TABLE IF NOT EXISTS query_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        sql        TEXT NOT NULL,
        row_count  INTEGER,
        truncated  INTEGER,
        elapsed_ms INTEGER,
        error      TEXT
    );
"""
)

# v34 adds the two ownership-lifecycle columns on game_platforms:
#
#   unowned_at — ownership on this platform ENDED (a refund, a revoked key, a
#   lapsed subscription title). The row is kept, with owned=0, so the spend /
#   duplication / platform-count aggregates (all of which already filter
#   owned=1) drop it while its acquisition history survives. Deleting the game
#   was the only prior remedy and it cascades every OTHER platform's playtime
#   away with it. Written only by add_game_to_platform(unowned_at=…), which
#   also pins `owned` in manual_overrides so a source that keeps listing the
#   title (Xbox ownership is title HISTORY, which never forgets) can't silently
#   re-own it.
#
#   last_seen_in_source — the last sync that actually RETURNED this row from
#   the platform's own source, as opposed to last_synced, which any write to
#   the row touches. The two answer different questions: a row not returned
#   this run is not evidence of a refund (a dropped page in the source's
#   pagination looks identical), so nothing acts on this column automatically —
#   check_library's ownership.unseen_in_source reports rows the source has
#   stopped returning across N consecutive SUCCESSFUL syncs and leaves the call
#   to a human. NULL = never seen in a source: a hand-added row, or any row
#   predating this column (deliberately not backfilled — stamping every
#   existing row would assert evidence no sync ever produced).
_V34_SCHEMA_DDL = _V33_SCHEMA_DDL.replace(
    "        delisted         INTEGER NOT NULL DEFAULT 0,\n        UNIQUE(game_id, platform)",
    "        delisted         INTEGER NOT NULL DEFAULT 0,\n"
    "        unowned_at       TEXT,\n"
    "        last_seen_in_source TEXT,\n"
    "        UNIQUE(game_id, platform)",
)

# v36 freezes the platform's last-played date INTO each snapshot. The history
# gate (tools/history.py) needs to know when the game was last played *as of
# that snapshot*, and game_platforms.last_played is mutable: reading it would
# make a historical window's answer change the next time the game is launched,
# un-suppressing a correction that was correctly suppressed before. A snapshot
# is an immutable observation, so the date it was observed with belongs on it.
# (v35 was data-only — the Steam last_played backfill — so it reuses v34's shape.)
_V36_SCHEMA_DDL = _V34_SCHEMA_DDL.replace(
    "        playtime_minutes INTEGER NOT NULL,\n        PRIMARY KEY (game_id, platform, snapshot_date)",
    "        playtime_minutes INTEGER NOT NULL,\n"
    "        last_played      TEXT,\n"
    "        PRIMARY KEY (game_id, platform, snapshot_date)",
)

# v37 adds game_assessments: the recorded COMPONENTS of a game-quality verdict
# (ADR 0006 decision 5) — craft numbers, fit call, anchors cited, price seen,
# the verdict itself. Append-only history with at most one row per (game, UTC
# day): a re-record on the same day REPLACES that day's row through the
# expression unique index below, the same "≤1 row per day" convention
# play_history uses, so an assessment refined mid-conversation doesn't mint a
# second verdict.
#
# The row carries steam_appid itself because an assessed candidate may be
# unowned: identifiers hang off game_platforms, so a bare games row minted for
# a candidate has nowhere to keep one (the same reason game_wishlist carries
# store_identifier). instead_game_id is ON DELETE SET NULL — the "play what you
# own instead: X" link is evidence about the verdict, not a dependency of it.
#
# HARD CONSTRAINT (ADR 0006): nothing here ever feeds tag_affinity or
# discover_games. Verdicts are model output; mining them back into ranking
# would be a self-reinforcement loop. Read-only context + calibration only.
_V37_SCHEMA_DDL = (
    _V36_SCHEMA_DDL
    + """
    CREATE TABLE IF NOT EXISTS game_assessments (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        game_id                  INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
        assessed_at              TEXT NOT NULL,
        verdict                  TEXT NOT NULL CHECK (verdict IN
                                     ('buy_now', 'wishlist_for_sale', 'try_demo',
                                      'skip', 'play_what_you_own')),
        summary                  TEXT,
        craft_adjusted           REAL,
        craft_positive_pct       REAL,
        review_count             INTEGER,
        recent_trajectory        TEXT CHECK (recent_trajectory IN
                                     ('improving', 'stable', 'regressing')),
        opencritic_score         REAL,
        fit_call                 TEXT CHECK (fit_call IN
                                     ('strong fit', 'probable fit', 'coin flip',
                                      'probable miss')),
        anchors_cited            TEXT,
        flags                    TEXT,
        price_seen               REAL,
        price_currency           TEXT,
        price_platform           TEXT,
        target_price             REAL,
        instead_game_id          INTEGER REFERENCES games(id) ON DELETE SET NULL,
        steam_appid              INTEGER,
        context                  TEXT,
        owned_at_assessment      INTEGER NOT NULL DEFAULT 0,
        wishlisted_at_assessment INTEGER NOT NULL DEFAULT 0
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_game_assessments_game_day
        ON game_assessments(game_id, date(assessed_at));
    CREATE INDEX IF NOT EXISTS idx_game_assessments_game_id
        ON game_assessments(game_id);
"""
)

# v38 records the METHODOLOGY behind each verdict: which skill recorded it, at
# which version, declared by which model. All three are DECLARED-ONLY — a claim
# the recording client made about itself — and NULL means unknown. The server
# never default-stamps its own idea of "current": a stale installed copy of the
# skill, or an ad-hoc assessment made without one, must not inherit the
# canonical version this repo happens to ship, or the columns would answer a
# question nobody asked and calibration would compare methodologies that were
# never used.
#
# Free text, no CHECK and no index: new skills and new models must not need a
# schema change, and the table is small enough that calibration scans it
# anyway. `model` is whatever the assessing client's environment declared,
# verbatim and lowercased — expect FAMILY-level values from ChatGPT ("gpt-5"),
# because a router's fast/thinking variant is not reliably visible to the model
# itself; a guessed variant would be worse than the family.
_V38_SCHEMA_DDL = _V37_SCHEMA_DDL.replace(
    "        owned_at_assessment      INTEGER NOT NULL DEFAULT 0,",
    "        skill                    TEXT,\n"
    "        skill_version            TEXT,\n"
    "        model                    TEXT,\n"
    "        owned_at_assessment      INTEGER NOT NULL DEFAULT 0,",
)

# v39 adds game_assessments.presentation: the model-authored presentation of a
# verdict (elevator pitch, "for you if" / "not for you if" bullets, and the
# lineage comparisons) as ONE JSON object rather than four columns — it is a
# display payload read back whole, never filtered or joined on, and a fifth
# bullet kind must not need a migration.
#
# Declared content, exactly like the v38 provenance columns: whatever the
# recording client authored, capped and lightly normalized, never synthesized
# here. NULL means the recorder wrote no presentation. And like every other
# column on this table it stays out of tag_affinity and discover_games.
_V39_SCHEMA_DDL = _V38_SCHEMA_DDL.replace(
    "        owned_at_assessment      INTEGER NOT NULL DEFAULT 0,",
    "        presentation             TEXT,\n"
    "        owned_at_assessment      INTEGER NOT NULL DEFAULT 0,",
)

# v40 adds the two things about a price that the model cannot work out from
# the library, and the bookkeeping that lets an alert fire on them once.
#
# game_prices.history_low / history_low_currency: ITAD's all-time low for the
# game (`historyLow.all`). It is still not price HISTORY — one number, cached
# beside the current price and overwritten with it — but it is the difference
# between "50% off" and "cheaper than it has ever been". Its currency is stored
# separately and never assumed to equal the deal's: nothing in this codebase
# converts currencies, so a comparison across two of them is refused, not
# approximated.
#
# game_prices.deal_ends_at: when the winning deal expires (nullable in ITAD's
# payload — plenty of prices are open-ended). Sale-window urgency.
#
# game_wishlist.last_alerted_at / last_alert_key: the debounce for
# deal_alerts.py. The KEY is what makes it a debounce rather than a mute — it
# encodes the event (target reached / all-time low) *and* the price that
# triggered it, so the same deal never repeats while a further drop mints a new
# key and alerts again. NULL = never alerted, which is the honest state for
# every row predating this column.
_V40_SCHEMA_DDL = _V39_SCHEMA_DDL.replace(
    "        deal_url      TEXT,\n        fetched_at    TEXT NOT NULL,",
    "        deal_url      TEXT,\n"
    "        history_low   REAL,\n"
    "        history_low_currency TEXT,\n"
    "        deal_ends_at  TEXT,\n"
    "        fetched_at    TEXT NOT NULL,",
).replace(
    "        store_identifier TEXT,\n        UNIQUE(game_id, platform)",
    "        store_identifier TEXT,\n"
    "        last_alerted_at TEXT,\n"
    "        last_alert_key TEXT,\n"
    "        UNIQUE(game_id, platform)",
)

# Semantic views backing query_library()/get_db_schema() — NOT part of the
# versioned schema chain (like _FTS_DDL below). Dropped and recreated on every
# migrate_db run via _sync_query_views so a view definition change deploys on
# the next restart without a schema-version bump; views are cheap to rebuild.
#
# v_game_playtime unifies per-(game, platform) playtime: switch2 totals are
# authoritatively SUM(nintendo_play_summary.playtime_minutes) joined through
# the game's nintendo_title_id identifier (the same join as tools/history.py's
# _SWITCH2_DELTA_SQL: game_platform_identifiers.identifier_type =
# 'nintendo_title_id', identifier_value = nintendo_play_summary.application_id
# — plain equality, since both are normalized to uppercase at ingest; see
# data/db/__init__.py::normalize_identifier_value), joined back to the
# game_platforms row via game_platform_id) — including the
# manual-baseline sentinel device row, which represents real pre-tracking
# playtime and belongs in a pure SUM (see set_switch2_playtime_baseline).
# Two deliberate exceptions mirror how the PCTL sync itself writes
# game_platforms.playtime_minutes (it recomputes the same SUM through
# upsert_game_platform, which honors set_playtime pins):
#   * a switch2 row whose playtime_minutes is pinned in gp.manual_overrides
#     keeps the pinned gp value — the pin outranks the summary SUM everywhere
#     else in the codebase, so it must here too;
#   * a switch2 row with NO summary rows (e.g. added manually with a known
#     playtime, never seen by a PCTL sync) falls back to gp.playtime_minutes
#     via COALESCE instead of reporting NULL.
# Every other platform passes through game_platforms.playtime_minutes as-is.
_QUERY_VIEWS_DDL = """
DROP VIEW IF EXISTS v_game_playtime;
CREATE VIEW v_game_playtime AS
SELECT
    gp.game_id  AS game_id,
    gp.platform AS platform,
    CASE
        WHEN gp.platform = 'switch2'
             AND NOT EXISTS (
                 SELECT 1 FROM json_each(COALESCE(gp.manual_overrides, '[]'))
                 WHERE json_each.value = 'playtime_minutes'
             )
        THEN COALESCE(
            (
                SELECT SUM(nps.playtime_minutes)
                FROM nintendo_play_summary nps
                JOIN game_platform_identifiers gpi
                  ON gpi.identifier_type = 'nintendo_title_id'
                 AND gpi.identifier_value = nps.application_id
                WHERE gpi.game_platform_id = gp.id
            ),
            gp.playtime_minutes
        )
        ELSE gp.playtime_minutes
    END AS playtime_minutes
FROM game_platforms gp;

DROP VIEW IF EXISTS v_owned_games;
CREATE VIEW v_owned_games AS
SELECT
    g.id                       AS game_id,
    g.name                     AS name,
    gp.platform                AS platform,
    g.content_type             AS content_type,
    g.is_primary_library_item  AS is_primary_library_item,
    g.completion_status        AS completion_status,
    vgp.playtime_minutes       AS playtime_minutes,
    gp.last_played             AS last_played,
    gp.acquired_at             AS acquired_at,
    gp.price_paid              AS price_paid,
    gp.price_currency          AS price_currency,
    gp.purchase_source         AS purchase_source,
    gp.bundle_name             AS bundle_name,
    gp.delisted                AS delisted
FROM games g
JOIN game_platforms gp ON gp.game_id = g.id
JOIN v_game_playtime vgp ON vgp.game_id = gp.game_id AND vgp.platform = gp.platform
WHERE gp.owned = 1;
"""

# Derived search index — NOT part of the versioned schema chain. Created and
# fully resynced by _run_migrations' _sync_fts_index on every migrate_db run,
# which self-heals after destructive games-table rebuilds (those drop the
# triggers with the old table). Indexes COALESCE(name_normalized, lower(name))
# to mirror tools.search.NORMALIZED_NAME_SQL for backfill-pending rows.
_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS games_fts USING fts5(
    name_normalized,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS games_fts_ai AFTER INSERT ON games BEGIN
    INSERT INTO games_fts(rowid, name_normalized)
    VALUES (new.id, COALESCE(new.name_normalized, lower(new.name)));
END;

CREATE TRIGGER IF NOT EXISTS games_fts_au
AFTER UPDATE OF name, name_normalized ON games BEGIN
    DELETE FROM games_fts WHERE rowid = old.id;
    INSERT INTO games_fts(rowid, name_normalized)
    VALUES (new.id, COALESCE(new.name_normalized, lower(new.name)));
END;

CREATE TRIGGER IF NOT EXISTS games_fts_ad AFTER DELETE ON games BEGIN
    DELETE FROM games_fts WHERE rowid = old.id;
END;
"""
