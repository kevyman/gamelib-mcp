# Series Gap Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Delegation guidance (Sonnet 5 executor):** delegate to Haiku the test scaffolding (mirroring `tests/test_igdb.py` mock patterns and `tests/test_tools_series.py` fixtures), `EXPECTED_TOOLS` bookkeeping, and doc edits. Keep for yourself: the IGDB query string (apicalypse syntax is finicky), the gap-set SQL, and the ranking logic.

**Goal:** A `discover_series_gaps` MCP tool answering "which entries am I missing in series I own and love?" — combining the existing `game_series` tables with live IGDB series-member lookups and the user's own ratings/playtime.

**Architecture:** `game_series` (populated during IGDB backfill, kinds `collection`/`franchise`) tells us which series the user owns games in and how much they like them (via `ratings` + playtime). A new `fetch_series_members(kind, igdb_id)` in `data/igdb.py` pulls the *complete* member list for a series from IGDB (`/v4/games where collections = (id)` / `franchises = (id)`), filtered to main-game types. Gaps = members whose `igdb_id` matches no owned game and no wishlist entry. Member lists are cached in the existing `meta` KV table (7-day TTL) — no schema migration needed. The tool ranks the user's series by affinity (avg personal rating, then playtime), fetches members for the top N only, and reports the missing entries.

**Tech Stack:** Python 3.12, aiosqlite, httpx, FastMCP. Reuses IGDB credentials (`TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`) and the module's existing `_IGDB_REQUEST_GATE` rate limiting. **No migration.**

## Global Constraints

- No schema change — cache lives in `meta` under keys `series_members:{kind}:{igdb_id}` storing `{"fetched_at": iso, "members": [...]}`.
- Every IGDB request must go through the existing gate/retry helpers in `data/igdb.py` (`_post_igdb_games` + `_IGDB_REQUEST_GATE`); never raw httpx from the tool layer.
- IGDB main-game filter: keep game types `{0 (main), 4 (standalone expansion), 8 (remake), 9 (remaster)}` — module constant `SERIES_MEMBER_GAME_TYPES`. DLC/bundles/ports are noise for "gap" purposes.
- The tool must be useful without credentials: if `igdb_credentials_configured()` is false, return a structured `"unconfigured"` response (mirroring the sync-status convention), not an exception.
- Test runner `.venv/bin/python -m pytest`; ruff + mypy gate each commit.

---

### Task 1: `fetch_series_members` in `data/igdb.py`

**Files:**
- Modify: `gamelib_mcp/data/igdb.py`
- Test: `tests/test_igdb.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class SeriesMember:
    igdb_id: int
    name: str
    first_release_date: str | None   # ISO YYYY-MM-DD via _unix_to_iso
    game_type: int
    platforms: list[int]             # raw IGDB platform ids

SERIES_MEMBER_GAME_TYPES = frozenset({0, 4, 8, 9})

async def fetch_series_members(kind: str, series_igdb_id: int) -> list[SeriesMember]:
    """All main-game members of an IGDB collection or franchise.

    kind is "collection" or "franchise" (matching game_series.kind). Paginates
    (IGDB caps at 500/page; use limit 500 + offset loop, few series exceed one
    page). Raises IGDBRequestFailure on API failure — callers decide whether
    stale cache is acceptable.
    """
```

- [ ] **Step 1: Failing tests** in `tests/test_igdb.py` (mirror its existing `_post_igdb_games`-mock style):

```python
async def test_fetch_series_members_builds_collection_query_and_parses(self):
    captured = {}
    async def fake_post(url, query):        # match the real helper's signature
        captured["query"] = query
        return [
            {"id": 1, "name": "Pikmin", "first_release_date": 1009843200,
             "game_type": 0, "platforms": [{"id": 130}]},
            {"id": 2, "name": "Pikmin 4 Bundle", "game_type": 3},
        ]
    with patch("gamelib_mcp.data.igdb._post_igdb_games", side_effect=fake_post):
        members = await fetch_series_members("collection", 555)
    self.assertIn("where collections = (555)", captured["query"])
    self.assertEqual([m.igdb_id for m in members], [1])   # bundle filtered out
    self.assertEqual(members[0].first_release_date, "2002-01-01")

async def test_fetch_series_members_rejects_unknown_kind(self):
    with self.assertRaises(ValueError):
        await fetch_series_members("saga", 1)
```

