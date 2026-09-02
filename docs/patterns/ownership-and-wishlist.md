# Ownership lifecycle and wishlist tracking

Why this exists: ADR 0007 decides that ownership can end and that "not seen in
a source" is not "no longer owned"; this file carries the implementation notes
for both, plus the wishlist table's separation rules — which share the same
invariant, that `game_platforms.owned=0` must never be overloaded to mean
"wanted". Moved out of the root `CLAUDE.md` on 2026-09-01.

## Ownership lifecycle (ADR 0007)

**Ownership lifecycle** (ADR 0007): ownership can END — a refund, a revoked key, a lapsed subscription title — and that is not a wishlist entry, not a delete, and not a cleared price. `add_game_to_platform(unowned_at="YYYY-MM-DD")` retires an EXISTING platform row: `owned=0` + `unowned_at`, keeping acquisition history/identifiers/playtime, so it drops out of every aggregate (all of which already filter `owned = 1`) without `delete_game` cascading the game's other platforms away. It never mints a row (a typo must not record a purchase that never happened) and PINS `owned` in `manual_overrides` — a source can keep listing a title you no longer own (Xbox ownership is title *history*), so re-listing is not proof; undo with `unowned_at="none"` (restores + unpins) or `set_playtime(clear=["owned"])` (unpins only). Separately, `last_seen_in_source` records what the source RETURNED (every sync passes `upsert_game_platform(from_source=True)`; no manual tool sets it), so "not seen this run" stops being indistinguishable from "no longer owned" — 26 Epic giveaway rows going stale on one date was a dropped page, not 26 ownership events. Nothing acts on it automatically: `check_library`'s `ownership.unseen_in_source` reports rows missing from the last 3 consecutive *successful* syncs (per-platform success timestamps live in `meta`; a failed run must never make a row look abandoned) and hands the call to a human.

## Wishlist tracking

**Wishlist tracking**: separate table because a wishlist item may not be owned anywhere, and overloading `game_platforms.owned=0` would blur that invariant (and `upsert_game_platform`'s `ON CONFLICT` unconditionally overwrites `owned` — a later sync could silently un-own a row). Fulfillment: `clear_fulfilled_wishlist_entries` deletes a wishlist row once actually owned on that platform (runs after every refresh/sync + inline in add_game_to_platform). Removal: `delete_stale_wishlist_entries(platform, source, keep_game_ids)` is scoped so it never touches manual/other-source rows, and callers invoke it **only** after every fetched item resolved to a game_id this round (failed fetches propagate; for Steam, any single unresolved item skips the whole reconciliation) — otherwise a partial fetch could be mistaken for "wishlist is now empty" and wipe real entries. `sync(targets=["wishlist"])` covers Steam + switch2; PSN has no wishlist API (`add_game_to_platform(owned=False)` only). Push is opt-in and Steam-only: `add_game_to_platform(owned=False, push_to_store=True)` adds the appid to the REAL Steam wishlist via `data/steam_wishlist.py::push_to_steam_wishlist` (official `IWishlistService/AddToWishlist` with the access token embedded in the minted `steamLoginSecure`, storefront-AJAX CSRF fallback); a failed push still records the local row with the error in `store_push`, and the next pull converges the row to `source="steam"`. DekuDeals has no write API (confirmed 2026-08-06, issue #110) — switch2 push returns a manual search link instead. Removal push is deliberately unimplemented. Rows also carry a `source` of `manual` (default) or `assessment` — the latter a promotion out of a game-quality "wishlist for sale" verdict (ADR 0006 decision 5 / issue #110 phase 1), kept distinct from hand-curated entries so it stays bulk-removable by source. A manual write (`add_game_to_platform`'s `wishlist_source`) only ever accepts those two values; the sync-reserved sources (steam/dekudeals) are rejected so a hand-written row can't pose as reconcilable.

## Deal alerts

**Deal alerts** (`gamelib_mcp/deal_alerts.py`, 07-06 roadmap item 3): enabled
only by `DEAL_ALERT_WEBHOOK_URL`, run at the end of
`lifecycle._run_startup_refresh` — which the periodic loop also runs — so there
is no second scheduler. It re-prices through `get_wishlist_deals()`'s own 12 h
TTL rather than around it, and fires on two lines the user already drew:
`below_assessed_target` (a price reaching the `target_price` a recorded verdict
named) and `at_history_low` with `cut_pct > 0` (ITAD's all-time low, on an
actual discount — a never-discounted game sits at its own low forever). No
`alert_price` column and no `set_wishlist_alert` tool: ADR 0004's surface
budget says a feature that rides on data the user already entered does not mint
a tool of its own.

Debounce, not mute: `game_wishlist.last_alert_key` stores the event AND the
price that produced it (`target:19.99` / `low:12.49`), so the same deal never
repeats while a further drop mints a new key and speaks again. The key is
compared per GAME (any of its wishlist rows) and stamped onto all of them, so a
game wishlisted on two platforms cannot alert twice about one event. Nothing is
stamped unless the POST returned 2xx — stamping a failed send would silence the
retry, and the missed price drop is exactly what the feature exists to prevent.

Never-fail rule: `run_deal_alerts` swallows every failure into its return value
(`{configured, checked, triggered, sent, failed}`) and the lifecycle hook wraps
it again. A dead webhook loses an alert, which is recoverable; a webhook that
takes the library sync down with it is not.
