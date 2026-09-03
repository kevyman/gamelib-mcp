# Session material: cookie ingest and the Nintendo SSO handshake

Why this exists: every cookie and token this server holds arrives through one
path — the single-use `/ingest/{nonce}` paste form — and the Nintendo eShop
importer additionally replays a browser OAuth handshake so a 1-hour eShop token
never has to be stored. The root `CLAUDE.md` keeps one line per provider and the
"only via `create_session_ingest_link`" rule; the per-provider detail and the
handshake moved here on 2026-09-01.

## Cookie/session files (env vars for `import_purchases` and ownership syncs)

Optional cookie-file paths for `import_purchases` sessions: `NINTENDO_COOKIES_FILE` (the shared accounts.nintendo.com login session — drives both Switch ownership and eShop purchases; `provider="nintendo"`), `EPIC_COOKIES_FILE` (www.epicgames.com website session for order history/prices — separate from the Legendary launcher session that syncs ownership; `provider="epic"`), `HUMBLE_COOKIES_FILE` (`provider="humble"`), `STEAM_REFRESH_TOKEN_FILE` (the long-lived `steamRefresh_steam` login token from login.steampowered.com — `provider="steam_refresh"`, **preferred**; `data/steam_session.py` mints fresh `steamLoginSecure` store cookies from it on demand, ~200-day validity so no re-pasting), `STEAM_STORE_COOKIES_FILE` (`provider="steam_store"` — the legacy short-lived `steamLoginSecure` fallback, used only when no refresh token is stored) — all populated **only** via `create_session_ingest_link`, which serves a single-use browser paste form so cookies never enter the chat (the old `set_*_session` chat tools were removed; the underlying `tools/session_admin.py::set_*_session` functions remain as the form's internal save path); defaults live under `data/`. `NINTENDO_PCTL_SESSION_FILE` (Switch playtime) goes through the same link as `provider="nintendo_pctl"` — not a cookie paste but an interactive Nintendo sign-in the form walks the user through, for the same reason: the `npf://` link pasted back carries a one-time code redeemable for a long-lived token.

## eShop session via accounts SSO (shared with VGCS)

**eShop session via accounts SSO (shared with VGCS)**: the eShop's `__Secure-next-auth.session-token` is a NextAuth JWE that hard-expires in 1h, so storing it directly would mean re-pasting hourly. Instead the eShop importer reuses the **one** `accounts.nintendo.com` login session VGCS ownership already stores (`create_session_ingest_link(provider="nintendo")` → `NINTENDO_COOKIES_FILE`; the `NASID`/`NATID`/`NAID`-family cookies, good for weeks-to-months) — `data/purchases/nintendo_ec.py::_load_account_cookies` reads that same file, and `_establish_ec_session` replays the browser's silent OAuth handshake (csrf → signin → authorize → callback) to mint a fresh eShop session on every import. One session powers both ownership and purchases; no separate export, no keep-warm loop, no dependence on process uptime. A redirect to `/login` during the handshake means the account session finally expired → re-run `create_session_ingest_link(provider="nintendo")`. The longer-lived mobile `session_token` flow (`nintendo_pctl.py`) is *not* available for this web OAuth client.
