"""
Stock Cockpit collector: US stocks, trading-side companion to the fundamentals Screener.

Two modes, both on GitHub Actions (nothing depends on a laptop):
  python collector/collect.py --daily   after the close (and pre-market): universe, daily bars, market regime,
                                        breadth, sectors, per-stock table, earnings calendar, short interest
  python collector/collect.py --live    every 15 minutes while the market is open: live index, VIX, breadth and
                                        sector moves, watchlist prices, intraday alerts

Data: Alpaca market data (free plan: live IEX prices, full-market SIP bars 15 minutes delayed, 200 calls a
minute), Nasdaq Trader symbol directory and earnings calendar, FINRA short interest, CBOE VIX.

What was tested before building (2016-2026, S&P 1500 daily data):
  * The regime score separates calm from dangerous markets (next-20-day swings 12% vs 30% a year, average
    worst dip -3.0% vs -6.8%), but stress had the best average next-20-day return: it is a sizing gauge,
    not a sell signal.
  * Daily red-day relative strength did NOT predict outperformance in US stocks (short-term reversal
    dominates), so it is not built on daily data. An intraday version is to be tested on Alpaca minute data.
"""

import io
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import swing  # ported from the Screener: swing amplitude, bounce levels and their track record

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DOCS, DATA = ROOT / "docs", ROOT / "data"
OUT_PATH, STOCKS_PATH, LIVE_PATH = DOCS / "data.json", DOCS / "stocks.json", DOCS / "live.json"
WATCH_PATH = DOCS / "watchlist.json"
SCREENER_PATH, RESCORE_PATH = DOCS / "screener.json", DOCS / "rescore.txt"
STATE_PATH, CACHE_PATH = DATA / "state.json", DATA / "cache.json"
DATA.mkdir(exist_ok=True)  # git doesn't keep empty folders
ET = ZoneInfo("America/New_York")

