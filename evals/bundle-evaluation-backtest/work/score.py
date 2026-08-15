#!/usr/bin/env python3
"""Score blind-eval results against ground truth.

Ground-truth classes (per constituent, farmed playtime discounted):
  wanted      : non-farmed playtime >= 120 min, OR rated >= 7, OR completion in
                (completed, playing, evergreen)
  never       : zero (or farmed-only) playtime and no rating
  ambiguous   : everything else (launched < 2h unrated, rated < 7 without other
                signals) -> excluded from the matrix, listed separately
  excluded    : rows with an "exclude" key (owned_before / engagement_unknown)

Second, stricter tier reported alongside (mass-idle-session robustness):
  strong_wanted: playtime >= 480 min, OR completion/rating criteria, OR
                 relaunched >= 365 days after the bundle decision date.

Explicit negatives: rows with not_picked=true (declined in a pick-N month) and
never-redeemed lineup items; a must-have prediction there is a called-out miss.

Prediction tiers come from results/<id>.json per_game: must-have / nice /
filler / owned. "owned" predictions are checked against fixture owned_before
flags (sanity, not scored).
"""
import json
import pathlib
from collections import Counter, defaultdict
from datetime import date

ROOT = pathlib.Path(__file__).resolve().parent.parent


def norm(t: str) -> str:
    t = t.lower().strip()
    for junk in [" - the final cut", " (humble original)", " (extra)", " (2013)",
                 "the complete first season", "definitive edition", "enhanced edition", ":", "'", "’",
                 "™", "®", ".", ",", "-", "  "]:
        t = t.replace(junk, " ")
    return " ".join(t.split())


def match(title, pool):
    """Match an evaluator title to an unclaimed ground-truth title.
    Exact (normalized) match wins; otherwise the LONGEST substring match,
    so 'Muv-Luv Alternative' never falls through to plain 'Muv-Luv' and
    'Sniper Elite' never claims 'Sniper Elite V2'. Caller removes the
    returned title from the pool (consume-once)."""
    n = norm(title)
    for gt in pool:
        if norm(gt) == n:
            return gt
    cands = [gt for gt in pool if norm(gt) in n or n in norm(gt)]
    return max(cands, key=lambda g: len(norm(g))) if cands else None


def days_between(a, b):
    ya, ma, da = map(int, a[:10].split("-"))
    yb, mb, db = map(int, b[:10].split("-"))
    return (date(yb, mb, db) - date(ya, ma, da)).days


def classify(row, decision_date):
    if "exclude" in row:
        return "excluded"
    # Engagement whose last_played predates the decision date cannot have been
    # caused by this bundle (family share / another copy) - unattributable, so
    # excluded rather than counted as post-decision engagement (HITMAN in
    # monthly-2018-10, Cities: Skylines in monthly-2018-11).
    if row.get("pt", 0) > 0 and row.get("last") and decision_date and row["last"] < decision_date:
        return "excluded"
    pt = row.get("pt", 0)
    farmed = row.get("farmed", 0)
    score = row.get("score")
    comp = row.get("completion")
    eff_pt = 0 if farmed else pt
    wanted = eff_pt >= 120 or (score is not None and score >= 7) or comp in (
        "completed", "playing", "evergreen")
    never = eff_pt == 0 and score is None
    strong = (
        eff_pt >= 480
        or (score is not None and score >= 7)
        or comp in ("completed", "playing", "evergreen")
        or (eff_pt >= 120 and row.get("last") and decision_date
            and days_between(decision_date, row["last"]) >= 365)
    )
    if wanted:
        return "strong_wanted" if strong else "wanted"
    if never:
        return "never"
    return "ambiguous"


