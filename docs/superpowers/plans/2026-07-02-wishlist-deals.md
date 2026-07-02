# Wishlist Price Tracking / Deal Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Delegation guidance (Sonnet 5 executor):** delegate to Haiku subagents the mechanical steps — transcribing test scaffolding from existing test files, running pytest/ruff/mypy, updating `EXPECTED_TOOLS` in `tests/test_tool_registration.py`, and doc edits. Keep for yourself: the migration code, the ITAD response parsing (verify the live API shape first), and any SQL.

**Goal:** A `get_wishlist_deals` MCP tool that answers "which wishlist games are on sale / under $X right now?", backed by IsThereAnyDeal (Steam/GOG/Epic) and DekuDeals (switch2) price fetches cached in a new `game_prices` table.

**Architecture:** A new provider module `data/itad.py` (IsThereAnyDeal API v2, keyed by Steam appid) and a price-scrape extension to the existing DekuDeals shared-wishlist fetch. Prices land in a `game_prices` table (one row per game+platform, overwritten each refresh). The tool reads `game_wishlist ⋈ game_prices`, refreshing stale prices on demand. To make appid→wishlist-item resolution possible for *unowned* Steam games (which have no `game_platforms` row to hang an identifier on), the migration also adds `game_wishlist.store_identifier`, populated by the Steam wishlist sync.

**Tech Stack:** Python 3.12, aiosqlite, httpx, FastMCP. New env vars: `ITAD_API_KEY` (free, from isthereanydeal.com/apps/my/), optional `ITAD_COUNTRY` (default `US`).

## Global Constraints

- Schema version at plan time is **17**; this plan writes migration **v17→v18**. If another migration has landed first, renumber to `SCHEMA_VERSION + 1` everywhere (constant, DDL name, step function, `_MIGRATION_STEPS` entry) — check `SCHEMA_VERSION` in `gamelib_mcp/data/db/__init__.py:105` before starting.
- Test runner: `.venv/bin/python -m pytest`. Lint/type gates: `.venv/bin/ruff check gamelib_mcp tests scripts` and `.venv/bin/mypy gamelib_mcp` — run both before every commit.
- All new tools follow the repo pattern: business logic in `gamelib_mcp/tools/`, a declarative `@mcp.tool()` passthrough in `main.py`, a Pydantic response model in `tools/models.py`, and an entry in `tests/test_tool_registration.py` `EXPECTED_TOOLS`/`EXPECTED_ANNOTATIONS` (and the tool-count test).
- A failed/partial price fetch must never delete or blank previously cached prices — stale prices with an honest `fetched_at` beat missing data.
- **Verify the ITAD v2 API contract against https://docs.isthereanydeal.com before writing the parser.** The endpoint names and JSON shapes in Task 3 were written from documentation knowledge with a January 2026 cutoff; if the live API differs, the *plan's* structure stands (lookup → prices → normalize) but field names must follow the live docs. Record one real (sanitized) response as a test fixture.

---

### Task 1: Migration v18 — `game_prices` table + `game_wishlist.store_identifier`

**Files:**
- Modify: `gamelib_mcp/data/db/schema.py` (append `_V18_SCHEMA_DDL`)
- Modify: `gamelib_mcp/data/db/__init__.py` (bump `SCHEMA_VERSION`, add `_migrate_v17_to_v18`, extend `_MIGRATION_STEPS`, swap the two `executescript(_V17_SCHEMA_DDL)` calls in `_run_migrations` and the one in `_rebuild_table_from_current_schema` to `_V18_SCHEMA_DDL`)
- Test: `tests/test_db_migration.py` (add cases following the existing per-version test pattern in that file)

**Interfaces:**
- Produces: table `game_prices(game_id, platform, shop, price, regular_price, cut_pct, currency, deal_url, fetched_at)` with `UNIQUE(game_id, platform, shop)`; column `game_wishlist.store_identifier TEXT`.

- [ ] **Step 1: Write the failing migration test**

Add to `tests/test_db_migration.py` (mirror the structure of the v16/v17 tests already in the file — a Haiku subagent can transcribe the pattern):

