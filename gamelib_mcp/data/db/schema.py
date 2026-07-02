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
