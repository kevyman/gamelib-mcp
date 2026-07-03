# Preference-Aware Cross-Platform Wishlist Deals Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `get_wishlist_deals` recommends which platform to buy each wishlist game on, honoring the owner's `hardware_preference` (Switch 2 over Steam), pricing Steam-wishlisted games on Switch 2 via DekuDeals search when IGDB says a Switch release exists, and overriding the preference only when another platform's deal is dramatically cheaper.

**Architecture:** Three layers. (1) *Availability:* IGDB already returns per-platform release data on every search; a new `games.igdb_platforms` column (JSON array of IGDB platform ids, schema v19) persists it, written by `_apply_igdb_metadata`; Switch 2 is IGDB platform **508** (verified live 2026-07-03), original Switch is 130, and both count as internal `switch2`. (2) *Pricing:* a new `dekudeals.fetch_search_prices()` prices arbitrary titles via DekuDeals' public search page — verified live (2026-07-03) to use the **exact same card markup** as the wishlist page, so the existing `_parse_wishlist_prices` parser is reused unchanged; only a new healable `search_url_template` config field is added. (3) *Ranking:* `tools/deals.py` groups price options per game across platforms, reads `hardware_preference` from meta, and picks a `recommended` option: the preferred platform's cheapest deal unless another platform's price is below `preference_override_ratio` (default 0.5) × the preferred price.

**Tech Stack:** Python 3.12 / uv, aiosqlite, httpx, BeautifulSoup+soupsieve, rapidfuzz (via existing `extract_best_fuzzy_key`), FastMCP.

## Global Constraints

- **Test runner:** `.venv/bin/python -m pytest` — MUST run with Claude Code's Bash sandbox disabled (`dangerouslyDisableSandbox: true`) or outside any sandbox: aiosqlite tests hang under sandboxing (worker thread completes but the event-loop callback never resumes the coroutine). A hung test run shows ~0 output and low CPU; kill it and rerun unsandboxed.
- **Lint/type gates (CI-blocking):** `.venv/bin/ruff check gamelib_mcp tests scripts` and `.venv/bin/mypy gamelib_mcp` (mypy covers `gamelib_mcp` only, not `tests/`).
- **Schema version:** current is 18; this plan introduces exactly one bump, to **19**. Adding a version = new `_V19_SCHEMA_DDL` + new step function + `_MIGRATION_STEPS` entry + `SCHEMA_VERSION = 19` + updating both `executescript(_V18_SCHEMA_DDL)` call sites in `_run_migrations` to `_V19_SCHEMA_DDL`.
- **IGDB platform ids:** PC=6, PS5=167, Switch=130, **Switch 2=508**. Apicalypse filter syntax: `platforms = (a,b)` means "contains at least one of".
- **Healable-scraper rules:** new config fields need `kind` metadata; `url_template` fields require the provider's `ALLOWED_HOSTS` entry to include the host. Never point a scraper at a non-allowlisted host.
- **Single-user by design** (docs/adr/0001-single-user.md): no per-user parameters or tables.
- **Patching convention in tests:** patch functions *as imported into* the module under test (e.g. `gamelib_mcp.tools.deals.fetch_search_prices`), not their origin module.
- Commit after each task with a conventional-commit message; end commit messages with the project's standard Claude co-author trailer.

---

### Task 1: IGDB Switch 2 constants, `IGDBGame.platforms`, multi-id platform filter

**Files:**
- Modify: `gamelib_mcp/data/igdb.py` (constants block ~line 78–91; `IGDBGame` dataclass ~line 256; `_build_search_game_query` ~line 402; `search_game` parse loop ~line 455; `resolve_game` ~line 516; `resolve_and_link_game` ~line 563; `choose_igdb_platform_hint` ~line 806; `upsert_backfill_platform_release_dates` ~line 823)
- Modify: `gamelib_mcp/data/nintendo.py:329`, `gamelib_mcp/data/nintendo_pctl.py:205` (switch2 platform hints)
- Test: `tests/test_igdb.py`

**Interfaces:**
- Produces: `IGDB_PLATFORM_SWITCH2 = 508`; `PLATFORM_TO_IGDB_ANY: dict[str, tuple[int, ...]]` (preference-ordered, first id wins for release dates); `IGDB_TO_PLATFORM: dict[int, str]`; `IGDBGame.platforms: list[int]`; `_build_search_game_query(name, igdb_platform_id: int | tuple[int, ...] | None)`. Task 3 consumes `IGDBGame.platforms`; Task 6 consumes `IGDB_TO_PLATFORM`.
- `PLATFORM_TO_IGDB` (int map) is kept unchanged — `epic.py`/`gog.py`/`psn.py` and several tests use it, and for those single-id platforms it is equivalent.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_igdb.py` (follow the file's existing unittest style):

```python
class PlatformFilterTests(unittest.TestCase):
    def test_single_platform_id_filter_unchanged(self):
        query = igdb._build_search_game_query("Hades", 6)
        self.assertIn("where platforms = 6;", query)

    def test_tuple_platform_ids_render_contains_any(self):
        query = igdb._build_search_game_query(
            "Mario Kart World", igdb.PLATFORM_TO_IGDB_ANY["switch2"]
        )
        self.assertIn("where platforms = (508,130);", query)

    def test_platforms_field_requested(self):
        query = igdb._build_search_game_query("Hades")
        self.assertIn(" platforms,", query)

    def test_platform_maps_cover_switch_generations(self):
        self.assertEqual(igdb.IGDB_PLATFORM_SWITCH2, 508)
        self.assertEqual(igdb.PLATFORM_TO_IGDB_ANY["switch2"], (508, 130))
        self.assertEqual(igdb.IGDB_TO_PLATFORM[130], "switch2")
        self.assertEqual(igdb.IGDB_TO_PLATFORM[508], "switch2")
        self.assertEqual(igdb.IGDB_TO_PLATFORM[6], "steam")
        self.assertEqual(igdb.IGDB_TO_PLATFORM[167], "ps5")
```

And a parse test inside the existing `search_game` test class (mirror how neighbors patch `_post_igdb_games`/token — read one existing test in the file first and copy its patching exactly):

```python
    async def test_search_game_captures_platform_ids(self):
        item = {
            "id": 1,
            "name": "Hades",
            "category": 0,
            "platforms": [6, 130, 508],
            "release_dates": [{"platform": 167, "date": 1600000000}],
        }
        # ...patch _get_token/_post_igdb_games per the file's existing pattern,
        # returning [item]...
        results = await igdb.search_game("Hades", None)
        self.assertEqual(results[0].platforms, [6, 130, 167, 508])
```

(`platforms` must be the sorted union of the `platforms` field and `release_dates` platform keys.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_igdb.py -q -k "PlatformFilter or captures_platform_ids"` (sandbox disabled)
Expected: FAIL — `AttributeError: ... has no attribute 'PLATFORM_TO_IGDB_ANY'`

- [ ] **Step 3: Implement in `gamelib_mcp/data/igdb.py`**

Constants block (after `IGDB_PLATFORM_SWITCH = 130`, replacing its comment):

```python
IGDB_PLATFORM_SWITCH = 130
IGDB_PLATFORM_SWITCH2 = 508  # Nintendo Switch 2 (IGDB added it post-launch; verified 2026-07-03)

# Our platform value → IGDB platform ID (primary id; single-id platforms only —
# for switch2, which spans two IGDB platforms, use PLATFORM_TO_IGDB_ANY).
PLATFORM_TO_IGDB: dict[str, int] = {
    "steam": IGDB_PLATFORM_PC,
    "epic": IGDB_PLATFORM_PC,
    "gog": IGDB_PLATFORM_PC,
    "ps5": IGDB_PLATFORM_PS5,
    "switch2": IGDB_PLATFORM_SWITCH,
}

# Our platform value → all IGDB platform ids that count as it, preference-
# ordered: the first id with a release date wins in
# upsert_backfill_platform_release_dates. switch2 covers both generations
# (native Switch 2 SKUs + the backward-compatible Switch library).
PLATFORM_TO_IGDB_ANY: dict[str, tuple[int, ...]] = {
    "steam": (IGDB_PLATFORM_PC,),
    "epic": (IGDB_PLATFORM_PC,),
    "gog": (IGDB_PLATFORM_PC,),
    "ps5": (IGDB_PLATFORM_PS5,),
    "switch2": (IGDB_PLATFORM_SWITCH2, IGDB_PLATFORM_SWITCH),
}

# Reverse map for availability checks (games.igdb_platforms → our platforms).
# PC deliberately maps to "steam": it's the only PC storefront with a price
# source, which is all this map is consumed for (tools/deals.py).
IGDB_TO_PLATFORM: dict[int, str] = {
    IGDB_PLATFORM_PC: "steam",
    IGDB_PLATFORM_PS5: "ps5",
    IGDB_PLATFORM_SWITCH: "switch2",
    IGDB_PLATFORM_SWITCH2: "switch2",
}
```

