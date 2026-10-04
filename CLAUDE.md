# Stock Cockpit: project brief for Claude Code

## Owner and goal
Raza (Karachi). Trades US stocks, swing and intraday, mostly checked on his phone. Plain English with technical terms explained, direct answers, concise pressure-testing. Not financial advice tooling: it informs decisions, it never trades.

This repo is the always-on **trading** side ("when to act"). The **fundamentals** side is his local Streamlit Screener (`Documents/Projects/Stock Fundamental Analysis v3`: yfinance + SEC EDGAR, insider buying, AI summaries, `swing.py`/`tactical.py`/`insider_dips.py`), which stays on the laptop. Don't duplicate its insider-buying work here (owner: insider buying is in the Screener, and e.g. ZTS sat flat for months despite it). Sister project with the same architecture: `battlemaster69/Cockpit` (crypto).

## Repo and hosting
- `battlemaster69/stock-cockpit`, public (free Actions minutes and Pages). Pages serves `/docs` from `main`; `docs/.nojekyll` must exist.
- Secrets: `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `NTFY_TOPIC`.
- GitHub's own cron is best-effort (on the crypto repo it never fired for hours), so cron-job.org timers dispatch `daily.yml` (once after the close) and `live.yml` (every 15 min, Mon-Fri 13-21 UTC) with `auto=true`; both skip when their output is fresh.

## Architecture
- `collector/collect.py --daily`: universe (Nasdaq Trader directory minus ETFs/warrants/units/preferreds, then price >= $5 and 20-day dollar volume >= $20M via Alpaca daily bars; rebuilt weekly, ~1,350 stocks), 400 days of SIP daily bars (`adjustment=all`, end 16 min ago because the free plan can't read the latest 15 min of SIP), regime/breadth/sectors, per-stock table (`docs/stocks.json`), Nasdaq earnings calendar (no key, browser UA), FINRA short interest (public API; sorting needs an EQUAL settlementDate filter, so probe recent weekdays for the latest release), alerts. Writes `docs/data.json`, `docs/stocks.json`, `data/cache.json` (universe, prev closes, short interest), `data/state.json` (ntfy dedupe).
- `collector/collect.py --live`: Alpaca clock (paper API) -> if open, IEX snapshots for universe + ETFs + watchlist, compared with yesterday's SIP close from the cache; CBOE delayed VIX JSON. Writes `docs/live.json`.
- `docs/index.html`: single-file dashboard, vanilla JS, no build. Watchlist edits commit `docs/watchlist.json` via the GitHub API with a fine-grained token in the phone's localStorage.

## Data sources checked from GitHub's US runners (Oct 2026)
Works: Alpaca data API, Nasdaq Trader symbol files, Nasdaq earnings API, FINRA short volume + short interest API, SEC data.sec.gov, CBOE CSVs and delayed quotes (follow redirects), Wikipedia. Blocked: Yahoo/yfinance (429), SEC www.sec.gov Archives without a proper "Name email" User-Agent (403).

## Findings that shape what gets built (S&P 1500 current members, daily, 2016-2026; survivorship caveat)
- Regime score buckets: calm uptrend 65% of days, SPY next 20d +0.94%, vol 12%, avg worst dip -3.0%; stress 10% of days, +3.2%, vol 30%, dip -6.8%. Monotonic in risk, not in return: present as a sizing/stops gauge.
- Red-day relative strength (green on SPY -1% days, top decile, leaders near highs, holding near highs in 3% pullbacks): no edge, slightly negative over 5-20 days; weakest decile bounced (+1.1-2.3% excess, ~51-55% beat, flattered by survivorship). Gap-ups >=5% on 3x volume -0.7% (20d), 52-week-high breakouts -0.4%, RSI(2) dips ~0. Daily price/volume signals in liquid US stocks are largely priced by the close.
- Next step (Phase 1b): test intraday red-day strength and opening-range/volume breakouts on Alpaca minute data (SIP history since 2016 on the free plan), build only what passes, with a forward track record like the crypto Cockpit.

## Screener bridge (owner: "high beta stocks, the levels they respect and how far they are; good-score stocks at a not-good price; already in the Screener, make it live")
- `tools/export_screener.py` (run on the laptop) reads the Screener's `data/snapshot.parquet` read-only -> `docs/screener.json` (scores, axes, verdicts, z_score, fair_value_per_share, buy_below, last_earnings_date). Never write into the Screener's folder.
- `collector/swing.py` is the Screener's `swing.py` ported verbatim (only `plain.money` replaced). Keep the two in step; if the Screener's version changes, re-port.
- `tactical_measures()` mirrors the Screener's `tactical.py` (dip in own-volatility units, median dollar volume, rel volume) on Alpaca adjusted bars (so swing's raw-to-adjusted scale is 1), plus beta to SPY.
- `setups()` = the Screener's lens gates (`lenses.py`): "Swing · at a level" and "Swing · movers" sorted by amplitude (not by the small-sample hit rate), "Quality dip" = "Dip · liquid" ranked by score_overall as `notify.py` does. "Good score at buy price" is new: score >= 60, price within 5% of buy_below AND v_value in at-buy/cheap-on-one/both, best score first. The Screener's reverse DCF gives fair value > 2x price for ~200 of 1,650 stocks (lenders, spin-offs), so fair value > 3x price is tagged `model_check` and never alerted.
- Live: `live_setups()` re-prices cached candidates (`cache.setups`) with IEX prices; `arrivals()` follows notify.py (new names only, silent first run, 21-day cooldown).
- Earnings feedback: the Nasdaq calendar is also read backwards to the snapshot date (past days carry actual EPS and surprise; cached in `cache.cal`); reports after a stock's `last_earnings_date` are flagged stale, with the price reaction, and written to `docs/rescore.txt`.

## Hard constraints
- Free tiers only (Alpaca Basic 200 calls/min; batch symbols, about 200 per request).
- Public repo: never commit keys, holdings or position sizes.
- Phone-first dashboard, light and dark themes, no build step.

## Working conventions
- Test collector changes locally before pushing (mock Alpaca with saved bars if no key), then read the Actions log after each push.
- Explain changes to the owner in plain English, briefly.
