# Stock Cockpit

An always-on, phone-first dashboard for trading US stocks. It runs entirely on GitHub's servers (free for a public repo), so it keeps updating with your laptop off. It's the "when to act" companion to the fundamentals Screener, which decides "what to own".

Not financial advice: it informs decisions and never trades.

## What it shows

| Tab | What | Refreshed |
|---|---|---|
| Header | **Market regime gauge** (0-100): SPY trend vs its 50/200-day averages, breadth (share of ~1,350 liquid stocks above their 50-day average, new highs vs lows), VIX level and curve. Plus SPY/QQQ/IWM, VIX and breadth cards | Daily after the close; live every 15 min in market hours |
| Levels | The Screener's swing screen, live: high-beta names that oscillate (7%+ weekly swings, choppy, $20M+ a day) within 4% of a level they've turned at 3+ times, with what followed past touches, the ceiling overhead and beta | Daily + re-priced every 15 min |
| Value | Your Screener's scores re-priced live: "Good score at buy price" (score 60+, at or near the DCF buy price, multiples agree), "Quality dip" (down 1+ standard deviation this week for that stock, best business first), and earnings reported since the score was computed (surprise, price reaction, rescore list) | Daily + re-priced every 15 min |
| Market | What the score is made of, how each regime behaved 2016-2026, sector ETFs ranked vs SPY | Daily + live |
| Watchlist | Your tickers: live price, trend, strength vs SPY, next earnings, days to cover | Live every 15 min |
| Earnings | Next 14 days for every liquid stock (Nasdaq calendar), with time and EPS estimate | Daily |
| Short interest | FINRA days to cover; "under pressure" = crowded shorts in a rising stock | Twice a month (FINRA) |
| Stocks | All liquid US stocks (price over $5, $20M+ traded a day), searchable and sortable | Daily |
| Alerts | Regime changes, VIX above 25/30, SPY down 1.5%+ intraday, watchlist moves of 5%+, watchlist earnings in 3 days (also pushed via ntfy) | |

## What was tested before building (S&P 1500, daily data 2016-2026)

- **The regime gauge separates calm from dangerous markets.** Over the next 20 days, swings averaged 12% a year in calm uptrends vs 30% in stress, and the average worst dip was -3.0% vs -6.8%. But stress had the best average 20-day return (+3.2%), so it is a gauge for **position size and stop width**, not a sell signal.
- **Daily "red-day relative strength" does not work in US stocks.** Stocks that held up or closed green on SPY's red days lagged slightly afterwards; the weakest bounced (short-term reversal). Gap-ups, 52-week-high breakouts and oversold dips judged at the close showed no robust edge either. So none of these are built on daily data. Intraday versions (which stocks turn green first during a sell-off, opening-range breakouts) are to be tested on Alpaca minute data before anything is added.

## Setup

1. **Alpaca** (free): create an account at alpaca.markets, use the paper-trading side, and generate an API key. The free "Basic" market data plan is enough: live IEX prices, full-market data 15 minutes delayed, history since 2016, 200 calls a minute.
2. **Secrets** (repo Settings > Secrets and variables > Actions): `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `NTFY_TOPIC` (your ntfy topic; can be the same as the crypto Cockpit's).
3. **Pages**: Settings > Pages, deploy from branch `main`, folder `/docs`.
4. **Timers** (cron-job.org, same token style as the crypto Cockpit, with Actions: Read and write on this repo):
   - daily: `https://api.github.com/repos/YOUR-USERNAME/stock-cockpit/actions/workflows/daily.yml/dispatches`, Monday-Friday at 21:40 UTC, body `{"ref":"main","inputs":{"auto":"true"}}`
   - live: same URL with `live.yml`, every 15 minutes, Monday-Friday 13:00-21:00 UTC, same body. Outside market hours (and on holidays) the scan exits after asking Alpaca whether the market is open.
5. **Phone**: open the Pages link and add it to your home screen. To edit the watchlist from the phone, add this repo to your fine-grained GitHub token (Contents: Read and write) and paste the token in the dashboard's settings.

## Refresh button

The ↻ button in the header asks GitHub to run the collector now: during US market hours (pre-market included) the live scan (about a minute), otherwise the full daily update (about 3 minutes). The page redraws when the new data lands. It uses the token from the dashboard's settings, which needs **Actions: Read and write** on this repo (plus Contents: Read and write for watchlist edits).

## Screener scores (laptop, only after a fundamentals rebuild)

The Screener's daily refresh only updates prices, which the Cockpit already does itself every 15 minutes, so there is nothing to do day to day. The fundamentals stay in the Screener; the Cockpit only re-prices them. When the Screener rebuilds its fundamentals snapshot (after earnings season, or after rescoring the tickers the Value tab marks as reported since scored):

    python tools/export_screener.py --push

It reads `Stock Fundamental Analysis v3/data/snapshot.parquet` read-only and publishes scores, verdicts and fair-value/buy-below prices to `docs/screener.json`. Stocks that report earnings after their score was computed are listed in `docs/rescore.txt`, which the Screener's refresh can read (`python refresh_cli.py --file <cockpit>/docs/rescore.txt`).

Alerts follow the Screener's `notify.py` rules: only new arrivals on "Swing · at a level", "Quality dip" and "Good score at buy price"; the first run seeds silently; a name off a list for 21 days can alert again.

## Customise (`config.json`)
Universe filters, indices and sector ETFs, earnings window, short-interest thresholds, alert levels.

## Good to know
- Live prices come from one exchange (IEX) and are compared with the previous full-market close; full-market volume is 15 minutes delayed on the free plan.
- Short interest is reported twice a month and published about a week later.
- The repo is public: never commit keys, holdings or position sizes.