`IGDBGame`: add field `platforms: list[int] = field(default_factory=list)` (after `platform_release_dates`, with comment `# all IGDB platform ids the game is released on`).

`_build_search_game_query` — widen the parameter and add `platforms` to the fields clause:

```python
def _build_search_game_query(
    name: str, igdb_platform_id: int | tuple[int, ...] | None = None
) -> str:
    escaped_name = _escape_igdb_search_term(name)
    filters = []
    if igdb_platform_id is not None:
        ids = igdb_platform_id if isinstance(igdb_platform_id, tuple) else (igdb_platform_id,)
        if len(ids) == 1:
            filters.append(f"platforms = {ids[0]}")
        else:
            # Apicalypse: (a,b) = "contains at least one of".
            filters.append(f"platforms = ({','.join(str(i) for i in ids)})")
```

In the fields clause change `"version_parent.id, version_parent.name, version_title, "` +
`"release_dates.platform, release_dates.date;"` to include the new field:

```python
        "version_parent.id, version_parent.name, version_title, "
        "platforms, release_dates.platform, release_dates.date;",
```

`search_game`: widen its `igdb_platform_id: int | None` annotation the same way, and in the parse loop after `platform_dates` is built add:

```python
        platform_ids = sorted(
            {int(p) for p in item.get("platforms") or [] if isinstance(p, int)}
            | set(platform_dates)
        )
```

and pass `platforms=platform_ids,` in the `IGDBGame(...)` constructor call (after `platform_release_dates=platform_dates,`).

`resolve_game` and `resolve_and_link_game`: widen `igdb_platform_id: int | None` to `int | tuple[int, ...] | None` (annotation-only; bodies pass it through).

`choose_igdb_platform_hint` — return tuples from the ANY map:

```python
async def choose_igdb_platform_hint(game_id: int) -> tuple[int, ...] | None:
    platforms_by_game = await load_platforms_for_games([game_id])
    platforms = platforms_by_game.get(game_id, [])
    if not platforms:
        return None
    for platform in platforms:
        if platform["platform"] == "steam":
            return PLATFORM_TO_IGDB_ANY["steam"]
    for platform in platforms:
        if platform.get("owned") and platform["platform"] in PLATFORM_TO_IGDB_ANY:
            return PLATFORM_TO_IGDB_ANY[platform["platform"]]
    return None
```

`upsert_backfill_platform_release_dates` — replace the single-id lookup:

```python
    for platform in platforms_by_game.get(game_id, []):
        candidate_ids = PLATFORM_TO_IGDB_ANY.get(platform["platform"], ())
        release_date = next(
            (
                igdb_game.platform_release_dates[pid]
                for pid in candidate_ids
                if pid in igdb_game.platform_release_dates
            ),
            None,
        )
        game_platform_id = platform["game_platform_id"]
        if release_date is None or game_platform_id is None:
            continue
```

(the `upsert_game_platform_enrichment` call below stays as-is).

