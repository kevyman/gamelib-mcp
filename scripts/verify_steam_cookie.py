"""Throwaway verifier for the Steam store session cookie.

Reads the cookie from the SAME place the importer does (STEAM_STORE_COOKIES_FILE
or data/steam_store_cookies.json) and exercises the real fetch path, printing
ONLY sanitized diagnostics:

- cookie NAMES present (never values); for the two that matter, a length + a
  short sha256 fingerprint so you can confirm identity across runs without
  revealing the secret.
- counts of licenses / purchase records / skipped rows.
- per-currency totals and a few sample rows (your own purchase data).

The cookie value is never printed. Run:  .venv/bin/python scripts/verify_steam_cookie.py
"""

import asyncio
import hashlib

from gamelib_mcp.data.purchases.steam_history import (
    _load_steam_cookies,
    fetch_steam_purchases,
)


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


async def main() -> None:
    cookies = _load_steam_cookies()
    if not cookies:
        print("NO COOKIES FOUND — set STEAM_STORE_COOKIES_FILE or place "
              "data/steam_store_cookies.json first.")
        return

    print("cookie names present:", sorted(cookies))
    for name in ("steamLoginSecure", "sessionid"):
        v = cookies.get(name)
        if v:
            print(f"  {name}: present (len={len(v)}, fp={_fp(v)})")
        else:
            print(f"  {name}: MISSING")

    try:
        records, skipped = await fetch_steam_purchases()
    except Exception as exc:  # noqa: BLE001 - surface any auth/parse failure
        print(f"\nFETCH FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        return

    priced = [r for r in records if r.price_paid]
    totals: dict[str, float] = {}
    for r in priced:
        totals[r.price_currency or "?"] = totals.get(r.price_currency or "?", 0.0) + r.price_paid

    print(f"\nOK — records={len(records)}  priced={len(priced)}  skipped={len(skipped)}")
    print("totals by currency:", {k: round(v, 2) for k, v in totals.items()})
    print("\nsample (first 8 records):")
    for r in records[:8]:
        price = f"{r.price_paid} {r.price_currency or ''}".strip() if r.price_paid else "(free/unpriced)"
        print(f"  {r.acquired_at or '????-??-??'}  {price:<14}  {r.title[:45]}")


if __name__ == "__main__":
    asyncio.run(main())
