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

ROOT = Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text())
DOCS, DATA = ROOT / "docs", ROOT / "data"
OUT_PATH, STOCKS_PATH, LIVE_PATH = DOCS / "data.json", DOCS / "stocks.json", DOCS / "live.json"
WATCH_PATH = DOCS / "watchlist.json"
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


@module("Stock table")
def stock_table(f, universe):
    """One row per universe stock: trend, momentum vs SPY, volatility and liquidity (the Stocks tab and watchlist)."""
    c, h, l, v = f["c"], f["h"], f["l"], f["v"]
    spy = c["SPY"].dropna()
    spy20, spy60 = pct(spy.iloc[-1], spy.iloc[-21]), pct(spy.iloc[-1], spy.iloc[-61])
    rows = []
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
        rows.append({"sym": s, "name": u["name"], "exch": u["exch"], "price": st["price"], "prev": r2(cs.iloc[-2], 4),
                     "chg1": st["chg1"], "chg5": st["chg5"], "chg20": st["chg20"], "vs50": st["vs50"], "vs200": st["vs200"],
                     "from_high": st["from_high"], "rs20": r2((st["chg20"] or 0) - (spy20 or 0)),
                     "rs60": r2(pct(cs.iloc[-1], cs.iloc[-61]) - (spy60 or 0)) if len(cs) > 61 else None,
                     "atr_pct": r2(atr / cs.iloc[-1] * 100), "dv20": round(float((cs * vs).iloc[-20:].mean())),
                     "rvol": r2(vs.iloc[-1] / vs.iloc[-21:-1].mean()) if vs.iloc[-21:-1].mean() else None,
                     "spark": [r2(x, 4) for x in cs.iloc[-60:].tolist()]})
    return rows


# ------------------------------------------------------------------ catalysts

@module("Earnings calendar")
def earnings_calendar(universe_syms):
    """Nasdaq's earnings calendar (free, no key) for the next N days, kept to the universe."""
    keep, out = set(universe_syms), []
    for k in range(CONFIG.get("earnings_days", 14) + 1):
        d = (NOW + timedelta(days=k)).date()
        if d.weekday() >= 5:
            continue
        data = get_json("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()}, headers=BROWSER) or {}
        for r in (data.get("data") or {}).get("rows") or []:
            sym = (r.get("symbol") or "").strip().upper()
            if sym in keep:
                out.append({"sym": sym, "date": d.isoformat(), "name": r.get("name"),
                            "time": {"time-pre-market": "before open", "time-after-hours": "after close"}.get(r.get("time"), ""),
                            "eps_est": fnum(r.get("epsForecast")), "ests": fnum(r.get("noOfEsts")),
                            "last_eps": fnum(r.get("lastYearEPS")), "mcap": fnum(r.get("marketCap"))})
        time.sleep(0.6)
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
    log(f"universe {len(universe)} stocks, watchlist {len(wl)}")
    bars = daily_bars(sorted(set(syms + ETFS + wl)), 400) if universe else {}
    f = frames(bars) if bars else None
    vixd = fetch_vix()
    regime = market_regime(f, vixd, syms) if f is not None else None
    sectors = sector_table(f) if f is not None else None
    extra = [{"sym": s, "name": s, "exch": ""} for s in wl if s not in set(syms)]
    stocks = stock_table(f, universe + extra) if f is not None else None
    earnings = earnings_calendar(syms + wl) or []
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
    send_alerts(alerts, state)
    if f is not None:
        cache["prev_close"] = {s: float(c.dropna().iloc[-1]) for s, c in f["c"].items() if c.notna().any()}
        cache["session"] = f["c"].index[-1].strftime("%Y-%m-%d")

    save(OUT_PATH, {"generated_at": NOW.isoformat(timespec="seconds"), "session": cache.get("session"),
                    "regime": regime, "sectors": sectors, "earnings": earnings, "short_date": si.get("date"),
                    "squeeze": [x for x in sq if x["pressure"]][:CONFIG["short_interest"]["list_size"]]
                    + [x for x in sq if not x["pressure"]][:CONFIG["short_interest"]["list_size"]],
                    "regimes": [{"min": lo, "label": lb, "guide": g, "stats": s} for lo, lb, g, s in REGIMES],
                    "universe_count": len(universe),
                    "alerts": state.get("recent", []), "errors": ERRORS, "watchlist_missing": [s for s in wl if s not in by]})
    if stocks is not None:
        save(STOCKS_PATH, {"generated_at": NOW.isoformat(timespec="seconds"), "stocks": stocks})
    save(CACHE_PATH, cache)
    save(STATE_PATH, state)
    log(f"daily done: {len(stocks or [])} stocks, {len(earnings)} earnings, {len(sq)} high short interest, errors={len(ERRORS)}")


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
    syms = sorted(set(universe + ETFS + wl))
    prev = prev_closes(syms, now_et.date()) or cache.get("prev_close") or {}
    snap = snapshots(syms)

    def move(sym):
        s = snap.get(sym) or {}
        p = fnum((s.get("latestTrade") or {}).get("p")) or fnum((s.get("dailyBar") or {}).get("c"))
        pc = prev.get(sym) or fnum((s.get("prevDailyBar") or {}).get("c"))
        day = s.get("dailyBar") or {}
        return {"price": r2(p, 4), "chg": r2(pct(p, pc)), "high": fnum(day.get("h")), "low": fnum(day.get("l"))} if p and pc else None
    moves = {s: move(s) for s in set(universe + ETFS + wl)}
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
    send_alerts(alerts, state)
    out["alerts"] = state.get("recent", [])[:20]
    save(LIVE_PATH, out)
    save(STATE_PATH, state)
    log(f"live done: {len(uni)} stocks priced, SPY {spy}, errors={len(ERRORS)}")


if __name__ == "__main__":
    live() if "--live" in sys.argv else daily()
