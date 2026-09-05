# Ownership lifecycle and wishlist tracking

Why this exists: ADR 0007 decides that ownership can end and that "not seen in
a source" is not "no longer owned"; this file carries the implementation notes
for both, plus the wishlist table's separation rules — which share the same
invariant, that `game_platforms.owned=0` must never be overloaded to mean
"wanted". Moved out of the root `CLAUDE.md` on 2026-09-01.

## Ownership lifecycle (ADR 0007)

**Ownership lifecycle** (ADR 0007): ownership can END — a refund, a revoked key, a lapsed subscription title — and that is not a wishlist entry, not a delete, and not a cleared price. `add_game_to_platform(unowned_at="YYYY-MM-DD")` retires an EXISTING platform row: `owned=0` + `unowned_at`, keeping acquisition history/identifiers/playtime, so it drops out of every aggregate (all of which already filter `owned = 1`) without `delete_game` cascading the game's other platforms away. It never mints a row (a typo must not record a purchase that never happened) and PINS `owned` in `manual_overrides` — a source can keep listing a title you no longer own (Xbox ownership is title *history*), so re-listing is not proof; undo with `unowned_at="none"` (restores + unpins) or `set_playtime(clear=["owned"])` (unpins only). Separately, `last_seen_in_source` records what the source RETURNED (every sync passes `upsert_game_platform(from_source=True)`; no manual tool sets it), so "not seen this run" stops being indistinguishable from "no longer owned" — 26 Epic giveaway rows going stale on one date was a dropped page, not 26 ownership events. Nothing acts on it automatically: `check_library`'s `ownership.unseen_in_source` reports rows missing from the last 3 consecutive *successful* syncs (per-platform success timestamps live in `meta`; a failed run must never make a row look abandoned) and hands the call to a human.

## Wishlist tracking

### Deal notifications

`DEAL_ALERT_WEBHOOK_URL` enables deal pushes after library refresh. Discord
webhook hosts receive a compact Markdown digest: a price-tag heading, bold
prices, discount badges, clickable game titles, and subtext for platform,
shop, trigger, and sale end date. This deliberately gives deals a different
shape from the illustrated daily briefing cards sharing the channel. Link
previews are suppressed and mentions disabled. Other webhook hosts retain
the plain-text Slack-compatible payload.

Discord messages split between complete deals within 1,900 UTF-16 units,
with the total deal count on each page and page numbers when a digest spans
more than one page. Provider text is truncated, then escaped before Markdown
is added; unusually long or invalid URLs are omitted
instead of sending broken links. Only games in successfully delivered pages
get their debounce stamp, so failed pages can retry on the next refresh. The
renderer measures the assembled page, including the heading, and rejects an
oversized page without sending or stamping it; other pages still proceed.

Discord references: [Markdown formatting](https://support.discord.com/hc/en-us/articles/210298617-Markdown-Text-101-Chat-Formatting-Bold-Italic-Underline),
[webhook payloads](https://docs.discord.com/developers/resources/webhook#execute-webhook),
and [message flags](https://docs.discord.com/developers/resources/message#message-flags).

### Wishlist storage and sync

**Wishlist tracking**: separate table because a wishlist item may not be owned anywhere, and overloading `game_platforms.owned=0` would blur that invariant (and `upsert_game_platform`'s `ON CONFLICT` unconditionally overwrites `owned` — a later sync could silently un-own a row). Fulfillment: `clear_fulfilled_wishlist_entries` deletes a wishlist row once actually owned on that platform (runs after every refresh/sync + inline in add_game_to_platform). Removal: `delete_stale_wishlist_entries(platform, source, keep_game_ids)` is scoped so it never touches manual/other-source rows, and callers invoke it **only** after every fetched item resolved to a game_id this round (failed fetches propagate; for Steam, any single unresolved item skips the whole reconciliation) — otherwise a partial fetch could be mistaken for "wishlist is now empty" and wipe real entries. `sync(targets=["wishlist"])` covers Steam + switch2; PSN has no wishlist API (`add_game_to_platform(owned=False)` only). Push is opt-in and Steam-only: `add_game_to_platform(owned=False, push_to_store=True)` adds the appid to the REAL Steam wishlist via `data/steam_wishlist.py::push_to_steam_wishlist` (official `IWishlistService/AddToWishlist` with the access token embedded in the minted `steamLoginSecure`, storefront-AJAX CSRF fallback); a failed push still records the local row with the error in `store_push`, and the next pull converges the row to `source="steam"`. DekuDeals has no write API (confirmed 2026-08-06, issue #110) — switch2 push returns a manual search link instead. Removal push is deliberately unimplemented. Rows also carry a `source` of `manual` (default) or `assessment` — the latter a promotion out of a game-quality "wishlist for sale" verdict (ADR 0006 decision 5 / issue #110 phase 1), kept distinct from hand-curated entries so it stays bulk-removable by source. A manual write (`add_game_to_platform`'s `wishlist_source`) only ever accepts those two values; the sync-reserved sources (steam/dekudeals) are rejected so a hand-written row can't pose as reconcilable.
