#!/usr/bin/env python3
"""Build frozen per-bundle fixtures for the bundle-evaluation backtest.

Reads lineups/<id>.json (reconstructed full lineups) and work/owned_dated.tsv
(name<TAB>first-acquired-date for every owned primary game), emits
fixtures/<id>.json containing ONLY what a blind evaluator may see:
lineup, price/structure, prior-ownership flags, and the library as of the
decision date. Never playtime, ratings, or completion of anything.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
LINEUPS = ROOT / "lineups"
FIXTURES = ROOT / "fixtures"
UNDATED_NOTE = (
    "owned_as_of_date covers only the 2,616 of 3,479 currently-owned primary games "
    "with a recorded acquisition date; 863 undated games are excluded, so treat the "
    "list as a lower bound on what was owned."
)

# Per-constituent prior ownership at decision time, established from the DB
# (cross-platform acquisition rows). Keys are matched case-insensitively as
# substrings against lineup titles.
OWNED_BEFORE = {
    "choice-2023-04": {
        "death stranding": "already owned: Epic (free giveaway, Dec 2022)",
        "life is strange 2": "already owned: PS5 (PS Plus, Mar 2023)",
    },
    "choice-2023-08": {
        "disco elysium": "already owned: Epic (bought Oct 2021)",
    },
    "choice-2024-05": {
        "yakuza: like a dragon": "already owned: PS5 (PS Plus, Aug 2022)",
    },
    "named-rpg-legends": {
        "baldur's gate: enhanced": "already owned: GOG (Dec 2019)",
        "planescape": "already owned: GOG (Dec 2019)",
        "neverwinter nights: enhanced edition": "already owned: GOG (Jun 2023) — base game only",
        "wrath of the righteous": "already owned: Steam (Humble Choice Feb 2023)",
    },
    "named-action-roguelikes": {
        "barony": "already owned: Epic (free giveaway, Jul 2020)",
    },
    "named-atari-recharged": {
        "black widow": "already owned: Epic (free giveaway, Mar 2022)",
        "centipede": "already owned: Epic (free giveaway, Mar 2022)",
        "caverns of mars": "already owned: Epic (free giveaway, undated)",
    },
    "monthly-2018-10": {
        "resident evil revelations": "already owned: Steam (Humble Capcom Bundle, Oct 2015)",
    },
    "monthly-2017-12": {
        "tomb raider": "already owned: Steam (bought Dec 2014)",
    },
}

# id -> (decision_date, price_display, extra_notes)
BUNDLES = {
    "monthly-2016-09":  ("2016-09-30", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2017-09":  ("2017-09-29", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2017-12":  ("2017-12-29", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2018-01":  ("2018-01-26", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2018-08":  ("2018-08-31", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2018-10":  ("2018-10-26", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2018-11":  ("2018-11-30", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "monthly-2019-01":  ("2019-01-25", "$12 (Humble Monthly subscription month)", "Mystery box: only the early unlock(s) were known before paying; full lineup revealed on drop day."),
    "choice-2020-02":   ("2020-02-07", "$12 subscription month (grandfathered Classic plan; full 12-game menu included)", None),
    "choice-2020-04":   ("2020-04-03", "$12 subscription month (grandfathered Classic plan; full 12-game menu included)", None),
    "choice-2023-04":   ("2023-04-04", "$10.75 subscription month (annual-plan share; list $11.99)", None),
    "choice-2023-08":   ("2023-08-01", "$10.21 subscription month (annual-plan share; list $11.99)", None),
    "choice-2024-05":   ("2024-05-07", "$10.75 subscription month (annual-plan share; list $11.99)", None),
    "choice-2024-12":   ("2024-12-03", "€8.58 subscription month (annual-plan share; list €9.99)", None),
    "named-rpg-legends":            ("2023-07-29", "$11.14 paid", None),
    "named-action-roguelikes":      ("2023-11-16", "$13.00 paid", None),
    "named-luck-of-the-draw-encore":("2023-12-10", "$18.00 paid (tier 2 of 2; tier 1 was $12 for 6 games)", None),
    "named-muv-luv":                ("2024-07-04", "€23.85 paid", None),
    "named-atari-recharged":        ("2024-10-20", "€18.32 paid", None),
    "skipped-2020-10":  ("2020-10-29", None, "John SKIPPED this month (fact hidden from evaluator; evaluate as an open decision)."),
    "skipped-2021-01":  ("2021-01-24", None, "John SKIPPED this month (fact hidden from evaluator; evaluate as an open decision)."),
    "skipped-2021-09":  ("2021-09-10", None, "John SKIPPED this month (fact hidden from evaluator; evaluate as an open decision)."),
    "skipped-2022-05":  ("2022-05-17", None, "John SKIPPED this month (fact hidden from evaluator; evaluate as an open decision)."),
    "skipped-2022-08":  ("2022-08-28", None, "John SKIPPED this month (fact hidden from evaluator; evaluate as an open decision)."),
}


def owned_before_for(bundle_id: str, title: str):
    flags = OWNED_BEFORE.get(bundle_id, {})
    tl = title.lower()
    for key, note in flags.items():
        if key in tl:
            return note
    return None


def main():
    owned = []
    with open(ROOT / "work" / "owned_dated.tsv", encoding="utf-8") as fh:
        next(fh)
        for line in fh:
            name, _, d = line.rstrip("\n").partition("\t")
            if name and d:
                owned.append((name, d[:10]))
    assert len(owned) > 2500, f"owned_dated.tsv looks incomplete: {len(owned)} rows"

    FIXTURES.mkdir(exist_ok=True)
    for bid, (decision_date, price, note) in BUNDLES.items():
        lineup = json.loads((LINEUPS / f"{bid}.json").read_text(encoding="utf-8"))
        constituents = []
        for title in lineup["full_lineup"]:
            if "sneak peek" in title.lower():
                continue  # demo teaser, not a constituent
            entry = {"title": title}
            ob = owned_before_for(bid, title)
            if ob:
                entry["owned_before"] = ob
            if lineup.get("early_unlocks") and title in lineup["early_unlocks"]:
                entry["early_unlock"] = True
            constituents.append(entry)

        as_of = sorted(n for n, d in owned if d < decision_date)
        # A skipped month has no DB purchase record; hide the skip itself.
        price_display = price or lineup.get("price", {}).get("note") or (
            f"{lineup['price']['amount']} {lineup['price']['currency']}" if lineup.get("price") else "unknown"
        )

        fixture = {
            "id": bid,
            "bundle": lineup.get("official_name", bid),
            "decision_date": decision_date,
            "price": price_display,
            "structure": lineup.get("structure"),
            "tiers": lineup.get("tiers"),
            "key_platform": lineup.get("key_platform", "steam"),
            "constituents": constituents,
            "owned_count_as_of_date": len(as_of),
            "owned_as_of_date": as_of,
            "undated_note": UNDATED_NOTE,
            "notes": note,
        }
        (FIXTURES / f"{bid}.json").write_text(
            json.dumps(fixture, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"{bid}: {len(constituents)} constituents, {len(as_of)} owned as of {decision_date}")


if __name__ == "__main__":
    main()