```python
async def test_v17_to_v18_adds_game_prices_and_store_identifier(self):
    # Build a v17 DB using the existing helper pattern in this file, then:
    result = await migrate_db()
    self.assertEqual(result.final_version, 18)
    async with get_db() as db:
        cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(game_prices)")}
        self.assertLessEqual(
            {"game_id", "platform", "shop", "price", "regular_price",
             "cut_pct", "currency", "deal_url", "fetched_at"},
            cols,
        )
        wl_cols = {r[1] for r in await db.execute_fetchall("PRAGMA table_info(game_wishlist)")}
        self.assertIn("store_identifier", wl_cols)
```

- [ ] **Step 2: Run it to make sure it fails**

Run: `.venv/bin/python -m pytest tests/test_db_migration.py -q -k v18`
Expected: FAIL (final_version is 17 / table missing).

- [ ] **Step 3: Implement the migration**

In `schema.py`, append after `_V17_SCHEMA_DDL`:

```python
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
```

(Verify the `.replace()` anchor matches `_V16_SCHEMA_DDL`'s `game_wishlist` DDL exactly; if brittle, define the full block instead — correctness over cleverness.)

In `data/db/__init__.py`:
1. `SCHEMA_VERSION = 18`
2. Import `_V18_SCHEMA_DDL` alongside the existing `_V17_SCHEMA_DDL` import.
3. Add after `_migrate_v16_to_v17`:

```python
async def _migrate_v17_to_v18(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add game_prices + game_wishlist.store_identifier (see schema.py v18 note).

    Additive only — no data migration. ALTER TABLE ADD COLUMN is guarded so a
    re-run after a partial failure doesn't error.
    """
    if progress is not None:
        progress("Migrating to v18: add game_prices and game_wishlist.store_identifier.")

    wl_cols = await _table_columns(db, "game_wishlist")
    if "store_identifier" not in wl_cols:
        await db.execute("ALTER TABLE game_wishlist ADD COLUMN store_identifier TEXT")

    await db.executescript(
        """
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
    )

    await _set_user_version(db, 18)
    await db.commit()
```

4. Append `(17, _migrate_v17_to_v18),` to `_MIGRATION_STEPS`.
5. Replace `_V17_SCHEMA_DDL` with `_V18_SCHEMA_DDL` at its three use sites: the fresh-init `executescript` in `_run_migrations`, the final-reconciliation `executescript` in `_run_migrations`, and `_rebuild_table_from_current_schema`.

- [ ] **Step 4: Run migration tests + full suite**

Run: `.venv/bin/python -m pytest tests/test_db_migration.py -q` then the full suite.
Expected: PASS (the fresh-init path now creates v18, so unrelated tests keep passing).

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/data/db/schema.py gamelib_mcp/data/db/__init__.py tests/test_db_migration.py
git commit -m "feat: v18 schema — game_prices cache + wishlist store_identifier"
```

### Task 2: Persist the Steam appid on wishlist rows

**Files:**
- Modify: `gamelib_mcp/data/db/upserts.py` (`upsert_wishlist_entry` gains `store_identifier`)
- Modify: `gamelib_mcp/data/steam_wishlist.py` (pass the appid through)
- Test: `tests/test_wishlist.py`

**Interfaces:**
- Produces: `upsert_wishlist_entry(game_id, platform, wishlisted_at=None, source=None, store_identifier=None) -> int` — existing callers (dekudeals, platforms.py manual path) keep working via the default.

- [ ] **Step 1: Write the failing test** (in `tests/test_wishlist.py`, following its existing fixture pattern)

```python
async def test_upsert_wishlist_entry_stores_store_identifier(self):
    game_id = await upsert_game(None, "Hollow Knight: Silksong")
    await upsert_wishlist_entry(game_id, "steam", source="steam", store_identifier="1030300")
    async with get_db() as db:
        row = await db.execute_fetchone(
            "SELECT store_identifier FROM game_wishlist WHERE game_id = ?", (game_id,)
        )
    self.assertEqual(row["store_identifier"], "1030300")
```

- [ ] **Step 2: Run it to verify failure** (`TypeError: unexpected keyword argument`).

- [ ] **Step 3: Implement**

In `upsert_wishlist_entry`, add the parameter and column (keep the `ON CONFLICT` update — `store_identifier = COALESCE(excluded.store_identifier, store_identifier)` so a manual re-add without an id doesn't erase one a sync recorded):

```python
async def upsert_wishlist_entry(
    game_id: int,
    platform: str,
    wishlisted_at: str | None = None,
    source: str | None = None,
    store_identifier: str | None = None,
) -> int:
    ...
    await db.execute(
        """INSERT INTO game_wishlist (game_id, platform, wishlisted_at, source, store_identifier)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(game_id, platform) DO UPDATE SET
               wishlisted_at = excluded.wishlisted_at,
               source = excluded.source,
               store_identifier = COALESCE(excluded.store_identifier, store_identifier)""",
        (game_id, platform, now, source, store_identifier),
    )
```

In `steam_wishlist.py`, both `upsert_wishlist_entry` calls pass `store_identifier=str(appid)`.

- [ ] **Step 4: Run tests** (`tests/test_wishlist.py` + full suite) — PASS.

- [ ] **Step 5: Commit** — `feat: record Steam appid on wishlist rows for price lookups`.

### Task 3: `data/itad.py` — IsThereAnyDeal price provider

**Files:**
- Create: `gamelib_mcp/data/itad.py`
- Test: `tests/test_itad.py`

**Interfaces:**
- Produces: `is_itad_configured() -> bool`; `async fetch_steam_prices(appids: list[int]) -> dict[int, PriceInfo]` where `PriceInfo` is a dataclass `{shop: str, price: float, regular_price: float, cut_pct: int, currency: str, deal_url: str | None}` (best current deal across shops per appid).

**Contract check first (do NOT delegate):** read https://docs.isthereanydeal.com. As of the plan author's knowledge: ITAD v2 uses `GET /games/lookup/v1?key=...&appid=<appid>` (or `POST /unstable/id-lookup/shop/61/v1` batch by Steam shop id) to map a Steam appid to an ITAD game UUID, then `POST /games/prices/v3?key=...&country=US` with a JSON array of UUIDs returning per-game `deals: [{shop:{id,name}, price:{amount,currency}, regular:{amount}, cut, url}]`. Confirm names/paths and adjust the code below to the live contract; capture one sanitized real response into the test as `_SAMPLE_PRICES_RESPONSE`.

- [ ] **Step 1: Write failing parser tests** (pure functions, no network):

```python
# tests/test_itad.py
from gamelib_mcp.data.itad import _best_deal, PriceInfo

_SAMPLE_DEALS = [
    {"shop": {"id": 61, "name": "Steam"}, "price": {"amount": 9.99, "currency": "USD"},
     "regular": {"amount": 19.99, "currency": "USD"}, "cut": 50, "url": "https://example/steam"},
    {"shop": {"id": 35, "name": "GOG"}, "price": {"amount": 8.99, "currency": "USD"},
     "regular": {"amount": 19.99, "currency": "USD"}, "cut": 55, "url": "https://example/gog"},
]

def test_best_deal_picks_lowest_price():
    best = _best_deal(_SAMPLE_DEALS)
    assert best == PriceInfo(shop="GOG", price=8.99, regular_price=19.99,
                             cut_pct=55, currency="USD", deal_url="https://example/gog")

def test_best_deal_empty_returns_none():
    assert _best_deal([]) is None

def test_best_deal_skips_malformed_entries():
    assert _best_deal([{"shop": None}, _SAMPLE_DEALS[0]]).shop == "Steam"
```

- [ ] **Step 2: Run to verify failure** (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `data/itad.py`**

```python
"""IsThereAnyDeal price lookups for Steam/GOG/Epic wishlist items.

ITAD API v2 (https://docs.isthereanydeal.com), free key via
isthereanydeal.com/apps/my/. Two-step: Steam appid -> ITAD game UUID
(lookup endpoint), then a batch prices call. Only the *best current deal*
per game is kept — ITAD itself is the history-of-record, gamelib only
caches "what does it cost right now" in game_prices.

Follows the provider conventions of this package: module-level env reads
with os.getenv fallbacks, explicit httpx timeouts, and failures raised to
the caller (the tool layer decides whether stale cache is acceptable).
"""

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

_ITAD_BASE = "https://api.isthereanydeal.com"
_ITAD_TIMEOUT = 20.0
_LOOKUP_BATCH = 100


@dataclass(frozen=True)
class PriceInfo:
    shop: str
    price: float
    regular_price: float | None
    cut_pct: int | None
    currency: str
    deal_url: str | None


def is_itad_configured() -> bool:
    return bool(os.getenv("ITAD_API_KEY"))


def _best_deal(deals: list) -> PriceInfo | None:
    """Pick the cheapest well-formed deal. Pure; tolerates malformed entries."""
    best: PriceInfo | None = None
    for deal in deals or []:
        try:
            shop = deal["shop"]["name"]
            amount = float(deal["price"]["amount"])
            currency = deal["price"]["currency"]
        except (TypeError, KeyError, ValueError):
            continue
        regular = deal.get("regular") or {}
        info = PriceInfo(
            shop=str(shop),
            price=amount,
            regular_price=float(regular["amount"]) if "amount" in regular else None,
            cut_pct=int(deal["cut"]) if deal.get("cut") is not None else None,
            currency=str(currency),
            deal_url=deal.get("url"),
        )
        if best is None or info.price < best.price:
            best = info
    return best


async def fetch_steam_prices(appids: list[int]) -> dict[int, PriceInfo]:
    """Map Steam appids to their best current deal. Raises on HTTP failure."""
    api_key = os.getenv("ITAD_API_KEY", "")
    if not api_key or not appids:
        return {}
    country = os.getenv("ITAD_COUNTRY", "US")

    async with httpx.AsyncClient(
        timeout=_ITAD_TIMEOUT, headers={"User-Agent": "gamelib-mcp/1.0"}
    ) as client:
        # Step 1: appid -> ITAD UUID.  VERIFY endpoint/shape against live docs.
        uuid_by_appid: dict[int, str] = {}
        for i in range(0, len(appids), _LOOKUP_BATCH):
            chunk = appids[i : i + _LOOKUP_BATCH]
            resp = await client.post(
                f"{_ITAD_BASE}/lookup/id/shop/61/v1",
                params={"key": api_key},
                json=[f"app/{appid}" for appid in chunk],
            )
            resp.raise_for_status()
            payload = resp.json()
            for appid in chunk:
                uuid = payload.get(f"app/{appid}")
                if uuid:
                    uuid_by_appid[appid] = uuid

        if not uuid_by_appid:
            return {}

        # Step 2: batch prices for all found UUIDs.
        resp = await client.post(
            f"{_ITAD_BASE}/games/prices/v3",
            params={"key": api_key, "country": country},
            json=list(uuid_by_appid.values()),
        )
        resp.raise_for_status()
        prices_payload = resp.json()

    deals_by_uuid: dict[str, list] = {}
    for entry in prices_payload if isinstance(prices_payload, list) else []:
        if isinstance(entry, dict) and entry.get("id"):
            deals_by_uuid[entry["id"]] = entry.get("deals") or []

    result: dict[int, PriceInfo] = {}
    for appid, uuid in uuid_by_appid.items():
        best = _best_deal(deals_by_uuid.get(uuid, []))
        if best is not None:
            result[appid] = best
    return result
```

- [ ] **Step 4: Add a mocked end-to-end test for `fetch_steam_prices`** using `unittest.mock.patch` on `httpx.AsyncClient` (copy the mock-client pattern from `tests/test_steam_store.py` or `tests/test_opencritic.py` — good Haiku delegation). Assert appid→PriceInfo mapping and that a missing appid is absent from the result.

- [ ] **Step 5: Run `tests/test_itad.py`, ruff, mypy** — PASS.

- [ ] **Step 6: Commit** — `feat: IsThereAnyDeal price provider`.

### Task 4: DekuDeals prices for switch2 wishlist items

**Files:**
- Modify: `gamelib_mcp/data/dekudeals.py` (add `fetch_wishlist_prices`)
- Modify: `gamelib_mcp/data/scrape_config.py` (extend `DekuDealsScrapeConfig` with price selectors; update `ALLOWED_HOSTS` only if a new host is needed — it is not, dekudeals.com is already allowed)
- Test: `tests/test_scrape_parsers.py` (new fixture-driven parser test) + a recorded fixture page under `gamelib_mcp/data/scrape_fixtures/`

**Interfaces:**
- Produces: `async fetch_wishlist_prices() -> dict[str, dict]` mapping wishlist item title → `{"price": float, "regular_price": float | None, "cut_pct": int | None, "currency": "USD", "deal_url": str}`.

**Approach:** the shared wishlist *HTML* page (same URL as the JSON export, without `.json`) renders each item with its current best eShop price — one fetch covers the whole wishlist. First, manually check whether the `.json` export already includes price fields (fetch it once with curl and look); if it does, parse those instead and skip the HTML scrape entirely — simpler wins. The steps below assume HTML is required.

- [ ] **Step 1: Record a fixture.** Fetch the live shared wishlist page once (`curl -sL "$DEKUDEALS_WISHLIST_URL" -o gamelib_mcp/data/scrape_fixtures/dekudeals_wishlist_page.html`), trim to ~3 items, and note the CSS structure (at plan time: each item is a `div.cell` containing an `a[href^="/items/"]` name link and a price block — verify against the fixture you record).

- [ ] **Step 2: Write the failing parser test** in `tests/test_scrape_parsers.py`, following that file's existing fixture-loading pattern:

```python
def test_dekudeals_wishlist_price_parse(self):
    html = _load_fixture("dekudeals_wishlist_page.html")
    prices = _parse_wishlist_prices(html, DekuDealsScrapeConfig())
    self.assertIn("Pikmin 4", prices)          # adjust to fixture contents
    entry = prices["Pikmin 4"]
    self.assertIsInstance(entry["price"], float)
    self.assertTrue(entry["deal_url"].startswith("https://www.dekudeals.com/items/"))
```

- [ ] **Step 3: Implement.** Add frozen selector fields to `DekuDealsScrapeConfig` (e.g. `wishlist_item_selector`, `price_selector`, `title_selector` — follow the dataclass/validation style of the other providers in `scrape_config.py`; selectors must compile under soupsieve for the validator). Implement in `dekudeals.py`:

```python
def _parse_wishlist_prices(html: str, config: DekuDealsScrapeConfig) -> dict[str, dict]:
    """Pure HTML->prices parse, selector-driven so the heal tools can fix drift."""
    ...

async def fetch_wishlist_prices() -> dict[str, dict]:
    """Fetch the shared wishlist HTML page and parse current prices per title.

    Raises on fetch failure (same rationale as _fetch_wishlist_items: a
    transient error must not be mistaken for 'nothing is on sale').
    """
    ...
```

Parse price strings like `"$29.99"` / `"$59.99 → $29.99 (-50%)"` defensively; unparseable items are skipped with `logger.debug`, never raised.

- [ ] **Step 4: Run parser tests + `tests/test_scrape_config.py`** — PASS (the config validator must accept the new fields; if `scrape_validate.py` replays dekudeals fixtures, extend its expectations per the "Parser changes must keep tests and fixture expectations in sync" rule in CLAUDE.md).

- [ ] **Step 5: Commit** — `feat: DekuDeals wishlist price scrape (selector-driven)`.

### Task 5: Price cache write/read in the db layer

**Files:**
- Modify: `gamelib_mcp/data/db/upserts.py` (add `upsert_game_prices`)
- Modify: `gamelib_mcp/data/db/queries.py` (add `load_wishlist_with_prices`)
- Modify: `gamelib_mcp/data/db/__init__.py` (re-export both — the façade re-exports everything)
- Test: `tests/test_wishlist.py`

**Interfaces:**
- Produces: `async upsert_game_prices(rows: list[dict]) -> int` where each row is `{game_id, platform, shop, price, regular_price, cut_pct, currency, deal_url}` (fetched_at stamped inside); `async load_wishlist_with_prices(platform: str | None) -> list[aiosqlite.Row]` returning wishlist rows LEFT JOINed to their price rows plus the steam appid (from `store_identifier`, falling back to the owned-row identifier subquery used in `tools/common.py::STEAM_APPID_SQL`).

- [ ] **Step 1: Failing tests** — upsert twice for the same (game, platform, shop) and assert one row with the newer price; `load_wishlist_with_prices(None)` returns a wishlist row with `price IS NULL` when no price is cached.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** with `INSERT ... ON CONFLICT(game_id, platform, shop) DO UPDATE SET` (all price columns + `fetched_at = excluded.fetched_at`).
- [ ] **Step 4: Tests pass; ruff/mypy pass.**
- [ ] **Step 5: Commit** — `feat: game_prices upsert + wishlist price join`.

### Task 6: `get_wishlist_deals` tool + registration

**Files:**
- Create: `gamelib_mcp/tools/deals.py`
- Modify: `gamelib_mcp/main.py` (passthrough with `@mcp.tool(annotations=DIAGNOSTIC_NETWORK_TOOL)` — read-mostly but network-refreshing, mirroring `diagnose_scrape`)
- Modify: `gamelib_mcp/tools/models.py` (add `WishlistDealEntry`, `WishlistDealsResponse(FlexibleModel)`)
- Test: `tests/test_tools_deals.py`, update `tests/test_tool_registration.py` (`EXPECTED_TOOLS`, `EXPECTED_ANNOTATIONS`, bump the tool-count test from 32 to 33)

**Interfaces:**
- Produces the MCP tool:

```python
async def get_wishlist_deals(
    platform: str | None = None,
    max_price: float | None = None,
    min_cut_pct: int | None = None,
    refresh: bool = False,
) -> dict:
    """
    Current prices/deals for wishlist games, cheapest first.

    Prices come from IsThereAnyDeal (Steam wishlist items; covers Steam/GOG/
    Epic shops) and DekuDeals (switch2 items). Cached in the DB; a fetch runs
    automatically when the cache is older than 12h, or immediately with
    refresh=True. Filters: platform, max_price (in the configured ITAD
    country's currency), min_cut_pct (e.g. 50 for "at least half off").
    Items with no known price are listed separately in unpriced.
    """
```

Behavior contract:
1. Load wishlist rows via `load_wishlist_with_prices` (platform validated with `validate_platform(..., LIBRARY_PLATFORMS)` from `tools/common.py`).
2. Staleness check: any row missing a price, or with `fetched_at` older than 12h (`_PRICE_TTL_HOURS = 12` module constant) → trigger a refresh pass for the affected sources (ITAD for steam-sourced rows with an appid, DekuDeals for switch2 rows), matching DekuDeals price titles to wishlist games with the same fuzzy helper (`extract_best_fuzzy_key`, `config.fuzzy_cutoff`) that `dekudeals.py` already uses.
3. A refresh failure logs a warning and serves cached rows, adding `"price_refresh_errors": [...]` to the response — it never raises, and never deletes cached prices.
4. Response: `{"deals": [...cheapest first...], "unpriced": [names...], "fetched_at": ..., "count": N}` — each deal entry `{game_id, name, platform, shop, price, regular_price, cut_pct, currency, deal_url, wishlisted_at}`.
5. `ITAD_API_KEY` unset: steam items land in `unpriced` with a one-line `"itad": "unconfigured"` note in the response; not an error.

- [ ] **Step 1: Failing tool tests** (`tests/test_tools_deals.py`, patching `gamelib_mcp.tools.deals.fetch_steam_prices` / `fetch_wishlist_prices` with mocks, following the patch pattern used in `tests/test_tools_admin.py`): cached-fresh path does not call fetchers; `refresh=True` does; `max_price`/`min_cut_pct` filter; fetcher exception → cached data + `price_refresh_errors`.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement `tools/deals.py`** per the contract; add the models; add the `main.py` passthrough (docstring = wire schema — copy the tool docstring above); register in `test_tool_registration.py`.
- [ ] **Step 4: Full suite + ruff + mypy** — PASS.
- [ ] **Step 5: Commit** — `feat: get_wishlist_deals tool (ITAD + DekuDeals price cache)`.

### Task 7: Docs

- [ ] Update `CLAUDE.md` (tools list under `tools/`, new env vars under Required Environment Variables, a short "Wishlist price tracking" design-pattern bullet), `.env.example` / `.env.local.example` (`ITAD_API_KEY=`, `ITAD_COUNTRY=US`), and `README.md` if it lists tools. Good Haiku delegation.
- [ ] Full suite + ruff + mypy; commit — `docs: wishlist deals tool + ITAD env vars`.

## Explicit non-goals (YAGNI)

- No price *history* (ITAD keeps it; add a `get_price_history` passthrough later if ever needed).
- No proactive alerting/notifications — the "alert" is the AI calling the tool. A periodic-refresh hook can come later once the pull model proves useful.
- No PSN prices (no wishlist sync exists for PSN; manual PSN wishlist items simply appear in `unpriced`).