(Read the real `_post_igdb_games` signature at `igdb.py:360` first and adjust the fake accordingly; the test must mock the module's own helper, not httpx, so the request gate is bypassed in tests.)

- [ ] **Step 2: Verify failure** (`ImportError: cannot import name 'fetch_series_members'`).

- [ ] **Step 3: Implement.** Query field list mirrors what backfill already requests plus `game_type`/`platforms`:

```python
_SERIES_FIELD_FOR_KIND = {"collection": "collections", "franchise": "franchises"}

async def fetch_series_members(kind: str, series_igdb_id: int) -> list[SeriesMember]:
    field = _SERIES_FIELD_FOR_KIND.get(kind)
    if field is None:
        raise ValueError(f"kind must be one of {sorted(_SERIES_FIELD_FOR_KIND)}")

    members: list[SeriesMember] = []
    offset = 0
    while True:
        query = (
            "fields id, name, first_release_date, game_type, platforms.id; "
            f"where {field} = ({series_igdb_id}); "
            f"limit 500; offset {offset};"
        )
        rows = await _post_igdb_games(_IGDB_GAMES_URL, query)
        for row in rows:
            game_type = row.get("game_type", 0)
            if game_type not in SERIES_MEMBER_GAME_TYPES:
                continue
            members.append(SeriesMember(
                igdb_id=row["id"],
                name=row.get("name", ""),
                first_release_date=_unix_to_iso(row.get("first_release_date")),
                game_type=game_type,
                platforms=[p["id"] for p in row.get("platforms", []) if isinstance(p, dict)],
            ))
        if len(rows) < 500:
            return members
        offset += 500
```

(Adapt the `_post_igdb_games` call to its actual signature — it may take headers/client args; follow how `backfill_missing_games` calls it. Filter semantics note: IGDB rows sometimes omit `game_type` — default 0 keeps main games.)

- [ ] **Step 4: Run `tests/test_igdb.py` + ruff + mypy** — PASS.
- [ ] **Step 5: Commit** — `feat: fetch_series_members IGDB lookup`.

### Task 2: Meta-cached member lookup

**Files:**
- Create: `gamelib_mcp/data/series_gaps.py`
- Test: `tests/test_series_gaps.py`

**Interfaces:**
- Consumes: `fetch_series_members` (Task 1), `get_meta`/`set_meta` from `data.db`.
- Produces:

```python
SERIES_CACHE_TTL_DAYS = 7

async def get_series_members_cached(
    kind: str, series_igdb_id: int, refresh: bool = False
) -> list[SeriesMember]:
    """fetch_series_members with a meta-KV cache (7-day TTL).

    Cache hit within TTL returns without a network call. On fetch failure with
    a stale cache present, serves the stale copy (logged); with no cache, the
    failure propagates.
    """
```

- [ ] **Step 1: Failing tests**: fresh fetch writes the meta key; second call within TTL does not call the fetcher (patch `fetch_series_members`, assert call count); `refresh=True` bypasses; fetch failure + stale cache → stale data returned; fetch failure + no cache → raises.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** — key `f"series_members:{kind}:{series_igdb_id}"`, value `json.dumps({"fetched_at": iso, "members": [asdict(m) for m in members]})`; rebuild `SeriesMember` on the way out. Guard `json.JSONDecodeError`/missing keys by treating the entry as absent.
- [ ] **Step 4: Tests pass.**
- [ ] **Step 5: Commit** — `feat: cached series-member lookup`.

### Task 3: `discover_series_gaps` tool

**Files:**
- Modify: `gamelib_mcp/tools/series.py` (add the tool beside `get_series_breakdown`)
- Modify: `gamelib_mcp/main.py` (passthrough, `@mcp.tool(annotations=DIAGNOSTIC_NETWORK_TOOL)` — read-only but open-world)
- Modify: `gamelib_mcp/tools/models.py` (`SeriesGapEntry`, `SeriesGapsResponse`)
- Test: `tests/test_tools_series.py`; `tests/test_tool_registration.py` (add tool, bump tool count 32→33)

**Interfaces:**
- Consumes: `get_series_members_cached` (Task 2), `game_series`/`game_series_membership`/`ratings`/`game_wishlist` tables, `PLATFORM_TO_IGDB` from `data.igdb`.
- Produces the MCP tool:

```python
async def discover_series_gaps(
    kind: str | None = None,
    min_owned: int = 2,
    limit: int = 10,
    include_unreleased: bool = False,
    refresh_cache: bool = False,
) -> dict:
    """
    Unowned entries in series you own and rate highly.

    Ranks your series by taste (average personal rating of its games, then
    total playtime), takes the top `limit`, fetches each one's full member
    list from IGDB, and subtracts what you own or already wishlisted.
    kind filters to collection|franchise; min_owned skips series where you
    own fewer games; include_unreleased keeps unreleased/undated entries.
    Requires IGDB credentials (TWITCH_CLIENT_ID/SECRET).
    """
```

Behavior contract:
1. If `not igdb_credentials_configured()`: return `{"results": [], "status": "unconfigured", "error_summary": "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set"}`.
2. Rank candidate series in SQL (one query):

```sql
SELECT s.id AS series_id, s.igdb_id, s.kind, s.name,
       COUNT(DISTINCT m.game_id) AS owned_count,
       AVG(r.normalized_score) AS avg_rating,
       SUM((SELECT COALESCE(SUM(gp.playtime_minutes), 0)
            FROM game_platforms gp WHERE gp.game_id = g.id)) AS total_playtime_minutes
FROM game_series s
JOIN game_series_membership m ON m.series_id = s.id
JOIN games g ON g.id = m.game_id AND g.is_primary_library_item = 1
LEFT JOIN ratings r ON r.game_id = g.id
WHERE s.igdb_id IS NOT NULL
  [AND s.kind = :kind]
GROUP BY s.id
HAVING owned_count >= :min_owned
ORDER BY (avg_rating IS NULL) ASC, avg_rating DESC, total_playtime_minutes DESC
LIMIT :limit
```

3. Owned/wishlisted igdb-id set (one query): `SELECT igdb_id FROM games WHERE igdb_id IS NOT NULL` unioned with wishlisted games' igdb_ids (`JOIN game_wishlist w ON w.game_id = games.id`). Note the whole `games` table's igdb_ids count as "have" — an owned-on-any-platform or even known-but-collapsed entry is not a gap.
4. Per ranked series: `members = await get_series_members_cached(kind, igdb_id, refresh=refresh_cache)`; gaps = members not in the have-set; drop entries with `first_release_date` in the future or NULL unless `include_unreleased`. A per-series fetch failure records `{"series": name, "error": str(exc)}` under `"errors"` and continues — one flaky series must not kill the report.
5. Map each gap's IGDB platform ids to library platform names where `PLATFORM_TO_IGDB` has a reverse mapping (build `{v: k for k, v in PLATFORM_TO_IGDB.items()}`; note ps4/ps5 both map — dedupe, keep library names only) as `available_on`.
6. Response:

```python
{
  "results": [
    {"series_id": ..., "series_name": ..., "kind": ...,
     "owned_count": ..., "avg_rating": ..., "total_playtime_hours": ...,
     "gaps": [{"igdb_id": ..., "name": ..., "release_date": ...,
                "game_type": ..., "available_on": [...]}, ...]},
    ...
  ],
  "series_checked": N, "errors": [...],
}
```

Series whose gap list comes back empty are still included (owning a complete series is signal, and it keeps `series_checked` honest) — but sorted after series with gaps.

- [ ] **Step 1: Failing tool tests** in `tests/test_tools_series.py` (its fixtures already create `game_series`/membership rows; patch `gamelib_mcp.tools.series.get_series_members_cached`): ranking respects `min_owned`; owned igdb_ids and wishlisted igdb_ids excluded from gaps; unreleased filtered by default; per-series fetch error lands in `errors` without failing; unconfigured path.
- [ ] **Step 2: Verify failure.**
- [ ] **Step 3: Implement** per contract; models; `main.py` passthrough; registration-test updates.
- [ ] **Step 4: Full suite + ruff + mypy** — PASS.
- [ ] **Step 5: Commit** — `feat: discover_series_gaps tool`.

### Task 4: Docs

- [ ] `CLAUDE.md`: tool list entry; one Key Design Patterns bullet (meta-cached IGDB member lists, 7-day TTL; igdb_id-only matching — a gap can be a false positive when the owned copy lacks an igdb_id; run IGDB backfill first). Haiku-delegable.
- [ ] Full suite + ruff + mypy; commit — `docs: series gap analysis`.

## Explicit non-goals (YAGNI)

- No "new release in a loved series" push on the periodic refresh loop — natural follow-up once the pull tool proves out; keep it out of this change.
- No auto-wishlisting of gaps — the AI can call `add_game_to_platform(owned=False)` per the user's say-so.
- No fuzzy name matching of members against igdb_id-less library rows (accept the false-positive gap; the fix is IGDB backfill coverage, not fuzzier matching here).