NOW = datetime.now(timezone.utc)
NOW_TS = int(NOW.timestamp())
SESSION = requests.Session()
SESSION.headers["User-Agent"] = "stock-cockpit/1.0"
BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept": "application/json, text/plain, */*"}
ERRORS = {}
AK, AS = os.getenv("ALPACA_KEY_ID", "").strip(), os.getenv("ALPACA_SECRET_KEY", "").strip()
DATA_API, TRADE_API = "https://data.alpaca.markets/v2", "https://paper-api.alpaca.markets/v2"
INDICES, SECTORS = CONFIG["indices"], CONFIG["sectors"]
ETFS = list(INDICES) + list(SECTORS)
ALERTS = CONFIG.get("alerts", {})


# ------------------------------------------------------------------ helpers

def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def get_json(url, params=None, headers=None, method="GET", body=None, retries=2):
    last = None
    for attempt in range(retries + 1):
        try:
            if method == "POST":
                r = SESSION.post(url, json=body, headers=headers, timeout=40)
            else:
                r = SESSION.get(url, params=params, headers=headers, timeout=40)
            if r.status_code == 429:
                last = "HTTP 429 rate limited"
                time.sleep(15 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json() if r.text.strip() else None
        except Exception as e:  # noqa: BLE001
            last = str(e)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def module(name):
    def wrap(fn):
        def inner(*a, **kw):
            t = time.time()
            try:
                out = fn(*a, **kw)
                log(f"{name}: ok ({time.time() - t:.1f}s)")
                return out
            except Exception as e:  # noqa: BLE001
                ERRORS[name] = str(e)[:300]
                log(f"{name}: FAILED - {e}")
                return None
        return inner
    return wrap


def fnum(x, default=None):
    try:
        v = float(str(x).replace("$", "").replace(",", ""))
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def r2(v, d=2):
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), d)


def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001
        return default


def save(path, obj):
    path.write_text(json.dumps(obj, separators=(",", ":"), default=lambda o: None))


def watchlist():
    w = load(WATCH_PATH, {"tickers": []}).get("tickers", [])
    return list(dict.fromkeys(t.strip().upper() for t in w if isinstance(t, str) and t.strip()))


# ------------------------------------------------------------------ Alpaca

def alpaca(path, params=None, base=DATA_API):
    if not AK or not AS:
        raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET_KEY secrets not set")
    time.sleep(0.32)  # free plan: 200 calls a minute
    return get_json(f"{base}{path}", params=params, headers={"APCA-API-KEY-ID": AK, "APCA-API-SECRET-KEY": AS})


def daily_bars(symbols, days):
    """Split- and dividend-adjusted daily bars from the full market (SIP), which the free plan serves up to
    15 minutes ago. Returns {symbol: [bar, ...]}."""
    start = (NOW - timedelta(days=days)).strftime("%Y-%m-%d")
    end = (NOW - timedelta(minutes=16)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = {}
    for i in range(0, len(symbols), 200):
        params = {"symbols": ",".join(symbols[i:i + 200]), "timeframe": "1Day", "start": start, "end": end,
                  "limit": 10000, "adjustment": "all", "feed": "sip"}
        while True:
            d = alpaca("/stocks/bars", params) or {}
            for s, bars in (d.get("bars") or {}).items():
                out.setdefault(s, []).extend(bars)
            if not d.get("next_page_token"):
                break
            params["page_token"] = d["next_page_token"]
    return out


def frames(bars):
    """{symbol: bars} -> {'o','h','l','c','v': DataFrame indexed by date, one column per symbol}."""
    cols = {f: {} for f in "ohlcv"}
    for s, bs in bars.items():
        for b in bs:
            d = b["t"][:10]
            for f in "ohlcv":
                cols[f].setdefault(s, {})[d] = b[f]
    out = {}
    for f in "ohlcv":
        df = pd.DataFrame(cols[f])
        df.index = pd.to_datetime(df.index)
        out[f] = df.sort_index()
    return out


def prev_closes(symbols, day):
    """Each symbol's last full-market daily close before `day` (a US/Eastern date)."""
    out = {}
    for s, bs in daily_bars(symbols, 12).items():
        before = [b for b in bs if b["t"][:10] < day.isoformat()]
        if before:
            out[s] = before[-1]["c"]
    return out


def intraday_bars(symbols, start, end, timeframe="5Min"):
    """Full-market (SIP) intraday bars, extended hours included; the free plan serves them up to 15 minutes ago."""
    out = {}
    for i in range(0, len(symbols), 200):
        params = {"symbols": ",".join(symbols[i:i + 200]), "timeframe": timeframe, "start": start, "end": end,
                  "limit": 10000, "adjustment": "raw", "feed": "sip"}
        while True:
            d = alpaca("/stocks/bars", params) or {}
            for s, bars in (d.get("bars") or {}).items():
                out.setdefault(s, []).extend(bars)
            if not d.get("next_page_token"):
                break
            params["page_token"] = d["next_page_token"]
    return out


def snapshots(symbols):
    """Live snapshots on the free IEX feed: latest trade, today's IEX bar, previous day's bar."""
    out = {}
    for i in range(0, len(symbols), 200):
        d = alpaca("/stocks/snapshots", {"symbols": ",".join(symbols[i:i + 200]), "feed": "iex"}) or {}
        out.update({k: v for k, v in d.items() if v})
    return out


# ------------------------------------------------------------------ universe

@module("Universe")
def build_universe(cache):
    """Liquid US common stocks: Nasdaq Trader's directory of every listed security, minus ETFs, test issues,
    warrants, units, preferreds and notes, then price >= $5 and 20-day average dollar volume >= $20M."""
    u = CONFIG["universe"]
    old = cache.get("universe")
    if old and NOW_TS - old.get("ts", 0) < u.get("rebuild_days", 7) * 86400 and old.get("symbols"):
        return old["symbols"]
    cands = {}
    for url, sym_col, exch in (("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", "Symbol", None),
                               ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", "ACT Symbol", "Exchange")):
        txt = SESSION.get(url, headers=BROWSER, timeout=40).text
        df = pd.read_csv(io.StringIO(txt), sep="|", dtype=str).fillna("")
        df = df[~df[sym_col].str.startswith("File Creation")]
        for _, r in df.iterrows():
            sym, name = r[sym_col].strip(), r["Security Name"].strip()
            low = f" {name.lower()} "
            if r.get("ETF") == "Y" or r.get("Test Issue") == "Y" or not sym or any(ch in sym for ch in "$^+=/ "):
                continue
            if any(k in low for k in u["exclude_name_keywords"]) and not any(k in low for k in u.get("keep_name_keywords", [])):
                continue
            cands[sym] = {"sym": sym, "name": name.split(" - ")[0][:60],
                          "exch": {"N": "NYSE", "A": "NYSE American", "P": "NYSE Arca", "Z": "Cboe", "V": "IEX"}.get(r.get(exch, ""), "Nasdaq") if exch else "Nasdaq"}
    bars = daily_bars(sorted(cands), 35)
    keep = []
    for sym, bs in bars.items():
        bs = bs[-20:]
        if len(bs) < 15:
            continue
        dv = sum(b["c"] * b["v"] for b in bs) / len(bs)
        if bs[-1]["c"] >= u["min_price"] and dv >= u["min_dollar_volume"]:
            keep.append({**cands[sym], "dv20": round(dv)})
    if len(keep) < 300:
        raise RuntimeError(f"only {len(keep)} liquid stocks found of {len(cands)} candidates")
    keep.sort(key=lambda x: -x["dv20"])
    cache["universe"] = {"ts": NOW_TS, "symbols": keep}
    return keep


#: Nasdaq's sector names -> the Screener's (Yahoo) names, so one sector filter covers both sources.
NASDAQ_SECTORS = {"Finance": "Financial Services", "Consumer Discretionary": "Consumer Cyclical", "Health Care": "Healthcare",
                  "Technology": "Technology", "Industrials": "Industrials", "Real Estate": "Real Estate", "Energy": "Energy",
                  "Utilities": "Utilities", "Consumer Staples": "Consumer Defensive", "Basic Materials": "Basic Materials",
                  "Telecommunications": "Communication Services"}


@module("Nasdaq sectors")
def sector_map(cache):
    """Sector and industry for every US-listed stock from Nasdaq's stock screener (free, no key), refreshed weekly.
    Used for stocks the Screener doesn't cover; the Screener's own sector wins where it has one."""
    old = cache.get("sector_map")
    if old and NOW_TS - old.get("ts", 0) < CONFIG["universe"].get("rebuild_days", 7) * 86400 and old.get("map"):
        return old["map"]
    d = get_json("https://api.nasdaq.com/api/screener/stocks", params={"tableonly": "true", "limit": 25000, "download": "true"},
                 headers=BROWSER) or {}
    out = {}
    for r in (d.get("data") or {}).get("rows") or []:
        sym, sec = (r.get("symbol") or "").strip().upper().replace("/", "."), (r.get("sector") or "").strip()
        if sym and sec in NASDAQ_SECTORS:
            out[sym] = [NASDAQ_SECTORS[sec], (r.get("industry") or "").strip()[:60]]
    if len(out) < 2000:
        raise RuntimeError(f"only {len(out)} sectors returned")
    cache["sector_map"] = {"ts": NOW_TS, "map": out}
    return out


# ------------------------------------------------------------------ market regime

def regime_score(spy, above50, nhnl, vix, vix3m):
    """0-100 from trend, breadth and volatility. Backtested 2016-2026: lower scores mean much bigger swings
    (it is a risk gauge for sizing and stops), not lower returns."""
    parts = {}
    ma50, ma200 = spy.rolling(50).mean().iloc[-1], spy.rolling(200).mean().iloc[-1]
    p = spy.iloc[-1]
    parts["SPY above 200-day average"] = 25 if p > ma200 else 0
    parts["SPY above 50-day average"] = 15 if p > ma50 else 0
    parts["50-day above 200-day"] = 10 if ma50 > ma200 else 0
    parts["Breadth (stocks above 50-day)"] = round(max(0, min(25, (above50 - 30) / 40 * 25)))
    parts["VIX level"] = 15 if vix < 15 else 10 if vix < 20 else 5 if vix < 25 else 0 if vix < 30 else -10
    parts["VIX curve inverted (stress)"] = -10 if vix3m and vix > vix3m else 0
    parts["New highs beat new lows"] = 10 if nhnl > 0 else 0
    return max(0, min(100, sum(parts.values()))), parts


REGIMES = [  # (min score, label, guidance) with the 2016-2026 backtest per bucket
    (75, "Calm uptrend", "Swings are smallest (average worst dip over the next 20 days -3.0%). Best backdrop for breakouts; normal size.",
     {"days_pct": 65, "spy_20d": 0.94, "vol": 12.0, "dip": -2.99}),
    (55, "Constructive", "Mostly healthy with some cracks. Normal size, tighter selection.",
     {"days_pct": 11, "spy_20d": 1.34, "vol": 15.2, "dip": -3.75}),
    (35, "Choppy", "Mixed trend and breadth; breakouts fail more often. Smaller size, take profits sooner.",
     {"days_pct": 9, "spy_20d": 1.02, "vol": 17.5, "dip": -4.58}),
    (15, "Correction", "Downtrend with weak breadth. Protect capital: small size, wider stops, or wait.",
     {"days_pct": 5, "spy_20d": 1.41, "vol": 23.2, "dip": -5.95}),
    (-1, "Stress", "Swings about 2.5x normal (average worst dip -6.8%). Historically the best average rebound (+3.2% next 20 days), so don't panic-sell the index, but size well down.",
     {"days_pct": 10, "spy_20d": 3.20, "vol": 30.1, "dip": -6.77}),
]


def regime_label(score):
    for lo, label, guide, stats in REGIMES:
        if score >= lo:
            return label, guide, stats


@module("CBOE VIX")
def fetch_vix():
    def hist(name):
        t = SESSION.get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv", timeout=40).text
        df = pd.read_csv(io.StringIO(t))
        df["DATE"] = pd.to_datetime(df["DATE"])
        return df.set_index("DATE")["CLOSE"].astype(float)
    vix, vix3m = hist("VIX"), hist("VIX3M")
    return {"vix": vix, "vix3m": vix3m}


def cboe_live(name):
    d = get_json(f"https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{name}.json", retries=1) or {}
    q = d.get("data") or {}
    return {"price": fnum(q.get("current_price")), "chg_pct": fnum(q.get("price_change_percent")), "prev": fnum(q.get("prev_day_close"))}


def series_stats(c):
    """Trend facts for one price series (a column of daily closes)."""
    c = c.dropna()
    if len(c) < 30:
        return None
    p = c.iloc[-1]
    ma = {n: c.rolling(n).mean().iloc[-1] if len(c) >= n else None for n in (20, 50, 200)}
    return {"price": r2(p, 4), "chg1": r2(pct(p, c.iloc[-2])), "chg5": r2(pct(p, c.iloc[-6])) if len(c) > 6 else None,
            "chg20": r2(pct(p, c.iloc[-21])) if len(c) > 21 else None,
            "vs50": r2(pct(p, ma[50])), "vs200": r2(pct(p, ma[200])), "above50": bool(ma[50] and p > ma[50]),
            "above200": bool(ma[200] and p > ma[200]), "from_high": r2(pct(p, c.iloc[-252:].max())),
            "spark": [r2(x, 4) for x in c.iloc[-60:].tolist()]}


@module("Market regime")
def market_regime(f, vixd, universe_syms):
    c, h, l = f["c"], f["h"], f["l"]
    stocks = c[[s for s in universe_syms if s in c.columns]]
    above50 = (stocks > stocks.rolling(50).mean()).where(stocks.notna()).mean(axis=1) * 100
    above200 = (stocks > stocks.rolling(200).mean()).where(stocks.notna()).mean(axis=1) * 100
    hi, lo = stocks >= stocks.rolling(252, min_periods=200).max(), stocks <= stocks.rolling(252, min_periods=200).min()
    nh, nl = hi.sum(axis=1), lo.sum(axis=1)
    nhnl5 = (nh - nl).rolling(5).mean()
    chg = stocks.pct_change(fill_method=None)
    vix = vixd["vix"].reindex(c.index).ffill() if vixd else None
    vix3m = vixd["vix3m"].reindex(c.index).ffill() if vixd else None
    spy = c["SPY"].dropna()
    hist = []
    for i in range(max(210, len(spy) - 90), len(spy) + 1):  # the last ~90 sessions, for the chart
        d = spy.index[i - 1]
        v, v3 = (vix.loc[d], vix3m.loc[d]) if vix is not None else (20, None)
        s, _ = regime_score(spy.iloc[:i], above50.loc[d], nhnl5.loc[d], v, v3)
        hist.append([d.strftime("%Y-%m-%d"), s])
    d = spy.index[-1]
    v, v3 = (float(vix.loc[d]), float(vix3m.loc[d])) if vix is not None else (None, None)
    score, parts = regime_score(spy, above50.loc[d], nhnl5.loc[d], v if v is not None else 20, v3)
    label, guide, stats = regime_label(score)
    prev_label = regime_label(hist[-2][1])[0] if len(hist) > 1 else label
    return {
        "as_of": d.strftime("%Y-%m-%d"), "score": score, "label": label, "guide": guide, "stats": stats, "parts": parts,
        "prev_label": prev_label, "history": hist,
        "indices": {k: {**(series_stats(c[k]) or {}), "name": n} for k, n in INDICES.items() if k in c},
        "vix": {"close": r2(v), "vix3m": r2(v3), "inverted": bool(v and v3 and v > v3),
                "chg1": r2(pct(v, float(vix.iloc[-2]))) if vix is not None else None,
                "spark": [r2(x) for x in vix.iloc[-60:].tolist()] if vix is not None else None},
        "breadth": {"above50": r2(above50.iloc[-1], 1), "above200": r2(above200.iloc[-1], 1),
                    "above50_5d_ago": r2(above50.iloc[-6], 1), "new_highs": int(nh.iloc[-1]), "new_lows": int(nl.iloc[-1]),
                    "adv": int((chg.iloc[-1] > 0).sum()), "dec": int((chg.iloc[-1] < 0).sum()),
                    "series50": [r2(x, 1) for x in above50.iloc[-60:].tolist()], "count": int(stocks.iloc[-1].notna().sum())},
    }


@module("Sectors")
def sector_table(f):
    c = f["c"]
    spy = c["SPY"].dropna()
    out = []
    for etf, name in SECTORS.items():
        s = series_stats(c[etf]) if etf in c else None
        if not s:
            continue
        rs20 = r2((s["chg20"] or 0) - (pct(spy.iloc[-1], spy.iloc[-21]) or 0))
        rs5 = r2((s["chg5"] or 0) - (pct(spy.iloc[-1], spy.iloc[-6]) or 0))
        out.append({"etf": etf, "name": name, **{k: s[k] for k in ("price", "chg1", "chg5", "chg20", "vs50", "above50", "from_high")},
                    "rs5": rs5, "rs20": rs20, "spark": s["spark"][-30:]})
    return sorted(out, key=lambda x: -(x["rs20"] or 0))


#: Screener fields carried onto each stock row (docs/screener.json, from tools/export_screener.py).
SCREENER_FIELDS = ("score_overall", "score_value", "score_growth", "score_quality", "score_financial_strength",
                   "score_cash_quality", "score_momentum", "score_moat", "axes_scored", "v_value", "v_quality", "v_growth",
                   "v_balance_sheet", "v_data", "v_moat", "v_expectations", "z_score", "z_zone", "m_flag",
                   "fair_value_per_share", "buy_below", "epv_per_share", "valuation_basis", "why_value",
                   "target_mean", "analyst_count", "last_earnings_date", "sector", "industry")
#: swing.analyse() outputs kept for the dashboard and the live re-pricing.
SWING_FIELDS = ("swing_amplitude", "swing_chop", "swing_reversals", "bounce_level", "bounce_touches", "dist_to_bounce",
                "bounce_median_10d", "bounce_hit_rate_10d", "bounce_resolved_10d", "bounce_median_21d",
                "bounce_hit_rate_21d", "bounce_resolved_21d", "bounce_last_touch", "resistance_level",
                "resistance_touches", "room_to_resistance", "clear_of_resistance", "swing_note")


def tactical_measures(cs, vs, spy_ret):
    """The Screener's tactical.py measures, on Alpaca's split- and dividend-adjusted daily bars: the week's move
    in units of the stock's own daily volatility (so a 5% fall in a utility and in a chip stock aren't treated
    alike), where it sits in its year, median dollar volume, and beta to SPY over a year."""
    out = {}
    if len(cs) < 70:
        return out
    daily = cs.pct_change().dropna()
    sigma = float(daily.iloc[-60:].std())
    r1w, r1m = float(cs.iloc[-1] / cs.iloc[-6] - 1), float(cs.iloc[-1] / cs.iloc[-22] - 1)
    out.update({"ret_1w": r1w, "ret_1m": r1m, "sigma_daily": sigma, "close_5ago": float(cs.iloc[-6]),
                "dip_sigma_1w": r1w / (sigma * math.sqrt(5)) if sigma else None,
                "dip_sigma_1m": r1m / (sigma * math.sqrt(21)) if sigma else None})
    year = cs.iloc[-252:]
    hi, lo = float(year.max()), float(year.min())
    out["range_position_52w"] = (float(cs.iloc[-1]) - lo) / (hi - lo) if hi > lo else None
    streak = 0
    for ch in reversed(daily.tolist()):
        if ch >= 0:
            break
        streak += 1
    out["down_days"] = streak
    dv = (cs * vs).dropna()
    out["dollar_volume_20d"] = float(dv.iloc[-20:].median()) if len(dv) else None  # median, as the Screener's lenses use
    base = vs.iloc[-60:].mean()
    out["rel_volume"] = float(vs.iloc[-5:].mean() / base) if base else None
    both = pd.concat([daily, spy_ret], axis=1, join="inner").dropna().iloc[-252:]
    if len(both) > 120 and both.iloc[:, 1].var() > 0:
        out["beta"] = float(both.iloc[:, 0].cov(both.iloc[:, 1]) / both.iloc[:, 1].var())
    return out


@module("Stock table")
def stock_table(f, universe, scr, sectors=None):
    """One row per stock: trend, momentum vs SPY, volatility and liquidity, the Screener's tactical and swing
    measures, and its scores re-priced at the latest close."""
    sectors = sectors or {}
    c, h, l, v = f["c"], f["h"], f["l"], f["v"]
    spy = c["SPY"].dropna()
    spy20, spy60 = pct(spy.iloc[-1], spy.iloc[-21]), pct(spy.iloc[-1], spy.iloc[-61])
    spy_ret = spy.pct_change().dropna()
    rows, fails = [], 0
    for u in universe:
        s = u["sym"]
        if s not in c:
            continue
        cs = c[s].dropna()
        if len(cs) < 60:
            continue
        hs, ls, vs = h[s].reindex(cs.index), l[s].reindex(cs.index), v[s].reindex(cs.index)
        st = series_stats(cs)
        tr = pd.concat([hs - ls, (hs - cs.shift()).abs(), (ls - cs.shift()).abs()], axis=1).max(axis=1)
        atr = tr.rolling(14).mean().iloc[-1]
        row = {"sym": s, "name": u["name"], "exch": u["exch"], "price": st["price"], "prev": r2(cs.iloc[-2], 4),
               "chg1": st["chg1"], "chg5": st["chg5"], "chg20": st["chg20"], "vs50": st["vs50"], "vs200": st["vs200"],
               "from_high": st["from_high"], "rs20": r2((st["chg20"] or 0) - (spy20 or 0)),
               "rs60": r2(pct(cs.iloc[-1], cs.iloc[-61]) - (spy60 or 0)) if len(cs) > 61 else None,
               "atr_pct": r2(atr / cs.iloc[-1] * 100), "dv20": round(float((cs * vs).iloc[-20:].mean())),
               "rvol": r2(vs.iloc[-1] / vs.iloc[-21:-1].mean()) if vs.iloc[-21:-1].mean() else None,
               "spark": [float(f"{x:.4g}") for x in cs.iloc[-60::2].tolist()]}  # 30 points, 4 significant figures: keeps the file phone-sized
        try:
            row.update({k: (r2(x, 4) if isinstance(x, float) else x) for k, x in tactical_measures(cs, vs, spy_ret).items()})
            frame = pd.DataFrame({"High": hs, "Low": ls, "Close": cs})
            sw = swing.analyse(frame, cs)  # bars are already adjusted, so the Screener's raw-to-adjusted scale is 1
            row.update({k: (r2(sw[k], 4) if isinstance(sw[k], float) else sw[k]) for k in SWING_FIELDS if k in sw})
        except Exception:  # noqa: BLE001  one odd series must not cost the table
            fails += 1
        rec = scr.get(s)
        if rec:
            row.update({k: rec[k] for k in SCREENER_FIELDS if k in rec})
            row["name"] = rec.get("name") or row["name"]
            p = row["price"]
            if rec.get("fair_value_per_share") and p:
                row["upside_to_fair"] = r2(rec["fair_value_per_share"] / p - 1, 4)
            if rec.get("target_mean") and p:
                row["upside_to_target"] = r2(rec["target_mean"] / p - 1, 4)  # analysts' mean price target vs now
                row["snap_price"] = rec.get("price")  # the price when the Screener read the target (staleness check)
        if not row.get("sector"):
            row["sector"], ind = sectors.get(s) or ["Other", ""]
            row["industry"] = row.get("industry") or ind or None
        rows.append(row)
    if fails:
        log(f"stock table: {fails} series skipped by the swing/tactical measures")
    return rows


def setups(rows):
    """The Screener's event lists, with its own gates (lenses.py): blanks pass a numeric gate unless the field is
    required, as there. Returns {list name: rows}."""
    st = CONFIG["setups"]
    g = lambda r, k: r.get(k)  # noqa: E731
    amp, chop, mdv = st["swing_min_amplitude"], st["swing_min_chop"], st["min_dollar_volume"]
    swing_ok = lambda r: g(r, "swing_amplitude") is not None and r["swing_amplitude"] >= amp \
        and (g(r, "swing_chop") is None or r["swing_chop"] >= chop) and (g(r, "dollar_volume_20d") or 0) >= mdv  # noqa: E731
    movers = [r for r in rows if swing_ok(r) and (g(r, "swing_reversals") is None or r["swing_reversals"] >= st["swing_min_reversals"])]
    at_level = [r for r in rows if swing_ok(r) and (g(r, "bounce_touches") or 0) >= 3 and (g(r, "bounce_resolved_10d") or 0) >= 3
                and g(r, "dist_to_bounce") is not None and abs(r["dist_to_bounce"]) <= st["at_level_pct"] / 100]
    dips = [r for r in rows if r.get("v_data") in ("clean", "caution", "check first") and g(r, "dip_sigma_1w") is not None
            and r["dip_sigma_1w"] <= st["dip_sigma"] and (g(r, "dollar_volume_20d") or 0) >= mdv
            and (g(r, "z_score") is None or r["z_score"] >= st["z_distress"])]
    # the price yardstick is the analysts' mean target (owner's choice: easier to read than the Screener's DCF buy
    # price, which throws up model artefacts for lenders and spin-offs); at least N analysts so one stray target
    # can't qualify a stock. The DCF fair value stays on the row as a second opinion.
    # A target read when the price was 40%+ away from today's is stale (big news since, a spin-off like CTVA, or a
    # split the target never caught up with), so it doesn't qualify a stock. Upside over 100% is listed but tagged.
    stale = lambda r: bool(g(r, "snap_price") and abs(r["price"] / r["snap_price"] - 1) > st["value_target_stale_pct"] / 100)  # noqa: E731
    value = [r for r in rows if (g(r, "score_overall") or 0) >= st["value_min_score"] and g(r, "upside_to_target") is not None
             and r["upside_to_target"] >= st["value_min_target_upside_pct"] / 100 and not stale(r)
             and (g(r, "analyst_count") or 0) >= st["value_min_analysts"] and r.get("v_data") in ("clean", "caution", "check first")]
    for r in rows:
        if r.get("upside_to_fair") is not None:
            r["model_check"] = r["upside_to_fair"] > 2  # DCF fair value over 3x the price: check it in the Screener
        if r.get("upside_to_target") is not None:
            r["target_check"] = r["upside_to_target"] > 1 or stale(r)  # target over 2x the price, or read at a very different price
    return {
        "Swing · at a level": sorted(at_level, key=lambda r: -r["swing_amplitude"]),  # by amplitude, as the Screener: not by the small-sample hit rate
        "Swing · movers": sorted(movers, key=lambda r: -r["swing_amplitude"]),
        "Quality dip": sorted(dips, key=lambda r: (-(r.get("score_overall") or -1), r["dip_sigma_1w"])),  # best business first
        VALUE_LIST: sorted(value, key=lambda r: -(r.get("score_overall") or 0)),  # best business first
    }


VALUE_LIST = "Good score below analyst target"


SETUP_KEEP = ("sym", "name", "price", "chg1", "swing_amplitude", "swing_chop", "swing_reversals", "bounce_level",
              "bounce_touches", "dist_to_bounce", "bounce_median_10d", "bounce_hit_rate_10d", "bounce_resolved_10d",
              "bounce_median_21d", "bounce_hit_rate_21d", "resistance_level", "room_to_resistance", "dip_sigma_1w",
              "ret_1w", "sigma_daily", "close_5ago", "dollar_volume_20d", "beta", "atr_pct", "score_overall", "v_value",
              "v_quality", "v_data", "z_score", "fair_value_per_share", "upside_to_fair", "target_mean", "analyst_count",
              "upside_to_target", "snap_price", "target_check", "earn", "model_check", "sector", "industry", "vs50", "vs200", "spark")


def arrivals(state, lists, today):
    """notify.py's rule: only new names alert; a name off a list for `arrival_cooldown_days` can alert again; the
    first sight of a list seeds it silently instead of firing everything at once."""
    keep = timedelta(days=CONFIG["setups"]["arrival_cooldown_days"])
    st, out = state.setdefault("arrivals", {}), {}
    for name, rows in lists.items():
        if name == "Swing · movers":  # a lasting property of a name, not an event
            continue
        first = name not in st
        known = {k: v for k, v in (st.get(name) or {}).items() if date.fromisoformat(v) >= today - keep}
        fresh = [r for r in rows if r["sym"] not in known]
        for r in rows:
            known[r["sym"]] = today.isoformat()
        st[name] = known
        if fresh and not first:
            out[name] = fresh
    return out


def arrival_alerts(new):
    cap = CONFIG["setups"]["alert_cap"]
    lines = {
        "Swing · at a level": lambda r: f'{r["sym"]} {abs(r["dist_to_bounce"]) * 100:.1f}% {"above" if r["dist_to_bounce"] >= 0 else "below"} '
                                        f'${r["bounce_level"]:.2f} (turned {r["bounce_touches"]}x; after {r["bounce_resolved_10d"]} touches '
                                        f'2 wk median {r["bounce_median_10d"] * 100:+.1f}%, {r["bounce_hit_rate_10d"] * 100:.0f}% up; swings {r["swing_amplitude"] * 100:.0f}%/wk)',
        "Quality dip": lambda r: f'{r["sym"]} score {r.get("score_overall") or 0:.0f}, {r["ret_1w"] * 100:+.1f}% this week '
                                 f'({r["dip_sigma_1w"]:+.1f} sd), value: {r.get("v_value") or "?"}',
        VALUE_LIST: lambda r: f'{r["sym"]} score {r.get("score_overall") or 0:.0f} at ${r["price"]:.2f}, analyst target '
                              f'${r["target_mean"]:.2f} ({r["upside_to_target"] * 100:+.0f}%, {r.get("analyst_count") or 0:.0f} analysts)',
    }
    out = []
    for name, rows in new.items():
        rows = [r for r in rows if not (name == VALUE_LIST and r.get("target_check"))]  # a doubtful target is listed, never pushed
        if not rows:
            continue
        shown = rows[:cap]
        out.append({"key": f"arr:{name}:{NOW_TS}", "level": "good", "title": f"{name}: {len(rows)} new",
                    "body": "\n".join(lines[name](r) for r in shown) + (f"\n...and {len(rows) - cap} more" if len(rows) > cap else "")})
    return out


# ------------------------------------------------------------------ catalysts

@module("Earnings calendar")
def earnings_calendar(universe_syms, cache, back_days=0):
    """Nasdaq's earnings calendar (free, no key): the next N days, plus `back_days` of reports already out (those
    rows carry the actual EPS and the surprise). Past days are cached; they don't change."""
    keep, out = set(universe_syms), []
    past = cache.setdefault("cal", {})
    today = NOW.astimezone(ET).date()
    for k in range(-back_days, CONFIG.get("earnings_days", 14) + 1):
        d = today + timedelta(days=k)
        if d.weekday() >= 5:
            continue
        if k < 0 and d.isoformat() in past:
            rows = past[d.isoformat()]
        else:
            data = get_json("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()}, headers=BROWSER) or {}
            rows = (data.get("data") or {}).get("rows") or []
            time.sleep(0.6)
            if k < 0:
                past[d.isoformat()] = [r for r in rows if (r.get("symbol") or "").strip().upper() in keep]
        for r in rows:
            sym = (r.get("symbol") or "").strip().upper()
            if sym in keep:
                out.append({"sym": sym, "date": d.isoformat(), "name": r.get("name"),
                            "time": {"time-pre-market": "before open", "time-after-hours": "after close"}.get(r.get("time"), ""),
                            "eps_est": fnum(r.get("epsForecast")), "ests": fnum(r.get("noOfEsts")),
                            "last_eps": fnum(r.get("lastYearEPS")), "mcap": fnum(r.get("marketCap")),
                            "eps": fnum(r.get("eps")), "surprise": fnum(r.get("surprise"))})
    cutoff = (today - timedelta(days=45)).isoformat()
    cache["cal"] = {k: v for k, v in past.items() if k >= cutoff}
    return out


def earnings_feedback(reported, scr, f, snapshot_date):
    """Reports released after the Screener scored a stock: the surprise, how the price took it, and a flag that the
    score predates the news. The flagged tickers go to docs/rescore.txt for the next Screener refresh."""
    c = f["c"] if f is not None else None
    out = []
    for e in reported:
        rec = scr.get(e["sym"])
        if not rec or e.get("eps") is None:
            continue
        scored = rec.get("last_earnings_date") or snapshot_date or ""
        reaction = None
        if c is not None and e["sym"] in c:
            cs = c[e["sym"]].dropna()
            idx = [d.strftime("%Y-%m-%d") for d in cs.index]
            # before the open: the report day's move; after the close (or unknown time): the next session's move
            day = e["date"] if e["time"] == "before open" else next((d for d in idx if d > e["date"]), None)
            if day in idx and idx.index(day) > 0:
                i = idx.index(day)
                reaction = r2((cs.iloc[i] / cs.iloc[i - 1] - 1) * 100)
        out.append({"sym": e["sym"], "name": rec.get("name"), "date": e["date"], "time": e["time"], "eps": e["eps"],
                    "eps_est": e["eps_est"], "surprise": e.get("surprise"), "reaction": reaction,
                    "score": rec.get("score_overall"), "v_value": rec.get("v_value"), "stale": e["date"] > scored[:10]})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


@module("FINRA short interest")
def short_interest(universe_syms, cache):
    """Latest FINRA short interest (published twice a month). Days to cover = shares short / average daily volume."""
    url = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"
    hdr = {"Accept": "application/json"}

    def rows_for(date, limit=1, offset=0):
        r = SESSION.post(url, json={"limit": limit, "offset": offset, "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "settlementDate", "fieldValue": date}]}, headers=hdr, timeout=60)
        return r.json() if r.status_code == 200 and r.text.strip() else []
    latest = None
    for back in range(0, 50):  # settlement dates are mid-month and month-end; published about 8 business days later
        d = (NOW - timedelta(days=back)).date()
        if d.weekday() < 5 and rows_for(d.isoformat()):
            latest = d.isoformat()
            break
    if not latest:
        raise RuntimeError("no short-interest release found in the last 50 days")
    if (cache.get("si") or {}).get("date") == latest:
        return cache["si"]
    keep, data, off = set(universe_syms), {}, 0
    while True:
        rows = rows_for(latest, 5000, off)
        for r in rows:
            sym = (r.get("symbolCode") or "").upper()
            if sym in keep:
                data[sym] = {"short": r.get("currentShortPositionQuantity"), "dtc": fnum(r.get("daysToCoverQuantity")),
                             "chg_pct": fnum(r.get("changePercent")), "adv": r.get("averageDailyVolumeQuantity")}
        if len(rows) < 5000:
            break
        off += 5000
    cache["si"] = {"date": latest, "data": data}
    return cache["si"]


# ------------------------------------------------------------------ alerts

def send_alerts(alerts, state):
    topic = os.getenv("NTFY_TOPIC", "").strip()
    sent, recent = state.setdefault("sent", {}), state.setdefault("recent", [])
    for a in alerts:
        if NOW_TS - sent.get(a["key"], 0) < ALERTS.get("cooldown_hours", 12) * 3600:
            continue
        sent[a["key"]] = NOW_TS
        recent.insert(0, {"ts": NOW_TS, "title": a["title"], "body": a["body"], "level": a["level"]})
        if topic:
            try:
                SESSION.post(f"https://ntfy.sh/{topic}", data=a["body"].encode(), timeout=15, headers={
                    "Title": a["title"].encode("ascii", "ignore").decode(), "Tags": "chart_with_upwards_trend",
                    "Priority": "high" if a["level"] == "hot" else "default"})
            except Exception as e:  # noqa: BLE001
                log(f"alert failed: {e}")
    state["recent"] = recent[:40]
    state["sent"] = {k: v for k, v in sent.items() if NOW_TS - v < 7 * 86400}


# ------------------------------------------------------------------ modes

def daily():
    cache, state = load(CACHE_PATH, {}), load(STATE_PATH, {})
    universe = build_universe(cache) or (cache.get("universe") or {}).get("symbols") or []
    syms = [u["sym"] for u in universe]
    wl = watchlist()
    screener = load(SCREENER_PATH, {}) or {}
    scr = screener.get("stocks") or {}
    snap_date = (screener.get("snapshot_built_at") or "")[:10]
    log(f"universe {len(universe)} stocks, watchlist {len(wl)}, Screener scores {len(scr)} (snapshot {snap_date or 'none'})")
    # ~2.2 years: the swing levels look back two years and need the outcome of each touch after it
    bars = daily_bars(sorted(set(syms + ETFS + wl + list(scr))), 800) if universe else {}
    f = frames(bars) if bars else None
    vixd = fetch_vix()
    regime = market_regime(f, vixd, syms) if f is not None else None
    sectors = sector_table(f) if f is not None else None
    smap = sector_map(cache) or (cache.get("sector_map") or {}).get("map") or {}
    have = set(syms)
    extra = [{"sym": s, "name": (scr.get(s) or {}).get("name") or s, "exch": ""} for s in dict.fromkeys(wl + list(scr)) if s not in have]
    stocks = stock_table(f, universe + extra, scr, smap) if f is not None else None
    back = 0
    if snap_date:
        back = min(30, max(0, (NOW.astimezone(ET).date() - date.fromisoformat(snap_date)).days))
    cal = earnings_calendar(sorted(have | set(wl) | set(scr)), cache, back_days=back) or []
    today_iso = NOW.astimezone(ET).date().isoformat()
    earnings = [e for e in cal if e["date"] >= today_iso]
    reported = earnings_feedback([e for e in cal if e["date"] < today_iso], scr, f, snap_date)
    stale = sorted({x["sym"] for x in reported if x["stale"]})
    RESCORE_PATH.write_text("".join(s + "\n" for s in stale))
    si = short_interest(syms + wl, cache) or cache.get("si") or {}
    si_data = si.get("data", {})
    nxt = {}
    for e in earnings:
        nxt.setdefault(e["sym"], e)
    for row in stocks or []:
        e, s = nxt.get(row["sym"]), si_data.get(row["sym"])
        row["earn"] = [e["date"], e["time"]] if e else None
        row["dtc"], row["si_chg"] = (s["dtc"], s["chg_pct"]) if s else (None, None)
    by = {r["sym"]: r for r in stocks or []}
    # crowded shorts; "under pressure" when the price is also rising (above its 50-day average and up over 20 days),
    # since a high days-to-cover alone mostly flags quiet stocks nobody is squeezing
    sq = [{**{k: by[s][k] for k in ("sym", "name", "price", "chg1", "chg20", "vs50", "rs20", "dtc", "si_chg", "earn")},
           "short": v["short"], "pressure": (by[s]["vs50"] or 0) > 0 and (by[s]["chg20"] or 0) > 0}
          for s, v in si_data.items() if s in by and (v["dtc"] or 0) >= CONFIG["short_interest"]["min_days_to_cover"]]
    sq.sort(key=lambda x: (not x["pressure"], -(x["dtc"] or 0)))

    alerts = []
    if regime and regime["label"] != regime["prev_label"]:
        alerts.append({"key": f"regime:{regime['as_of']}", "level": "hot" if regime["score"] < 35 else "good",
                       "title": f"Market regime: {regime['prev_label']} -> {regime['label']}",
                       "body": f"Score {regime['score']}/100. {regime['guide']}"})
    tomorrow = [e for e in earnings if e["sym"] in wl and e["date"] <= (NOW + timedelta(days=3)).date().isoformat()]
    if tomorrow:
        alerts.append({"key": f"earn:{NOW:%Y-%m-%d}", "level": "hot", "title": "Watchlist earnings coming up",
                       "body": "; ".join(f'{e["sym"]} {e["date"]} {e["time"]}'.strip() for e in tomorrow)})
    lists = setups(stocks or [])
    alerts += arrival_alerts(arrivals(state, lists, NOW.astimezone(ET).date()))
    send_alerts(alerts, state)
    if f is not None:
        cache["prev_close"] = {s: float(c.dropna().iloc[-1]) for s, c in f["c"].items() if c.notna().any()}
        cache["session"] = f["c"].index[-1].strftime("%Y-%m-%d")
    # what the 15-minute scan needs to re-price the lists with the live price
    cand = {r["sym"]: r for name, rs in lists.items() for r in rs}
    st = CONFIG["setups"]
    for r in stocks or []:  # names that could join a list on a live move: swing names near a level, screened stocks
        near = r.get("dist_to_bounce") is not None and abs(r["dist_to_bounce"]) <= 0.10 and (r.get("swing_amplitude") or 0) >= st["swing_min_amplitude"]
        if near or (r.get("v_data") and (r.get("dollar_volume_20d") or 0) >= st["min_dollar_volume"]):
            cand.setdefault(r["sym"], r)
    cache["setups"] = {s: {k: r.get(k) for k in SETUP_KEEP if k not in ("spark",)} for s, r in cand.items()}

    save(OUT_PATH, {"generated_at": NOW.isoformat(timespec="seconds"), "session": cache.get("session"),
                    "regime": regime, "sectors": sectors, "earnings": earnings, "short_date": si.get("date"),
                    "squeeze": [x for x in sq if x["pressure"]][:CONFIG["short_interest"]["list_size"]]
                    + [x for x in sq if not x["pressure"]][:CONFIG["short_interest"]["list_size"]],
                    "regimes": [{"min": lo, "label": lb, "guide": g, "stats": s} for lo, lb, g, s in REGIMES],
                    "setups": {k: [{f: r.get(f) for f in SETUP_KEEP} for r in v[:60]] for k, v in lists.items()},
                    "setup_settings": CONFIG["setups"], "reported": reported[:80], "rescore": stale,
                    "screener": {"snapshot": snap_date, "exported_at": screener.get("exported_at"), "count": len(scr)},
                    "universe_count": len(universe),
                    "alerts": state.get("recent", []), "errors": ERRORS, "watchlist_missing": [s for s in wl if s not in by]})
    if stocks is not None:
        save(STOCKS_PATH, {"generated_at": NOW.isoformat(timespec="seconds"), "stocks": stocks})
    save(CACHE_PATH, cache)
    save(STATE_PATH, state)
    log(f"daily done: {len(stocks or [])} stocks, {len(earnings)} earnings, {len(reported)} reported ({len(stale)} need rescoring), "
        f"{len(sq)} high short interest; " + ", ".join(f"{k} {len(v)}" for k, v in lists.items()) + f"; errors={len(ERRORS)}")


def premarket(day=None, dry=False):
    """US pre-market (4:00-9:30 New York): stocks up 3%+ on yesterday's close with real money behind the move.
    Full-market bars are 15 minutes delayed on the free plan, so this sees the pre-market up to 15 minutes ago.
    `day` replays a past morning for testing (no alerts sent)."""
    cache, state = load(CACHE_PATH, {}), load(STATE_PATH, {})
    universe = [u["sym"] for u in (cache.get("universe") or {}).get("symbols", [])]
    wl = watchlist()
    syms = sorted(set(universe + list(INDICES) + wl))
    day = day or datetime.now(ET).date()
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET)
    end = min(datetime(day.year, day.month, day.day, 9, 29, tzinfo=ET), NOW.astimezone(ET) - timedelta(minutes=16))
    if end <= start + timedelta(minutes=5):
        log("pre-market: too early for delayed data")
        return
    prev = prev_closes(syms, day)
    bars = intraday_bars(syms, start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                         end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    a = CONFIG.get("premarket", {})
    today_earn = {e["sym"]: e for e in (load(OUT_PATH, {}) or {}).get("earnings", []) if e["date"] == day.isoformat()}
    names = {u["sym"]: u["name"] for u in (cache.get("universe") or {}).get("symbols", [])}
    rows = []
    for s, bs in bars.items():
        pc = prev.get(s)
        if not pc or not bs:
            continue
        p, dv = bs[-1]["c"], sum(b["c"] * b["v"] for b in bs)
        rows.append({"sym": s, "name": names.get(s, s), "price": r2(p, 4), "prev": r2(pc, 4), "chg": r2(pct(p, pc)),
                     "dv": round(dv), "trades": sum(b.get("n", 0) for b in bs), "watch": s in wl,
                     "earn": today_earn[s]["time"] or "today" if s in today_earn else None,
                     "spark": [r2(b["c"], 4) for b in bs][-40:]})
    floor = a.get("min_dollar_volume", 1_000_000)
    liquid = [r for r in rows if r["dv"] >= floor and r["sym"] not in INDICES]
    up = sorted([r for r in liquid if r["chg"] >= a.get("alert_pct", 3)], key=lambda r: (not r["watch"], -r["chg"]))
    down = sorted([r for r in liquid if r["chg"] <= -a.get("alert_pct", 3)], key=lambda r: r["chg"])
    idx = {r["sym"]: {k: r[k] for k in ("price", "chg", "dv")} for r in rows if r["sym"] in INDICES}
    alerts = []
    seen = state.setdefault("pm_alerted", {})
    seen = {k: v for k, v in seen.items() if v == day.isoformat()}
    fresh = [r for r in up if r["sym"] not in seen]
    if fresh and not dry:
        spy = (idx.get("SPY") or {}).get("chg")
        alerts.append({"key": f"pm:{NOW_TS}", "level": "good",
                       "title": f"Pre-market up {a.get('alert_pct', 3)}%+: " + ", ".join(("★" if r["watch"] else "") + r["sym"] for r in fresh[:6]),
                       "body": "; ".join(f'{r["sym"]} {r["chg"]:+.1f}% ({usd_short(r["dv"])} traded{", earnings " + r["earn"] if r["earn"] else ""})' for r in fresh[:8])
                               + (f". SPY {spy:+.1f}% pre-market." if spy is not None else "") + " Data 15 min delayed."})
        for r in fresh:
            seen[r["sym"]] = day.isoformat()
    state["pm_alerted"] = seen
    send_alerts(alerts, state)
    out = load(LIVE_PATH, {}) or {}
    out.update({"premarket": {"generated_at": NOW.isoformat(timespec="seconds"), "day": day.isoformat(), "as_of": end.isoformat(timespec="minutes"),
                              "test": dry, "indices": idx, "up": up[:40], "down": down[:20], "count": len(rows), "liquid": len(liquid),
                              "settings": {"alert_pct": a.get("alert_pct", 3), "min_dollar_volume": floor}},
                "alerts": state.get("recent", [])[:20]})
    save(LIVE_PATH, out)
    save(STATE_PATH, state)
    log(f"pre-market done: {len(rows)} stocks traded, {len(liquid)} with ${floor:,.0f}+, {len(up)} up {a.get('alert_pct', 3)}%+, {len(fresh)} new")


def live_setups(cands, moves):
    """Re-price the daily candidates at the live price: distance to the bounce level and room to resistance, the
    week's move in standard deviations, and the upside to the analysts' target and the Screener's fair value."""
    rows = []
    for s, base in cands.items():
        m = moves.get(s)
        if not m or not m.get("price"):
            continue
        r, p = dict(base), m["price"]
        r["price"], r["chg1"] = p, m.get("chg")
        if r.get("bounce_level"):
            r["dist_to_bounce"] = p / r["bounce_level"] - 1
        if r.get("resistance_level"):
            r["room_to_resistance"] = r["resistance_level"] / p - 1
        if r.get("close_5ago") and r.get("sigma_daily"):
            r["ret_1w"] = p / r["close_5ago"] - 1
            r["dip_sigma_1w"] = r["ret_1w"] / (r["sigma_daily"] * math.sqrt(5))
        if r.get("target_mean"):
            r["upside_to_target"] = r["target_mean"] / p - 1
        if r.get("fair_value_per_share"):
            r["upside_to_fair"] = r["fair_value_per_share"] / p - 1
        rows.append(r)
    return setups(rows)


def usd_short(v):
    return f"${v / 1e6:.1f}M" if v >= 1e6 else f"${v / 1e3:.0f}K"


def live():
    """Every 15 minutes: the pre-market scan before the open, live moves while the market is open."""
    if "--premarket-date" in sys.argv:  # replay a past morning (testing; no alerts)
        return premarket(date.fromisoformat(sys.argv[sys.argv.index("--premarket-date") + 1]), dry=True)
    clock = alpaca("/clock", base=TRADE_API) if AK else {}
    nxt = datetime.fromisoformat(clock["next_open"]).astimezone(ET) if (clock or {}).get("next_open") else None
    now_et = NOW.astimezone(ET)
    if not (clock or {}).get("is_open") and "--force" not in sys.argv:
        if nxt and nxt.date() == now_et.date() and now_et.hour >= 4 and now_et < nxt:
            return premarket()
        log(f"market closed (next open {(clock or {}).get('next_open')}); nothing to do")
        return
    cache, state = load(CACHE_PATH, {}), load(STATE_PATH, {})
    universe = [u["sym"] for u in (cache.get("universe") or {}).get("symbols", [])]
    wl = watchlist()
    cands = cache.get("setups") or {}
    syms = sorted(set(universe + ETFS + wl + list(cands)))
    prev = prev_closes(syms, now_et.date()) or cache.get("prev_close") or {}
    snap = snapshots(syms)

    def move(sym):
        s = snap.get(sym) or {}
        p = fnum((s.get("latestTrade") or {}).get("p")) or fnum((s.get("dailyBar") or {}).get("c"))
        pc = prev.get(sym) or fnum((s.get("prevDailyBar") or {}).get("c"))
        day = s.get("dailyBar") or {}
        return {"price": r2(p, 4), "chg": r2(pct(p, pc)), "high": fnum(day.get("h")), "low": fnum(day.get("l"))} if p and pc else None
    moves = {s: move(s) for s in syms}
    uni = [moves[s]["chg"] for s in universe if moves.get(s) and moves[s]["chg"] is not None]
    vix = {}
    try:
        vix = cboe_live("VIX")
    except Exception as e:  # noqa: BLE001
        ERRORS["CBOE live VIX"] = str(e)[:200]
    out = {**({"premarket": (load(LIVE_PATH, {}) or {}).get("premarket")}),  # keep this morning's pre-market list
           "generated_at": NOW.isoformat(timespec="seconds"), "session_open": True,
           "indices": {k: moves.get(k) for k in INDICES}, "sectors": {k: moves.get(k) for k in SECTORS},
           "vix": vix, "watch": {s: moves.get(s) for s in wl},
           "breadth": {"count": len(uni), "up": sum(1 for x in uni if x > 0), "down": sum(1 for x in uni if x < 0),
                       "up2": sum(1 for x in uni if x >= 2), "down2": sum(1 for x in uni if x <= -2),
                       "median": r2(sorted(uni)[len(uni) // 2]) if uni else None},
           "errors": ERRORS}
    alerts = []
    spy = (moves.get("SPY") or {}).get("chg")
    if spy is not None and spy <= -ALERTS.get("spy_intraday_drop_pct", 1.5):
        alerts.append({"key": f"spydrop:{NOW:%Y-%m-%d}", "level": "hot", "title": f"SPY {spy:+.1f}% today",
                       "body": f"Breadth {out['breadth']['up']} up / {out['breadth']['down']} down. VIX {vix.get('price')}."})
    for lvl in ALERTS.get("vix_levels", []):
        if (vix.get("price") or 0) >= lvl > (vix.get("prev") or 99):
            alerts.append({"key": f"vix{lvl}:{NOW:%Y-%m-%d}", "level": "hot", "title": f"VIX above {lvl}",
                           "body": f"VIX {vix['price']:.1f} ({vix.get('chg_pct') or 0:+.1f}% today). Swings are widening: size down, widen stops."})
    for s in wl:
        m = moves.get(s)
        if m and m["chg"] is not None and abs(m["chg"]) >= ALERTS.get("watchlist_move_pct", 5):
            alerts.append({"key": f"wl:{s}:{NOW:%Y-%m-%d}", "level": "hot" if m["chg"] < 0 else "good",
                           "title": f"{s} {m['chg']:+.1f}% today", "body": f"{s} at {m['price']} ({m['chg']:+.1f}% vs yesterday's close)."})
    lists = live_setups(cands, moves) if cands else {}
    alerts += arrival_alerts(arrivals(state, lists, now_et.date()))
    out["setups"] = {k: [{f: r.get(f) for f in SETUP_KEEP if f != "spark"} for r in v[:60]] for k, v in lists.items()}
    send_alerts(alerts, state)
    out["alerts"] = state.get("recent", [])[:20]
    save(LIVE_PATH, out)
    save(STATE_PATH, state)
    log(f"live done: {len(uni)} stocks priced, SPY {spy}, errors={len(ERRORS)}")


if __name__ == "__main__":
    live() if "--live" in sys.argv else daily()