def main():
    gt = json.loads((ROOT / "work" / "ground_truth.json").read_text())["bundles"]
    matrix = Counter()          # (pred_tier, actual) -> n
    strict_matrix = Counter()   # same but wanted collapsed to strong only
    interesting = defaultdict(list)  # failure-mode buckets
    unmatched = []
    bundle_rows = []

    for res_file in sorted((ROOT / "results").glob("*.json")):
        res = json.loads(res_file.read_text())
        bid = res["id"]
        fixture = json.loads((ROOT / "fixtures" / f"{bid}.json").read_text())
        decision_date = fixture.get("decision_date")
        gt_rows = {r["t"]: r for r in gt.get(bid, [])}
        available = set(gt_rows)
        per_game = res.get("per_game", [])
        seen = set()

        for pg in per_game:
            tier = pg["tier"].lower().replace(" ", "-")
            if "loot box" in pg["title"].lower():
                continue
            gt_title = match(pg["title"], available)
            if gt_title is None:
                if not bid.startswith("skipped"):
                    unmatched.append((bid, pg["title"]))
                continue
            available.discard(gt_title)
            seen.add(gt_title)
            row = gt_rows[gt_title]
            actual = classify(row, decision_date)
            if tier == "owned":
                continue  # ownership handed to evaluator; not a prediction
            if actual == "excluded":
                continue
            if row.get("not_picked") or "never redeemed" in row.get("note", ""):
                interesting["explicit_negative"].append(
                    (bid, gt_title, tier, "declined/unredeemed in reality"))
            a3 = {"strong_wanted": "wanted", "wanted": "wanted"}.get(actual, actual)
            matrix[(tier, a3)] += 1
            s3 = "wanted" if actual == "strong_wanted" else (
                "weak" if actual == "wanted" else a3)
            strict_matrix[(tier, s3)] += 1
            if tier == "must-have" and a3 == "never":
                interesting["oversell"].append((bid, gt_title))
            if tier == "filler" and a3 == "wanted":
                interesting["dismissed_a_hit"].append(
                    (bid, gt_title, actual))

        if not bid.startswith("skipped"):
            for t, row in gt_rows.items():
                if t not in seen:
                    unmatched.append((bid, f"[missing from eval output] {t}"))

        bundle_rows.append({
            "id": bid,
            "verdict": res.get("verdict"),
            "actually": "skipped" if bid.startswith("skipped") else "bought",
            "confidence": res.get("confidence"),
        })

    out = {
        "matrix": {f"{k[0]}|{k[1]}": v for k, v in sorted(matrix.items())},
        "strict_matrix": {f"{k[0]}|{k[1]}": v for k, v in sorted(strict_matrix.items())},
        "failure_buckets": {k: v for k, v in interesting.items()},
        "bundle_level": bundle_rows,
        "unmatched_titles": unmatched,
    }
    (ROOT / "work" / "scoring.json").write_text(json.dumps(out, indent=1))

    print("=== Confusion matrix (pred tier x actual) ===")
    for tier in ["must-have", "nice", "filler"]:
        row = {a: matrix.get((tier, a), 0) for a in ["wanted", "never", "ambiguous"]}
        print(f"{tier:>10}: wanted={row['wanted']:3d} never={row['never']:3d} ambig={row['ambiguous']:3d}")
    print("\n=== Strict (wanted=strong only; weak = idle-suspect 2-8h) ===")
    for tier in ["must-have", "nice", "filler"]:
        row = {a: strict_matrix.get((tier, a), 0) for a in ["wanted", "weak", "never", "ambiguous"]}
        print(f"{tier:>10}: strong={row['wanted']:3d} weak={row['weak']:3d} never={row['never']:3d} ambig={row['ambiguous']:3d}")
    print("\n=== Bundle level ===")
    for b in bundle_rows:
        print(f"{b['id']:>32}: predicted {b['verdict']:<15} actually {b['actually']}")
    print(f"\nOversell (must-have -> never): {len(out['failure_buckets'].get('oversell', []))}")
    for x in out["failure_buckets"].get("oversell", []):
        print("   ", x)
    print(f"Dismissed a hit (filler -> wanted): {len(out['failure_buckets'].get('dismissed_a_hit', []))}")
    for x in out["failure_buckets"].get("dismissed_a_hit", []):
        print("   ", x)
    print(f"Explicit negatives, how tiered: ")
    for x in out["failure_buckets"].get("explicit_negative", []):
        print("   ", x)
    if unmatched:
        print(f"\nUNMATCHED ({len(unmatched)}):")
        for x in unmatched:
            print("   ", x)


if __name__ == "__main__":
    main()
