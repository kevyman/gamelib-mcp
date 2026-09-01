# Healable scrapers

Why this exists: only the *declarative* surface of the four brittle scrape
providers is healable, and the boundary between that and the imperative code
is the whole safety property — a proposal that could rewrite a reconciliation
guard would be a remote-code path, not a repair. The root `CLAUDE.md` keeps the
boundary rule and the validation gate; the detail moved here on 2026-09-01.

## Healable scrapers

**Healable scrapers**: the four brittle scrape providers (backloggd, steam_reviews, metacritic, dekudeals) keep their *declarative* surface (URLs, selectors, regexes, TTLs, caps) in frozen dataclasses in `data/scrape_config.py`, overridable via the versioned `scrape_config` table (load errors fail open to code defaults). URL hosts are frozen to per-provider allowlists; selectors/regexes must compile; everything is bounds-capped. `manage_scrape_config(action="propose")` persists nothing unless `scrape_validate.py` passes: structural check → fixture replay (`data/scrape_fixtures/`) → live trial + history sanity (titles fuzzy-overlap the library, appids resolve to owned games, Metascore within ±20 of stored). The *imperative* parts stay code and are not healable (title sibling-walks, score fusion, all of OpenCritic/IGDB, every reconciliation guard). Parser changes must keep `tests/test_scrape_parsers.py` and the fixture expectations in sync with the fixture pages.
