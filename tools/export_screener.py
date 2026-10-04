#!/usr/bin/env python3
"""
export_screener.py: publish the Screener's fundamental scores to the Stock Cockpit.

Run on the laptop after the Screener rebuilds its snapshot (fundamentals change quarterly; weekly is plenty).
Reads the Screener's data/snapshot.parquet READ-ONLY, writes docs/screener.json in this repo, and with --push
commits and pushes it. The Cockpit then re-prices everything price-dependent (upside to fair value, at buy
price, dips, levels) on GitHub, live, without the laptop.

    python tools/export_screener.py                 # write docs/screener.json
    python tools/export_screener.py --push          # ...and commit + push it
    python tools/export_screener.py --screener "D:/path/to/Stock Fundamental Analysis v3"
"""

import argparse
import json
import math
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SCREENER = REPO.parent / "Stock Fundamental Analysis v3"

#: What the Cockpit uses. Kept short on purpose: the repo is public, and the Screener's prose and filings
#: detail stay in the Screener.
NUMBERS = ["price", "score_overall", "score_value", "score_growth", "score_quality", "score_financial_strength",
           "score_cash_quality", "score_momentum", "score_moat", "axes_scored", "z_score", "beta",
           "fair_value_per_share", "buy_below", "epv_per_share", "pe_ttm", "target_mean", "last_eps_surprise",
           "market_cap"]
TEXT = ["name", "sector", "industry", "v_value", "v_quality", "v_growth", "v_balance_sheet", "v_data", "v_moat",
        "v_expectations", "z_zone", "price_verdict", "valuation_basis", "last_earnings_date"]
AXES = ["value", "growth", "quality", "financial_strength", "cash_quality", "momentum", "moat"]


def clean(v):
    if v is None:
        return None
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()[:10]
    if isinstance(v, (bool,)) or (hasattr(v, "dtype") and str(getattr(v, "dtype", "")) == "bool"):
        return bool(v)
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--screener", default=str(DEFAULT_SCREENER), help="path to the Screener project folder")
    ap.add_argument("--push", action="store_true", help="commit and push docs/screener.json")
    args = ap.parse_args()

    root = Path(args.screener) / "data"
    snap = pd.read_parquet(root / "snapshot.parquet")
    meta = {}
    try:
        meta = json.loads((root / "snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    built = meta.get("built_at") or datetime.fromtimestamp((root / "snapshot.parquet").stat().st_mtime, timezone.utc).isoformat()

    rows = {}
    for _, r in snap.iterrows():
        t = str(r.get("ticker") or "").strip().upper()
        if not t or t.startswith("FUND_"):
            continue
        rec = {}
        for c in NUMBERS:
            if c in snap.columns:
                v = clean(r.get(c))
                if v is not None:
                    rec[c] = round(float(v), 4)
        for c in TEXT:
            if c in snap.columns:
                v = clean(r.get(c))
                if v not in (None, ""):
                    rec[c] = str(v)
        if r.get("m_flag") is True:
            rec["m_flag"] = True
        why = clean(r.get("why_value"))
        if why:
            rec["why_value"] = " ".join(str(why).split())[:240]
        rows[t.replace("-", ".")] = rec  # Alpaca writes class shares as BRK.B

    out = {"exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "snapshot_built_at": built,
           "count": len(rows), "axes": AXES, "stocks": rows}
    path = REPO / "docs" / "screener.json"
    path.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {path} ({len(rows)} stocks, snapshot built {built}, {path.stat().st_size / 1024:.0f} KB)")

    if args.push:
        git = ["git", "-C", str(REPO)]
        subprocess.run(git + ["pull", "-q", "--rebase", "origin", "main"], check=False)
        subprocess.run(git + ["add", "docs/screener.json"], check=True)
        if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
            print("no change to push")
            return
        subprocess.run(git + ["commit", "-q", "-m", f"screener: scores from snapshot built {built[:10]}"], check=True)
        subprocess.run(git + ["push", "-q", "origin", "main"], check=True)
        print("pushed")


if __name__ == "__main__":
    main()
