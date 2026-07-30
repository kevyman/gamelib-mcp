# ADR 0007: ownership can end, and "not seen" is not "not owned"

Status: accepted (2026-07-30)

## Context

A spending audit on the production library started from one suspicious row and
ended up surfacing a schema gap.

**Red Dead Redemption 2, `game_id` 1660, Epic row 182772**: `owned=1`,
`price_paid=30.19 EUR`, `acquired_at=2020-10-23`, `purchase_source=epic`. The
Epic account's own transaction page shows *Purchased* and *Refunded* on that
same date, same amount. The copy never existed. Consequences, all of them
wrong: €30.19 of phantom spend, a #1 entry in the "purchased more than once"
ranking, RDR2 counted as a 3-platform game when it is 2, and every
cost-per-hour figure touching the row.

No tool could fix it:

- `add_game_to_platform(owned=False)` writes a **wishlist entry** and returns
  `wishlist_id`. A refunded game is not a wishlist item, and the ownership row
  is untouched.
- `delete_game` operates on the whole game and cascades to every platform.
  Deleting RDR2 to fix Epic would have destroyed the Steam and PS5 rows,
  including 387 and 164 minutes of playtime.
- `set_acquisition(clear=[...])` clears the money columns — which is what was
  actually done in the session — and leaves `owned=1`, so the duplication and
  platform counts stay wrong.

The second half of the problem showed up while looking for more of these. Rows
the source stops returning are simply left alone, keeping `owned=1` and an old
`last_synced`. Post-sync freshness on 2026-07-30:

- **Steam**: 2,010 rows fresh, 622 stale. Benign and explainable — 406 carry
  `delisted=1` (retired from the store, so `GetOwnedGames` correctly omits
  them), all 622 have zero playtime, and the rest are 2011–2013 Humble keys and
  DLC.
- **Epic**: 656 fresh, 33 stale. 25 of the 26 that went stale on one date are
  €0 giveaway claims (Batman: Arkham Origins, KOTOR I & II, Citizen Sleeper,
  Machinarium, …). Epic giveaways are permanent entitlements that cannot be
  refunded or removed. Twenty-six titles going stale together is a dropped page
  in the Legendary metadata fetch, not twenty-six ownership events.

Both cohorts produce **identical rows**. RDR2 was the only paid title in the
Epic batch, which made a stale timestamp look like refund evidence when it was
not — the refund was real, but it was confirmed from the transaction page, not
from anything in the database.

## Decision

### 1. `game_platforms.unowned_at` — ownership that ended, kept as history

A nullable timestamp (v34). Setting it flips the row to `owned=0` and keeps
everything else: acquisition columns, identifiers, playtime, enrichment. Every
aggregate in the codebase already filters `owned = 1`, so a retired row drops
out of spending, duplication, and platform counts the moment it is stamped,
with no reader changes at all. (`spend.duplicate_purchase` was the one check
reading acquisition rows without that filter; it now has it, because "bought it
twice, refunded one" is precisely the shape it must not report.)

One column covers three states that were previously indistinguishable from
permanent ownership: **refunds**, **revoked keys**, and **lapsed subscription
titles** (Game Pass / PS+ / Humble Choice).

### 2. The write path is a parameter on `add_game_to_platform`, not a new tool

ADR 0004 set the bar: 30 tools, one per operation, and a new registration needs
to earn its place. `add_game_to_platform` already owns the "correct an existing
platform row" job via `delisted`, and its docstring already says so. So:
`unowned_at="YYYY-MM-DD"` retires the row, `unowned_at="none"` restores it (the
same sentinel `update_game(completion_status="none")` uses).

Two guards make it safe to hand a model:

- It **never mints**. Name-targeting this tool normally creates a game on a
  miss; here a typo would record a purchase that never happened, which is the
  one place mint-on-miss would corrupt the very numbers this is fixing. No
  matching game, or no row on that platform → `ToolError`.
- `owned=False` is still the wishlist path and is rejected alongside
  `unowned_at`. They read similarly and mean opposite things.

### 3. A retirement PINS `owned`; a source is not always positive evidence

The tempting rule is "if the source lists it again, you own it again" — a
source omitting a row is weak evidence, but a source *returning* one looks
strong. It isn't universally: Xbox ownership is derived from **title history**,
which never forgets a game you once launched, so a lapsed Game Pass title would
be re-owned on every single sync and the correction would silently evaporate.

So a retirement records `owned` in `game_platforms.manual_overrides`, exactly
like `delisted`, and the sync write paths (`upsert_game_platform`,
`bulk_upsert_steam_library`) skip a protected column. Released by
`add_game_to_platform(unowned_at="none")` (restores ownership immediately) or
`set_playtime(clear=["owned"])` (hands the column back to sync without
changing it).

A sync that legitimately re-owns an *unpinned* row clears `unowned_at` in the
same statement, so the two can never disagree.

### 4. `game_platforms.last_seen_in_source` — what the source said, separately

`last_synced` moves whenever anything writes the row. `last_seen_in_source`
(v34) moves only when the platform's own source **returned** the row: every
platform sync passes `from_source=True` to `upsert_game_platform`, the Steam
bulk path stamps it with the run's `synced_at`, and no manual tool sets it at
all. NULL means "never seen in a source" — hand-added, or predating the column.
It is deliberately not backfilled: stamping existing rows would assert evidence
no sync ever produced.

Nothing acts on it automatically. `check_library`'s new
`ownership.unseen_in_source` reports rows absent from the last **three
consecutive SUCCESSFUL** syncs of their platform, and suggests
`add_game_to_platform(unowned_at=…)` for a human to confirm or dismiss. Three,
because 26 Epic rows vanishing from one run is a routine dropped page.
Successful, because a failed run must never make a row look abandoned — which
is what made the pre-v34 signal worthless. Rows with NULL stamps and
`delisted=1` rows are excluded; both are expected to go unseen.

"Successful" needs a definition the checker can trust, so each platform's
recent successful-sync timestamps are kept as a rolling 10-entry list in `meta`
(`sync_success_history_<platform>`), written where a platform's outcome is
recorded.

## Consequences

### Positive

- The three ownership-ended states are representable, and the numbers derived
  from ownership stop counting copies that no longer exist — without deleting a
  game and taking three platforms' playtime with it.
- A stale row now carries diagnostic value. "Last seen in the source three
  successful syncs ago" is actionable; "last_synced is old" never was.
- No reader changes: `owned = 1` was already the filter everywhere, which is
  the reason this fits in one column instead of a migration across every query.

### Negative / revisit triggers

- **A pinned `owned` outranks reality until released.** Re-buying a retired
  game needs `unowned_at="none"`; a sync alone will not notice. That is the
  deliberate trade against the Xbox title-history problem, and it is the first
  thing to revisit if it annoys in practice.
- **`add_game_to_platform` gains a 16th parameter**, past the ≤8 guidance ADR
  0004 already documents deviating from. A dedicated `set_ownership` tool is
  the alternative if this one's docstring stops being readable.
- **`last_seen_in_source` is only as good as the syncs that write it.** A
  platform whose sync is broken accumulates no history, so the check reports
  nothing for it — correctly, but silently. `sync.platform_error` is what
  surfaces that, and the two should be read together.