`gamelib_mcp/data/nintendo.py:329` and `gamelib_mcp/data/nintendo_pctl.py:205`: change `igdb_platform_id = PLATFORM_TO_IGDB.get(PLATFORM)` to `igdb_platform_id = PLATFORM_TO_IGDB_ANY.get(PLATFORM)` and update the import line in each file from `PLATFORM_TO_IGDB` to `PLATFORM_TO_IGDB_ANY`. (This is the point of the widening: a Switch-2-only title like Mario Kart World is 508-only on IGDB and a `platforms = 130` filter would miss it. `resolve_game`'s no-filter fallback already softens misses, but the filtered pass should hit.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_igdb.py tests/test_nintendo.py tests/test_igdb_apply_metadata.py tests/test_epic.py tests/test_gog.py tests/test_psn.py -q`
Expected: PASS. Note: `tests/test_nintendo.py:113,169` assert hint `igdb.PLATFORM_TO_IGDB["switch2"]` — update those two assertions to `igdb.PLATFORM_TO_IGDB_ANY["switch2"]`.

- [ ] **Step 5: Lint/type + commit**

Run: `.venv/bin/ruff check gamelib_mcp tests && .venv/bin/mypy gamelib_mcp`
```bash
git add gamelib_mcp/data/igdb.py gamelib_mcp/data/nintendo.py gamelib_mcp/data/nintendo_pctl.py tests/test_igdb.py tests/test_nintendo.py
git commit -m "feat: IGDB Switch 2 (508) awareness + capture per-game platform ids"
```

---

### Task 2: Schema v19 — `games.igdb_platforms` + wishlist IGDB re-claim

**Files:**
- Modify: `gamelib_mcp/data/db/schema.py` (append `_V19_SCHEMA_DDL` after `_V18_SCHEMA_DDL`, ~line 852)
- Modify: `gamelib_mcp/data/db/__init__.py` (`SCHEMA_VERSION` line 105; new `_migrate_v18_to_v19` next to `_migrate_v17_to_v18` ~line 1152; `_MIGRATION_STEPS` ~line 1336; both `executescript(_V18_SCHEMA_DDL)` sites in `_run_migrations`)
- Test: `tests/test_db_migration.py`

**Interfaces:**
- Produces: `games.igdb_platforms TEXT` column (JSON array of IGDB platform ids, NULL = availability unknown). Tasks 3/5/6 consume it.
- The migration also NULLs `igdb_cached_at`/`igdb_claimed_at` for wishlisted games so the existing background IGDB backfill re-fetches them and fills `igdb_platforms` (~10 games per enrichment pass; the whole wishlist converges over a few background cycles without touching the rest of the library).

- [ ] **Step 1: Write the failing test**

Read `tests/test_db_migration.py` first and match its fixture style. Add:

```python
    async def test_v19_adds_igdb_platforms_and_reclaims_wishlisted_games(self):
        # Fresh DB is already v19: column exists.
        async with db_module.get_db() as db:
            cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(games)")}
        self.assertIn("igdb_platforms", cols)

        # Step function re-claims IGDB only for wishlisted games missing availability.
        wished = await seed_game("Wishlisted Enriched")
        other = await seed_game("Not Wishlisted")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_cached_at = '2026-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
                (wished, other),
            )
            await db.commit()
        await db_module.upsert_wishlist_entry(wished, "steam", source="steam")

        async with db_module.get_db() as db:
            from gamelib_mcp.data.db import _migrate_v18_to_v19
            await _migrate_v18_to_v19(db, None)
            await db.commit()
            rows = {
                r["id"]: r["igdb_cached_at"]
                for r in await db.execute_fetchall(
                    "SELECT id, igdb_cached_at FROM games WHERE id IN (?, ?)", (wished, other)
                )
            }
        self.assertIsNone(rows[wished])
        self.assertIsNotNone(rows[other])
```

(If `seed_game`/`db_module` imports differ in that file, follow its local conventions.)

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_db_migration.py -q -k v19`
Expected: FAIL — no `igdb_platforms` column / no `_migrate_v18_to_v19`.

- [ ] **Step 3: Implement**

`schema.py`, after the `_V18_SCHEMA_DDL` block:

```python
# v19 adds games.igdb_platforms: the full set of IGDB platform ids a game is
# released on (JSON int array; NULL = not yet fetched), written by IGDB
# enrichment regardless of ownership. Powers cross-platform availability in
# get_wishlist_deals ("this Steam wishlist item also has a Switch release").
_V19_SCHEMA_DDL = _V18_SCHEMA_DDL.replace(
    "        igdb_cached_at   TEXT,",
    "        igdb_cached_at   TEXT,\n        igdb_platforms   TEXT,",
)
```

**Caution:** `igdb_cached_at   TEXT,` appears in every historical games DDL in the chained string (grep showed lines 175, 273, 389…). `str.replace` replaces all occurrences — that is fine and correct here, because each `CREATE TABLE IF NOT EXISTS games` in the chain must agree; but verify by asserting in the test run that `PRAGMA table_info(games)` on a fresh DB has the column exactly once (SQLite would error on a duplicate column anyway). If the anchor text's exact whitespace differs, copy it verbatim from the file.

`db/__init__.py`:

```python
SCHEMA_VERSION = 19
```

New step (place directly after `_migrate_v17_to_v18`):

```python
async def _migrate_v18_to_v19(db: aiosqlite.Connection, progress: _Progress | None) -> None:
    """Add games.igdb_platforms (see schema.py v19 note) and re-claim IGDB
    enrichment for wishlisted games so the backfill re-fetches their platform
    availability. Scoped to game_wishlist rows: re-claiming the whole library
    would burn thousands of IGDB calls for data only the deals tool reads,
    and only wishlist items are ever priced."""
    if progress is not None:
        progress("Migrating to v19: add games.igdb_platforms; re-claim IGDB for wishlisted games.")

    cols = await _table_columns(db, "games")
    if "igdb_platforms" not in cols:
        await db.execute("ALTER TABLE games ADD COLUMN igdb_platforms TEXT")

    await db.execute(
        """UPDATE games
           SET igdb_cached_at = NULL, igdb_claimed_at = NULL
           WHERE igdb_platforms IS NULL
             AND id IN (SELECT game_id FROM game_wishlist)"""
    )
```

`_MIGRATION_STEPS`: append `(18, _migrate_v18_to_v19),`.

In `_run_migrations`, change **both** `await db.executescript(_V18_SCHEMA_DDL)` call sites (the fresh-DB branch and the post-steps tail) to `_V19_SCHEMA_DDL`, and update the module's import of the DDL name from `schema` accordingly (check the import at the top of `db/__init__.py` — it imports the DDL constants from `.schema`).

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_db_migration.py -q`
Expected: PASS (including pre-existing migration-chain tests — they exercise old-shape DBs through the full ladder).

- [ ] **Step 5: Lint/type + commit**

```bash
git add gamelib_mcp/data/db/schema.py gamelib_mcp/data/db/__init__.py tests/test_db_migration.py
git commit -m "feat: schema v19 — games.igdb_platforms + IGDB re-claim for wishlisted games"
```

---

### Task 3: `_apply_igdb_metadata` persists `igdb_platforms`

**Files:**
- Modify: `gamelib_mcp/data/igdb.py` (`_apply_igdb_metadata`, ~line 709)
- Test: `tests/test_igdb_apply_metadata.py`

**Interfaces:**
- Consumes: `IGDBGame.platforms` (Task 1), `games.igdb_platforms` column (Task 2).
- Produces: `games.igdb_platforms` populated as a JSON int array on every IGDB apply where IGDB reported platforms. Not consulted against `manual_overrides` — it is IGDB-derived and not editable via `update_game`.

- [ ] **Step 1: Write the failing test**

Read one existing test in `tests/test_igdb_apply_metadata.py` for the `IGDBGame` construction + invocation pattern, then add:

```python
    async def test_writes_igdb_platforms_json(self):
        game_id = await seed_game("Crossplay Game")
        igdb_game = _make_igdb_game(igdb_id=901, platforms=[6, 130, 508])  # use the file's factory/ctor pattern
        await igdb._apply_igdb_metadata(game_id, igdb_game)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (game_id,)
            )
        self.assertEqual(json.loads(row["igdb_platforms"]), [6, 130, 508])

    async def test_empty_platforms_leaves_column_null(self):
        game_id = await seed_game("No Platform Data")
        igdb_game = _make_igdb_game(igdb_id=902, platforms=[])
        await igdb._apply_igdb_metadata(game_id, igdb_game)
        async with db_module.get_db() as db:
            row = await db.execute_fetchone(
                "SELECT igdb_platforms FROM games WHERE id = ?", (game_id,)
            )
        self.assertIsNone(row["igdb_platforms"])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_igdb_apply_metadata.py -q -k igdb_platforms`
Expected: FAIL (column stays NULL).

- [ ] **Step 3: Implement**

In `_apply_igdb_metadata`, immediately after `updates: dict = {"igdb_id": igdb_game.igdb_id, "igdb_cached_at": now}`:

```python
        if igdb_game.platforms:
            # NULL means "not fetched yet"; an empty fetch keeps NULL so the
            # deals tool can distinguish unknown from confirmed-single-platform.
            updates["igdb_platforms"] = json.dumps(igdb_game.platforms)
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_igdb_apply_metadata.py tests/test_igdb.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/data/igdb.py tests/test_igdb_apply_metadata.py
git commit -m "feat: persist IGDB platform availability to games.igdb_platforms"
```

---

### Task 4: DekuDeals per-title search pricing

**Files:**
- Modify: `gamelib_mcp/data/scrape_config.py` (`DekuDealsScrapeConfig` ~line 165; `ALLOWED_HOSTS` ~line 203)
- Modify: `gamelib_mcp/data/dekudeals.py`
- Create: `gamelib_mcp/data/scrape_fixtures/dekudeals_search_page.html`
- Test: `tests/test_scrape_parsers.py` (search-fixture parse), `tests/test_wishlist.py` or a new `tests/test_dekudeals_search.py` (fetch_search_prices unit tests — put them wherever the existing `fetch_wishlist_prices` tests live; check both files first)

**Interfaces:**
- Produces: `async def fetch_search_prices(titles: list[str]) -> dict[str, dict]` in `gamelib_mcp/data/dekudeals.py` — keys are the **requested** titles; values are the same price-dict shape `_parse_wishlist_prices` returns (`price, regular_price, cut_pct, currency, deal_url`). Task 6/7 consume it.
- Produces: `DekuDealsScrapeConfig.search_url_template` (healable), `ALLOWED_HOSTS["dekudeals"]` non-empty.

- [ ] **Step 1: Capture the fixture**

```bash
curl -s "https://www.dekudeals.com/search?q=hades" -H "User-Agent: gamelib-mcp/1.0" \
  -o gamelib_mcp/data/scrape_fixtures/dekudeals_search_page.html
```

Sanity check it parsed (verified working 2026-07-03 — 7 cards, `.d-block.col` / `a.main-link h6` / `.text-tight strong`, identical to the wishlist page markup):

```bash
.venv/bin/python - <<'EOF'
from bs4 import BeautifulSoup
soup = BeautifulSoup(open('gamelib_mcp/data/scrape_fixtures/dekudeals_search_page.html').read(), 'lxml')
cards = soup.select('.d-block.col')
assert len(cards) >= 3, cards
assert any(c.select_one('a.main-link h6') and c.select_one('a.main-link h6').get_text(strip=True) == 'Hades' for c in cards)
print('fixture ok,', len(cards), 'cards')
EOF
```

- [ ] **Step 2: Write the failing tests**

In `tests/test_scrape_parsers.py` (match its fixture-loading conventions):

```python
class DekuDealsSearchParseTests(unittest.TestCase):
    def test_search_page_parses_with_wishlist_selectors(self):
        html = (FIXTURES_DIR / "dekudeals_search_page.html").read_text(encoding="utf-8")
        results = dekudeals._parse_wishlist_prices(html)
        self.assertIn("Hades", results)
        hades = results["Hades"]
        self.assertGreater(hades["price"], 0)
        self.assertEqual(hades["currency"], "EUR")
        self.assertTrue(hades["deal_url"].endswith("/items/hades"))
```

(Assert structure, not exact prices — the fixture is frozen but re-captures must not break the test.)

For `fetch_search_prices` (async tests, patching `httpx` per the existing `_fetch_wishlist_items` test pattern in the file that tests it):

```python
class FetchSearchPricesTests(unittest.IsolatedAsyncioTestCase):
    async def test_maps_requested_title_to_matched_card(self):
        html = (FIXTURES_DIR / "dekudeals_search_page.html").read_text(encoding="utf-8")
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_response(html))  # use the file's response-stub helper
            results = await dekudeals.fetch_search_prices(["Hades"])
        self.assertIn("Hades", results)
        self.assertEqual(results["Hades"]["currency"], "EUR")

    async def test_fetch_error_skips_title_without_raising(self):
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
            results = await dekudeals.fetch_search_prices(["Hades"])
        self.assertEqual(results, {})

    async def test_no_fuzzy_match_yields_no_entry(self):
        html = (FIXTURES_DIR / "dekudeals_search_page.html").read_text(encoding="utf-8")
        with patch("gamelib_mcp.data.dekudeals.httpx.AsyncClient") as client_cls:
            client = client_cls.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=_response(html))
            results = await dekudeals.fetch_search_prices(["Completely Unrelated Title XYZ"])
        self.assertEqual(results, {})
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_scrape_parsers.py -q -k DekuDealsSearch` (plus the fetch tests' file)
Expected: FAIL — `fetch_search_prices` undefined.

- [ ] **Step 4: Implement**

`scrape_config.py` — add to `DekuDealsScrapeConfig` (after `fuzzy_cutoff`, before the price-scrape selectors, with a comment):

```python
    # Per-title price lookup (public search page). Search results render the
    # same card markup as the shared-wishlist page, so the wishlist selectors
    # below are shared by both parses (verified live 2026-07-03).
    search_url_template: str = field(
        default="https://www.dekudeals.com/search?q={query}",
        metadata={"kind": "url_template", "placeholders": frozenset({"query"})},
    )
```

`ALLOWED_HOSTS` — replace the dekudeals line:

```python
    "dekudeals": frozenset({"dekudeals.com", "www.dekudeals.com"}),
```

(and drop the now-wrong `# no configurable URLs` comment).

`dekudeals.py` — add `import asyncio` and `from urllib.parse import quote_plus, urlsplit` (extend the existing urlsplit import), a module constant, and the two functions:

```python
# Politeness delay between per-title search requests (fetch_search_prices).
_SEARCH_REQUEST_DELAY_SECONDS = 0.5


def _match_search_title(title: str, prices: dict[str, dict], cutoff: int) -> str | None:
    """Best search-result title for a requested title, or None. Exact
    (case-insensitive) first, then the same fuzzy matcher the wishlist
    sync uses."""
    by_lower = {t.lower(): t for t in prices}
    title_lower = title.lower()
    if title_lower in by_lower:
        return by_lower[title_lower]
    match = extract_best_fuzzy_key(title_lower, {k: k for k in by_lower}, cutoff=cutoff)
    return by_lower[match] if match else None


async def fetch_search_prices(titles: list[str]) -> dict[str, dict]:
    """Current switch2 prices for arbitrary titles via the public DekuDeals
    search page — used for games NOT on the shared wishlist (e.g. a
    Steam-wishlisted game that also has a Switch release).

    One GET per title; results parse with the same selector config as the
    wishlist page (identical card markup). Returns {requested_title:
    price_dict}. Per-title failures and non-matches are skipped rather than
    raised: unlike the wishlist scrape there is no removal reconciliation
    downstream, so a miss just leaves that item unpriced.
    """
    if not titles:
        return {}
    config = await load_scrape_config("dekudeals")
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "gamelib-mcp/1.0"}) as client:
        for i, title in enumerate(titles):
            if i:
                await asyncio.sleep(_SEARCH_REQUEST_DELAY_SECONDS)
            url = config.search_url_template.format(query=quote_plus(title))
            try:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("DekuDeals search failed for %r: %s", title, exc)
                continue
            prices = _parse_wishlist_prices(resp.text, config)
            matched = _match_search_title(title, prices, cutoff=config.fuzzy_cutoff)
            if matched is not None:
                results[title] = prices[matched]
    return results
```

Also update the module docstring's scope sentence to mention the search-page lookup.

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_scrape_parsers.py tests/test_wishlist.py tests/test_scrape_admin.py tests/test_scrape_validate.py -q` (the last two guard the config/validation surface — new field must not break override validation; if `test_scrape_validate.py` has a "known fields" expectation, add `search_url_template`).
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gamelib_mcp/data/scrape_config.py gamelib_mcp/data/dekudeals.py gamelib_mcp/data/scrape_fixtures/dekudeals_search_page.html tests/
git commit -m "feat: DekuDeals per-title price lookup via public search page"
```

---

### Task 5: Loader — cross-platform prices, ownership, availability

**Files:**
- Modify: `gamelib_mcp/data/db/queries.py` (`load_wishlist_with_prices`, line 393)
- Test: `tests/test_wishlist.py` (it already imports `db_module`; the existing loader tests, if any, live near the wishlist tests — search the file for `load_wishlist_with_prices` and extend in place)

**Interfaces:**
- Produces (row columns consumed by Task 7): existing columns plus `price_platform` (the `game_prices.platform` of the joined price row, NULL when no price rows), `igdb_platforms` (JSON text or NULL), `igdb_cached_at`, `owned_platforms` (JSON array text of platforms with `owned=1`, `'[]'` when none). **Join change:** `LEFT JOIN game_prices gp ON gp.game_id = w.game_id` — no longer also on platform — so one wishlist row now fans out per cached price row on *any* platform.

- [ ] **Step 1: Write the failing test**

```python
class LoadWishlistWithPricesCrossPlatformTests(ToolDBTestCase):
    async def test_returns_prices_from_other_platforms_with_metadata(self):
        game_id = await seed_game("Crossplay Deal")
        async with db_module.get_db() as db:
            await db.execute(
                "UPDATE games SET igdb_platforms = '[6, 508]', igdb_cached_at = 'x' WHERE id = ?",
                (game_id,),
            )
            await db.commit()
        await add_platform(game_id, "epic", owned=1)  # owned elsewhere; not on candidates
        await db_module.upsert_wishlist_entry(game_id, "steam", source="steam", store_identifier="42")
        await db_module.upsert_game_prices([
            {"game_id": game_id, "platform": "steam", "shop": "Steam", "price": 10.0,
             "regular_price": 10.0, "cut_pct": 0, "currency": "EUR", "deal_url": "u1"},
            {"game_id": game_id, "platform": "switch2", "shop": "dekudeals", "price": 12.0,
             "regular_price": 12.0, "cut_pct": 0, "currency": "EUR", "deal_url": "u2"},
        ])

        rows = await db_module.load_wishlist_with_prices(None)
        mine = [r for r in rows if r["game_id"] == game_id]
        self.assertEqual({r["price_platform"] for r in mine}, {"steam", "switch2"})
        self.assertEqual(json.loads(mine[0]["igdb_platforms"]), [6, 508])
        self.assertEqual(json.loads(mine[0]["owned_platforms"]), ["epic"])
        self.assertEqual(mine[0]["steam_appid"], 42)

    async def test_platform_filter_still_filters_wishlist_rows_not_price_rows(self):
        game_id = await seed_game("Filtered")
        await db_module.upsert_wishlist_entry(game_id, "switch2", source="dekudeals")
        rows = await db_module.load_wishlist_with_prices("steam")
        self.assertEqual([r for r in rows if r["game_id"] == game_id], [])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_wishlist.py -q -k CrossPlatform`
Expected: FAIL — `no such column: price_platform` (or KeyError).

- [ ] **Step 3: Implement**

Replace the SELECT in `load_wishlist_with_prices` (keep the surrounding function and `where` handling; update the docstring's join-shape paragraph to say the join is now on `game_id` only, deliberately fanning out across platforms, with `price_platform` disambiguating):

```python
        rows = await db.execute_fetchall(
            f"""SELECT w.game_id, g.name, w.platform, w.wishlisted_at, w.source,
                       w.store_identifier,
                       g.igdb_platforms, g.igdb_cached_at,
                       (
                           SELECT COALESCE(json_group_array(sgp2.platform), '[]')
                           FROM game_platforms sgp2
                           WHERE sgp2.game_id = w.game_id AND sgp2.owned = 1
                       ) AS owned_platforms,
                       COALESCE(
                           CAST(w.store_identifier AS INTEGER),
                           (
                               SELECT CAST(gpi.identifier_value AS INTEGER)
                               FROM game_platform_identifiers gpi
                               JOIN game_platforms sgp ON sgp.id = gpi.game_platform_id
                               WHERE sgp.game_id = w.game_id
                                 AND gpi.identifier_type = '{STEAM_APP_ID}'
                               ORDER BY gpi.is_primary DESC, gpi.id ASC
                               LIMIT 1
                           )
                       ) AS steam_appid,
                       gp.platform AS price_platform,
                       gp.shop, gp.price, gp.regular_price, gp.cut_pct,
                       gp.currency, gp.deal_url, gp.fetched_at
                FROM game_wishlist w
                JOIN games g ON g.id = w.game_id
                LEFT JOIN game_prices gp ON gp.game_id = w.game_id
                {where}""",
            params,
        )
```

(Preserve any trailing ORDER BY the current query has — check the lines just after 439.)

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_wishlist.py tests/test_tools_deals.py -q`
Expected: the new loader tests PASS. **`tests/test_tools_deals.py` may fail** — the tool still groups by `(game_id, row["platform"])` and rows now fan out; failures here are expected and fixed in Task 7. If it fails, note which tests and proceed (do not "fix" the tool here); if the suite must stay green per-commit, squash Tasks 5+7 into one commit at the end of Task 7 instead.

- [ ] **Step 5: Commit** (or defer per the note above)

```bash
git add gamelib_mcp/data/db/queries.py tests/test_wishlist.py
git commit -m "feat: wishlist price loader returns cross-platform prices + availability/ownership metadata"
```

---

### Task 6: deals.py pure helpers — candidates + recommendation rule

**Files:**
- Modify: `gamelib_mcp/tools/deals.py` (add helpers; no flow changes yet)
- Test: `tests/test_tools_deals.py`

**Interfaces:**
- Produces (Task 7 consumes):
  - `_available_platforms(igdb_platforms_json: str | None) -> set[str]`
  - `_candidate_platforms(wishlisted_on: set[str], available: set[str], owned: set[str], hw_pref: list[str]) -> set[str]`
  - `_pick_recommended(options: list[dict], hw_pref: list[str], override_ratio: float) -> tuple[dict, str]` — `options` are per-platform cheapest dicts each containing at least `platform` and `price`; returns `(chosen_option, reason)`.
  - Module constants: `_PRICEABLE_PLATFORMS = frozenset({"steam", "switch2"})`, `_MAX_SWITCH2_SEARCH_LOOKUPS = 12`, `_DEFAULT_OVERRIDE_RATIO = 0.5`.

- [ ] **Step 1: Write the failing tests**

```python
class DealsPureHelperTests(unittest.TestCase):
    def _opt(self, platform, price):
        return {"platform": platform, "price": price}

    def test_available_platforms_maps_igdb_ids(self):
        self.assertEqual(deals._available_platforms("[6, 130, 508, 167, 999]"),
                         {"steam", "switch2", "ps5"})
        self.assertEqual(deals._available_platforms(None), set())
        self.assertEqual(deals._available_platforms("not json"), set())

    def test_candidates_add_preferred_available_unowned_priceable(self):
        got = deals._candidate_platforms(
            wishlisted_on={"steam"}, available={"steam", "switch2", "ps5"},
            owned=set(), hw_pref=["switch2", "steam"],
        )
        self.assertEqual(got, {"steam", "switch2"})  # ps5 has no price source

    def test_candidates_exclude_owned_and_respect_empty_pref(self):
        self.assertEqual(
            deals._candidate_platforms({"steam"}, {"steam", "switch2"}, {"switch2"}, ["switch2"]),
            {"steam"},
        )
        self.assertEqual(
            deals._candidate_platforms({"steam"}, {"steam", "switch2"}, set(), []),
            {"steam"},
        )

    def test_pick_recommended_prefers_preferred_platform(self):
        options = [self._opt("steam", 10.0), self._opt("switch2", 14.0)]
        chosen, reason = deals._pick_recommended(options, ["switch2", "steam"], 0.5)
        self.assertEqual(chosen["platform"], "switch2")
        self.assertIn("preferred", reason)

    def test_pick_recommended_overrides_when_deal_too_good(self):
        options = [self._opt("steam", 4.99), self._opt("switch2", 14.0)]
        chosen, reason = deals._pick_recommended(options, ["switch2", "steam"], 0.5)
        self.assertEqual(chosen["platform"], "steam")
        self.assertIn("override", reason)

    def test_pick_recommended_boundary_is_strict(self):
        options = [self._opt("steam", 7.0), self._opt("switch2", 14.0)]
        chosen, _ = deals._pick_recommended(options, ["switch2"], 0.5)
        self.assertEqual(chosen["platform"], "switch2")  # 7.0 is NOT < 0.5*14.0

    def test_pick_recommended_no_pref_returns_cheapest(self):
        options = [self._opt("switch2", 14.0), self._opt("steam", 10.0)]
        chosen, reason = deals._pick_recommended(options, [], 0.5)
        self.assertEqual(chosen["platform"], "steam")
        self.assertEqual(reason, "cheapest available")
```

(Import `unittest` in the file if not present; these are sync tests.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_tools_deals.py -q -k PureHelper`
Expected: FAIL — attributes missing.

- [ ] **Step 3: Implement in `gamelib_mcp/tools/deals.py`**

Add near the top (after `_PRICE_TTL_HOURS`):

```python
# Platforms this tool has a price source for: ITAD (steam) / DekuDeals (switch2).
_PRICEABLE_PLATFORMS = frozenset({"steam", "switch2"})
# Cap on per-title DekuDeals search lookups per call — each is a live page
# fetch with a politeness delay, so a cold cache prices at most this many
# cross-platform candidates per call and defers the rest (12h TTL staggers
# the remainder across subsequent calls).
_MAX_SWITCH2_SEARCH_LOOKUPS = 12
_DEFAULT_OVERRIDE_RATIO = 0.5
```

Helpers:

```python
def _available_platforms(igdb_platforms_json: str | None) -> set[str]:
    """Internal platforms a game is released on, per games.igdb_platforms."""
    from ..data.igdb import IGDB_TO_PLATFORM

    if not igdb_platforms_json:
        return set()
    try:
        ids = json.loads(igdb_platforms_json)
    except ValueError:
        return set()
    if not isinstance(ids, list):
        return set()
    return {IGDB_TO_PLATFORM[i] for i in ids if isinstance(i, int) and i in IGDB_TO_PLATFORM}


def _candidate_platforms(
    wishlisted_on: set[str], available: set[str], owned: set[str], hw_pref: list[str]
) -> set[str]:
    """Platforms worth pricing for one game: where it's wishlisted, plus any
    hardware-preference platform it's available on, priceable, and not
    already owned (no point recommending a purchase of an owned copy)."""
    candidates = set(wishlisted_on) & _PRICEABLE_PLATFORMS
    for platform in hw_pref:
        if (
            platform in _PRICEABLE_PLATFORMS
            and platform in available
            and platform not in owned
        ):
            candidates.add(platform)
    return candidates


def _pick_recommended(
    options: list[dict], hw_pref: list[str], override_ratio: float
) -> tuple[dict, str]:
    """Choose which per-platform option to recommend.

    Preference order wins unless a non-preferred option's price drops
    strictly below override_ratio × the preferred price ("the deal is just
    too good"). options must be non-empty; prices are compared raw (no
    currency conversion — the caller flags mixed currencies)."""
    best = min(options, key=lambda o: o["price"])
    preferred = next(
        (
            min((o for o in options if o["platform"] == platform), key=lambda o: o["price"])
            for platform in hw_pref
            if any(o["platform"] == platform for o in options)
        ),
        None,
    )
    if preferred is None:
        return best, "cheapest available"
    if preferred["platform"] == best["platform"]:
        return preferred, f"cheapest available (also preferred platform {preferred['platform']})"
    if best["price"] < override_ratio * preferred["price"]:
        return best, (
            f"preference override: {best['platform']} at {best['price']} is below "
            f"{int(override_ratio * 100)}% of preferred {preferred['platform']} "
            f"price {preferred['price']}"
        )
    return preferred, (
        f"preferred platform {preferred['platform']} at {preferred['price']} "
        f"(cheapest elsewhere: {best['price']} on {best['platform']})"
    )
```

Add `import json` to the module imports.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_tools_deals.py -q -k PureHelper`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/tools/deals.py tests/test_tools_deals.py
git commit -m "feat: candidate-platform and preference/override recommendation helpers"
```

---

### Task 7: `get_wishlist_deals` — preference-aware flow

**Files:**
- Modify: `gamelib_mcp/tools/deals.py` (rewrite `get_wishlist_deals` + grouping; keep `_fetched_at_is_stale`, `_match_wishlist_game_id` as-is)
- Test: `tests/test_tools_deals.py` (new behavior tests + update existing characterization tests)

**Interfaces:**
- Consumes: loader columns from Task 5, helpers from Task 6, `fetch_search_prices` from Task 4 (import it in `deals.py` next to `fetch_wishlist_prices`), `get_meta` (import from `..data.db` — mirror `tools/discover.py:196`'s `hardware_preference` read).
- Produces: `get_wishlist_deals(platform, max_price, min_cut_pct, refresh, preference_override_ratio=_DEFAULT_OVERRIDE_RATIO)`. Response: **one entry per game** (previously per game×wishlist-platform). Each entry keeps the flat recommended-option fields (`platform, shop, price, regular_price, cut_pct, currency, deal_url`) so existing consumers read the same keys, plus `wishlisted_on: list[str]`, `recommendation_reason: str`, `alternatives: list[dict]` (other platforms' cheapest options, same flat shape minus name/game_id). Response gains optional `switch2_lookups_deferred: int` and `availability_pending: int`.

- [ ] **Step 1: Write the failing behavior tests**

Add to `tests/test_tools_deals.py` (using its existing `_seed_wishlist`/`_seed_price` helpers; add a small `_seed_meta_pref` helper and an `_set_igdb_platforms` helper):

```python
async def _set_igdb_platforms(game_id: int, ids: list[int]) -> None:
    async with db_module.get_db() as db:
        await db.execute(
            "UPDATE games SET igdb_platforms = ?, igdb_cached_at = 'x' WHERE id = ?",
            (json.dumps(ids), game_id),
        )
        await db.commit()


async def _set_hw_pref(platforms: list[str]) -> None:
    await db_module.set_meta("hardware_preference", json.dumps(platforms))


class PreferenceAwareDealsTests(ToolDBTestCase):
    async def test_steam_item_with_switch_release_gets_search_priced_and_preferred(self):
        game_id = await seed_game("Crossplay Game")
        await _seed_wishlist(game_id, "steam", store_identifier="42")
        await _set_igdb_platforms(game_id, [6, 508])
        await _set_hw_pref(["switch2", "steam"])

        itad = AsyncMock(return_value={42: PriceInfo("Steam", 10.0, 10.0, 0, "EUR", "u1")})
        search = AsyncMock(return_value={"Crossplay Game": {
            "price": 12.0, "regular_price": 12.0, "cut_pct": 0,
            "currency": "EUR", "deal_url": "u2"}})
        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", itad), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", search), \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_awaited_once_with(["Crossplay Game"])
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "switch2")   # preferred wins at 12.0 vs 10.0
        self.assertEqual(entry["price"], 12.0)
        self.assertIn("preferred", entry["recommendation_reason"])
        self.assertEqual(entry["alternatives"][0]["platform"], "steam")
        self.assertEqual(entry["wishlisted_on"], ["steam"])

    async def test_override_when_steam_deal_too_good(self):
        game_id = await seed_game("Bargain Game")
        await _seed_wishlist(game_id, "steam", store_identifier="43")
        await _set_igdb_platforms(game_id, [6, 130])
        await _set_hw_pref(["switch2", "steam"])
        await _seed_price(game_id, "steam", "Steam", 4.99, currency="EUR")
        await _seed_price(game_id, "switch2", "dekudeals", 14.0, currency="EUR")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_not_awaited()  # both platforms freshly cached
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "steam")
        self.assertIn("override", entry["recommendation_reason"])

    async def test_owned_on_switch2_suppresses_candidate(self):
        game_id = await seed_game("Already On Switch")
        await add_platform(game_id, "switch2", owned=1)
        await _seed_wishlist(game_id, "steam", store_identifier="44")
        await _set_igdb_platforms(game_id, [6, 508])
        await _set_hw_pref(["switch2", "steam"])
        await _seed_price(game_id, "steam", "Steam", 9.0, currency="EUR")

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        search.assert_not_awaited()
        entry = next(d for d in result["deals"] if d["game_id"] == game_id)
        self.assertEqual(entry["platform"], "steam")

    async def test_search_lookup_cap_defers_overflow(self):
        await _set_hw_pref(["switch2"])
        for i in range(deals._MAX_SWITCH2_SEARCH_LOOKUPS + 3):
            gid = await seed_game(f"Cap Game {i}")
            await _seed_wishlist(gid, "steam", store_identifier=str(100 + i))
            await _set_igdb_platforms(gid, [6, 508])

        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock(return_value={})) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()

        self.assertEqual(len(search.await_args.args[0]), deals._MAX_SWITCH2_SEARCH_LOOKUPS)
        self.assertEqual(result["switch2_lookups_deferred"], 3)

    async def test_availability_pending_counts_unfetched_games(self):
        gid = await seed_game("IGDB Pending")
        await _seed_wishlist(gid, "steam", store_identifier="77")
        # igdb_cached_at stays NULL from seed_game
        with patch("gamelib_mcp.tools.deals.fetch_steam_prices", AsyncMock(return_value={})), \
             patch("gamelib_mcp.tools.deals.fetch_search_prices", AsyncMock()) as search, \
             patch("gamelib_mcp.tools.deals.fetch_wishlist_prices", AsyncMock()), \
             patch("gamelib_mcp.tools.deals.is_itad_configured", return_value=True):
            result = await deals.get_wishlist_deals()
        search.assert_not_awaited()
        self.assertEqual(result["availability_pending"], 1)
```

(Check `seed_game` leaves `igdb_cached_at` NULL; if not, set it explicitly.)

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_tools_deals.py -q -k PreferenceAware`
Expected: FAIL — no `fetch_search_prices` import / no `recommendation_reason` key.

- [ ] **Step 3: Implement the new flow**

In `gamelib_mcp/tools/deals.py`: add imports `from ..data.db import get_meta` (extend the existing `..data.db` import block) and `from ..data.dekudeals import fetch_search_prices, fetch_wishlist_prices`.

Replace `_group_by_key`/`_key_needs_refresh` with game-level grouping and per-platform staleness, and rewrite `get_wishlist_deals`:

```python
def _group_rows_by_game(rows: list[aiosqlite.Row]) -> dict[int, dict]:
    """Collapse loader rows (wishlist-row × price-row fan-out) per game.

    Returns {game_id: {"name", "wishlisted_on": {platform: wishlisted_at},
    "steam_appid", "igdb_platforms", "igdb_cached_at", "owned_platforms",
    "prices": {price_platform: {shop_key: row}}}}.
    """
    games: dict[int, dict] = {}
    for row in rows:
        state = games.setdefault(
            row["game_id"],
            {
                "name": row["name"],
                "wishlisted_on": {},
                "steam_appid": row["steam_appid"],
                "igdb_platforms": row["igdb_platforms"],
                "igdb_cached_at": row["igdb_cached_at"],
                "owned_platforms": set(json.loads(row["owned_platforms"] or "[]")),
                "prices": {},
            },
        )
        state["wishlisted_on"].setdefault(row["platform"], row["wishlisted_at"])
        if row["steam_appid"] is not None:
            state["steam_appid"] = row["steam_appid"]
        if row["price_platform"] is not None:
            state["prices"].setdefault(row["price_platform"], {})[row["shop"]] = row
    return games


def _platform_needs_refresh(price_rows: dict, refresh: bool) -> bool:
    if refresh:
        return True
    priced = [r for r in price_rows.values() if r["price"] is not None]
    if not priced:
        return True
    return _fetched_at_is_stale(max(r["fetched_at"] for r in priced))
```

New `get_wishlist_deals` (full body — replaces the old one; keep the module docstring in sync):

```python
async def get_wishlist_deals(
    platform: str | None = None,
    max_price: float | None = None,
    min_cut_pct: int | None = None,
    refresh: bool = False,
    preference_override_ratio: float = _DEFAULT_OVERRIDE_RATIO,
) -> dict:
    """
    Current prices/deals for wishlist games, one entry per game, with a
    platform recommendation honoring hardware_preference.

    Prices come from IsThereAnyDeal (Steam wishlist items; covers Steam/GOG/
    Epic shops) and DekuDeals (switch2 items — both the shared-wishlist page
    and, for games wishlisted elsewhere that IGDB says also have a Switch
    release, per-title search lookups, capped per call). Cached 12h;
    refresh=True forces a live fetch. Each deal's flat fields describe the
    RECOMMENDED option (preferred platform unless another platform's price is
    below preference_override_ratio × the preferred price); other platforms'
    cheapest options appear in alternatives with the reasoning in
    recommendation_reason. platform filters by where the game is WISHLISTED,
    not where the recommendation lands. Prices are never currency-converted.
    """
    resolved_platform = _validate_platform(platform, LIBRARY_PLATFORMS) if platform else None

    hw_pref_raw = await get_meta("hardware_preference")
    hw_pref: list[str] = json.loads(hw_pref_raw) if hw_pref_raw else []

    rows = await load_wishlist_with_prices(resolved_platform)
    games = _group_rows_by_game(rows)

    # Partition stale (game, platform) pricing needs by provider.
    steam_needs_refresh: dict[int, int] = {}      # appid -> game_id
    switch2_wishlist_needs: dict[int, str] = {}   # game_id -> name (on the deku wishlist page)
    switch2_search_needs: dict[int, str] = {}     # game_id -> name (search lookup)
    availability_pending = 0

    for game_id, state in games.items():
        if state["igdb_cached_at"] is None:
            availability_pending += 1
        candidates = _candidate_platforms(
            set(state["wishlisted_on"]),
            _available_platforms(state["igdb_platforms"]),
            state["owned_platforms"],
            hw_pref,
        )
        for cand in candidates:
            if not _platform_needs_refresh(state["prices"].get(cand, {}), refresh):
                continue
            if cand == "steam":
                if state["steam_appid"] is not None:
                    steam_needs_refresh[int(state["steam_appid"])] = game_id
            elif cand == "switch2":
                if "switch2" in state["wishlisted_on"]:
                    switch2_wishlist_needs[game_id] = state["name"]
                else:
                    switch2_search_needs[game_id] = state["name"]

    switch2_lookups_deferred = max(0, len(switch2_search_needs) - _MAX_SWITCH2_SEARCH_LOOKUPS)
    if switch2_lookups_deferred:
        switch2_search_needs = dict(list(switch2_search_needs.items())[:_MAX_SWITCH2_SEARCH_LOOKUPS])

    price_refresh_errors: list[str] = []
    notes: dict[str, Any] = {}
    cache_updated = False

    if steam_needs_refresh:
        if not is_itad_configured():
            notes["itad"] = "unconfigured"
        else:
            try:
                prices = await fetch_steam_prices(list(steam_needs_refresh.keys()))
            except Exception as exc:
                logger.warning("ITAD price refresh failed: %s", exc)
                price_refresh_errors.append(f"itad refresh failed: {exc}")
            else:
                upsert_rows = [
                    {
                        "game_id": steam_needs_refresh[appid],
                        "platform": "steam",
                        "shop": info.shop,
                        "price": info.price,
                        "regular_price": info.regular_price,
                        "cut_pct": info.cut_pct,
                        "currency": info.currency,
                        "deal_url": info.deal_url,
                    }
                    for appid, info in prices.items()
                    if appid in steam_needs_refresh
                ]
                if upsert_rows:
                    await upsert_game_prices(upsert_rows)
                    cache_updated = True

    if switch2_wishlist_needs:
        try:
            prices_by_title = await fetch_wishlist_prices()
        except Exception as exc:
            logger.warning("DekuDeals price refresh failed: %s", exc)
            price_refresh_errors.append(f"dekudeals refresh failed: {exc}")
        else:
            config = await load_scrape_config("dekudeals")
            name_to_id = {name.lower(): gid for gid, name in switch2_wishlist_needs.items()}
            candidate_names = {name: name for name in name_to_id}
            upsert_rows = []
            for title, info in prices_by_title.items():
                matched_game_id = _match_wishlist_game_id(
                    title, candidate_names, name_to_id, cutoff=config.fuzzy_cutoff
                )
                if matched_game_id is None:
                    continue
                upsert_rows.append(_switch2_price_row(matched_game_id, info))
            if upsert_rows:
                await upsert_game_prices(upsert_rows)
                cache_updated = True

    if switch2_search_needs:
        try:
            by_title = await fetch_search_prices(list(switch2_search_needs.values()))
        except Exception as exc:  # fetch_search_prices is fail-soft; this is belt-and-braces
            logger.warning("DekuDeals search price refresh failed: %s", exc)
            price_refresh_errors.append(f"dekudeals search refresh failed: {exc}")
        else:
            name_to_id = {name: gid for gid, name in switch2_search_needs.items()}
            upsert_rows = [
                _switch2_price_row(name_to_id[title], info)
                for title, info in by_title.items()
                if title in name_to_id
            ]
            if upsert_rows:
                await upsert_game_prices(upsert_rows)
                cache_updated = True

    if cache_updated:
        rows = await load_wishlist_with_prices(resolved_platform)
        games = _group_rows_by_game(rows)

    deals: list[dict[str, Any]] = []
    unpriced: list[str] = []
    for game_id, state in games.items():
        options = []
        for price_platform, by_shop in state["prices"].items():
            priced = [r for r in by_shop.values() if r["price"] is not None]
            if not priced:
                continue
            cheapest = min(priced, key=lambda r: r["price"])
            options.append(
                {
                    "platform": price_platform,
                    "shop": cheapest["shop"],
                    "price": cheapest["price"],
                    "regular_price": cheapest["regular_price"],
                    "cut_pct": cheapest["cut_pct"],
                    "currency": cheapest["currency"],
                    "deal_url": cheapest["deal_url"],
                }
            )
        if not options:
            unpriced.append(state["name"])
            continue
        recommended, reason = _pick_recommended(options, hw_pref, preference_override_ratio)
        deals.append(
            {
                "game_id": game_id,
                "name": state["name"],
                **recommended,
                "wishlisted_at": min(state["wishlisted_on"].values()),
                "wishlisted_on": sorted(state["wishlisted_on"]),
                "recommendation_reason": reason,
                "alternatives": [o for o in options if o is not recommended],
            }
        )

    currencies = {
        c
        for d in deals
        for c in [d["currency"], *(a["currency"] for a in d["alternatives"])]
        if c is not None
    }

    if max_price is not None:
        deals = [d for d in deals if d["price"] <= max_price]
    if min_cut_pct is not None:
        deals = [d for d in deals if d["cut_pct"] is not None and d["cut_pct"] >= min_cut_pct]

    deals.sort(key=lambda d: d["price"])

    response: dict[str, Any] = {
        "deals": deals,
        "unpriced": unpriced,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(deals),
    }
    if price_refresh_errors:
        response["price_refresh_errors"] = price_refresh_errors
    if switch2_lookups_deferred:
        response["switch2_lookups_deferred"] = switch2_lookups_deferred
    if availability_pending:
        response["availability_pending"] = availability_pending
    if len(currencies) > 1:
        response["currency_note"] = (
            f"deals span multiple currencies ({', '.join(sorted(currencies))}); "
            "max_price/min_cut_pct/preference_override_ratio are not currency-converted"
        )
    response.update(notes)
    return response
```

Plus the small shared row builder:

```python
def _switch2_price_row(game_id: int, info: dict) -> dict:
    return {
        "game_id": game_id,
        "platform": "switch2",
        "shop": "dekudeals",
        "price": info.get("price"),
        "regular_price": info.get("regular_price"),
        "cut_pct": info.get("cut_pct"),
        "currency": info.get("currency"),
        "deal_url": info.get("deal_url"),
    }
```

Update the module docstring's first paragraph to describe game-level grouping + preference ranking.

- [ ] **Step 4: Update existing characterization tests, run the file**

Run: `.venv/bin/python -m pytest tests/test_tools_deals.py -q`
Expected failures to fix in the *tests* (behavior-shape changes, all intentional):
- Entries now carry `wishlisted_on`, `recommendation_reason`, `alternatives` — assertions doing exact-dict comparison need the new keys.
- A game wishlisted on two platforms now yields **one** entry (was two).
- Tests that patched only `fetch_steam_prices`/`fetch_wishlist_prices` must also patch `gamelib_mcp.tools.deals.fetch_search_prices` (an unpatched call would hit the network only if candidates exist — patch it everywhere for hygiene).
- With no `hardware_preference` meta and no `igdb_platforms`, per-game behavior (cheapest-of-cached, unpriced handling, TTL logic) must be unchanged — if such a test fails on values (not shape), treat it as a regression in the implementation, not the test.

- [ ] **Step 5: Run wishlist + deals + full targeted set**

Run: `.venv/bin/python -m pytest tests/test_tools_deals.py tests/test_wishlist.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add gamelib_mcp/tools/deals.py tests/test_tools_deals.py
git commit -m "feat: preference-aware cross-platform wishlist deals with override rule"
```

---

### Task 8: Wire schema — models, main.py, docs, full verification

**Files:**
- Modify: `gamelib_mcp/tools/models.py` (`WishlistDealEntry` line 320, `WishlistDealsResponse` line 333)
- Modify: `gamelib_mcp/main.py` (`get_wishlist_deals` tool, line ~646)
- Modify: `CLAUDE.md` (tools/deals.py bullet; data/dekudeals.py bullet; "Wishlist tracking" design-pattern paragraph; games-table bullet under Database)
- Test: `tests/test_tool_registration.py` (if it asserts tool signatures/response models — check)

**Interfaces:**
- Consumes: Task 7's response shape verbatim.

- [ ] **Step 1: Update models**

```python
class WishlistDealAlternative(FlexibleModel):
    platform: str
    shop: str
    price: float
    regular_price: float | None = None
    cut_pct: int | None = None
    currency: str | None = None
    deal_url: str | None = None


class WishlistDealEntry(FlexibleModel):
    game_id: int
    name: str
    platform: str
    shop: str
    price: float
    regular_price: float | None = None
    cut_pct: int | None = None
    currency: str | None = None
    deal_url: str | None = None
    wishlisted_at: str | None = None
    wishlisted_on: list[str] = []
    recommendation_reason: str | None = None
    alternatives: list[WishlistDealAlternative] = []


class WishlistDealsResponse(FlexibleModel):
    deals: list[WishlistDealEntry]
    unpriced: list[str]
    fetched_at: str
    count: int
    price_refresh_errors: list[str] | None = None
    itad: str | None = None
    currency_note: str | None = None
    switch2_lookups_deferred: int | None = None
    availability_pending: int | None = None
```

(If `FlexibleModel` is pydantic, use `Field(default_factory=list)` for the mutable defaults per the file's existing conventions.)

- [ ] **Step 2: Update `main.py` passthrough**

Add the parameter and pass it through; replace the docstring (the wire schema) with:

```python
@mcp.tool(annotations=DIAGNOSTIC_NETWORK_TOOL)
async def get_wishlist_deals(
    platform: str | None = None,
    max_price: float | None = None,
    min_cut_pct: int | None = None,
    refresh: bool = False,
    preference_override_ratio: float = 0.5,
) -> WishlistDealsResponse:
    """
    Current prices/deals for wishlist games — one entry per game, cheapest-
    recommended first, honoring the set_hardware_preference platform order.

    Prices come from IsThereAnyDeal (Steam wishlist items) and DekuDeals
    (switch2 — the shared wishlist page, plus per-title search lookups for
    games wishlisted elsewhere that IGDB says also have a Switch release;
    search lookups are capped per call, overflow reported in
    switch2_lookups_deferred and picked up on later calls). Cached 12h;
    refresh=True forces a live fetch. Each deal's flat fields are the
    RECOMMENDED purchase (preferred platform unless another platform's price
    is below preference_override_ratio × the preferred price — "the deal is
    too good"); other platforms appear in alternatives, reasoning in
    recommendation_reason. availability_pending counts wishlist games whose
    IGDB platform data hasn't been fetched yet (background enrichment fills
    it). platform filters by where the game is WISHLISTED. Prices are NOT
    currency-converted (Steam follows ITAD_COUNTRY; switch2 follows the
    DekuDeals region); the ratio and max_price compare raw numbers.
    """
    from .tools.deals import get_wishlist_deals as _get_wishlist_deals
    return await _get_wishlist_deals(
        platform, max_price, min_cut_pct, refresh, preference_override_ratio
    )
```

- [ ] **Step 3: Update CLAUDE.md**

- `tools/deals.py` bullet: replace with — `deals.py`: `get_wishlist_deals` (one entry per wishlist game, cheapest-recommended first; honors `hardware_preference` with a `preference_override_ratio` escape hatch — a non-preferred platform wins only when its price drops below ratio × the preferred price; Steam prices via IsThereAnyDeal, switch2 via DekuDeals wishlist page + capped per-title search lookups for cross-platform candidates flagged by `games.igdb_platforms`; 12h TTL, `refresh=True` forces a live fetch)
- `data/dekudeals.py` bullet: append — `fetch_search_prices` prices arbitrary titles via the public search page (`search_url_template`, healable; identical card markup to the wishlist page, so the selectors are shared).
- Database `games` bullet: append `igdb_platforms` (v19): JSON array of IGDB platform ids the game is released on, ownership-independent; Switch 2 is IGDB 508, Switch 130, both mapping to internal switch2.
- "Wishlist tracking" pattern paragraph: append two sentences describing preference-aware recommendation and that the v19 migration re-claims IGDB for wishlisted games so availability backfills automatically.

- [ ] **Step 4: Full verification (sandbox disabled)**

Run: `.venv/bin/python -m pytest -q` — Expected: all pass.
Run: `.venv/bin/ruff check gamelib_mcp tests scripts && .venv/bin/mypy gamelib_mcp` — Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add gamelib_mcp/tools/models.py gamelib_mcp/main.py CLAUDE.md
git commit -m "feat: expose preference-aware wishlist deals on the MCP surface; docs"
```

---

## Post-implementation smoke test (manual, needs the live deployment)

1. `refresh_library` or wait for background enrichment to fill `igdb_platforms` for wishlist games (`availability_pending` in the response shows progress; the v19 migration re-claimed them).
2. Ensure `hardware_preference` is set: `set_hardware_preference(["switch2", "steam"])`.
3. `get_wishlist_deals(refresh=True)` — expect: cross-platform games (e.g. Hades-likes on the Steam wishlist) gain switch2 `alternatives` or switch2 recommendations; `switch2_lookups_deferred` drains over successive calls.

## Known limitations (accepted, documented in docstrings)

- PS5 availability is captured but never priced (no PSN price source) — ps5 stays out of `_PRICEABLE_PLATFORMS`.
- A switch2-wishlisted game available on Steam can only be steam-priced if a Steam appid is resolvable (store_identifier or owned identifier); there is no name→appid lookup.
- IGDB re-claim only covers currently-wishlisted games; games wishlisted later get availability on their first IGDB pass (new games rows) or keep NULL until some future re-fetch (pre-existing enriched rows). `availability_pending` surfaces the gap.
- Original-Switch (130) availability counts as switch2 by backward-compatibility assumption.
