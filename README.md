# Stock Setup Scanner

Scans a universe of stocks (default: the S&P 500) and flags tickers that match one
of four technical setup patterns, with a 0-100 score and the specific reasons
each one triggered. **This is a rules-based technical screen, not investment
advice** — it only looks at price/volume, not fundamentals or news.

## Setups it looks for

- **Trend Pullback** — stock is in a confirmed uptrend (price > 50d SMA > 200d SMA,
  50d SMA rising) and has pulled back toward its 20d SMA with RSI cooling into the
  35-55 range and volume drying up — a classic "buy the dip in an uptrend" entry.
  Bonus if the swing-based market structure (see below) is still making higher lows;
  penalized if the pullback has closed below the last confirmed swing low.
- **Breakout** — price closes above its prior 20-day high, ideally on above-average
  volume and after a period of tightening volatility (a coiled range breaking out).
- **Volatility Squeeze** — Bollinger Band width near its tightest point in ~100
  days while the stock holds above its 50d SMA — flags stocks *coiling* for a
  potential move before it happens, so you can set an alert at the trigger level.
- **Structure Break** — market-structure analysis using swing highs/lows (a 5-bar
  fractal on each side). Fires on a fresh **break of structure (BOS)** — closing
  above the last swing high while already in a higher-high/higher-low uptrend — or
  a **change of character (CHoCH)** — the first close above the last swing high
  after a downtrend or mixed structure, often the earliest sign of a reversal.
  Only fires on the actual crossover day, not for every day price happens to sit
  above an old swing high.

Every row also shows the current **market structure** — `HH · HL` (uptrend: higher
highs, higher lows), `LH · LL` (downtrend), or `Mixed` — independent of which setup
fired, since it's useful context for the other three setups too.

Each row shows which setup(s) fired, a 0-100 score, RSI, volume vs. 20-day average,
a 60-day sparkline, and the plain-English reason (hover the "Why" cell for the
full explanation).

## Setup

```bash
pip3 install -r requirements.txt
```

## Live interactive dashboard (recommended)

```bash
python3 app.py --universe sp500 --open
```

Starts a local web app at `http://127.0.0.1:5050` with:
- A **Refresh** button that re-runs the scan live (no need to touch the terminal).
- **Tabs** to filter by setup type (All / Trend Pullback / Breakout / Volatility
  Squeeze / Structure Break), with live counts. Persists across refresh.
- **Sortable columns** — click any column header (Ticker, Price, 1d, RSI14, Vol/avg,
  Structure, Setup, Score) to sort; click again to reverse. Persists across refresh.
- A subtle animated background (soft drifting color blobs, off in both light and
  dark mode, respects `prefers-reduced-motion`) — purely decorative, stays out of
  the way of the data.
- **Click any row** to open a detail panel: every setup's score/reasons, the full
  indicator readout (SMAs, RSI, ATR%, Bollinger width percentile, 20d/52w highs),
  and **recent news** for that ticker (via Yahoo Finance).
- A **Line / Candles** toggle on the detail chart, with a timeline axis. Both modes
  overlay a dashed **trend projection** — a naive linear regression fit over a
  lightly-smoothed 60-session trend (smoothing first, and using 60 sessions rather
  than 30, keeps a short-term pullback/consolidation from dominating the fit and
  pointing the wrong way vs. the larger trend), extrapolated 15 trading days
  forward. This is a simple statistical extrapolation for illustration, **not a
  price forecast**.
- A **reference stop-loss / take-profit** calculation, drawn on the chart and shown
  as tiles: stop below the last confirmed swing low (or 2x ATR(14) if there's no
  clean recent swing low), with 2R/3R take-profit targets. It's a fixed formula
  applied identically to every ticker — **not personalized advice** — always size
  positions and set stops to your own risk tolerance.
- A **position sizing** calculator: enter your account size and risk-per-trade %,
  and it computes shares to buy so a stop-out loses roughly that % of the account
  (dollars at risk ÷ risk-per-share), plus position value and % of account — with
  a warning if the sized position costs more than the account holds. Inputs are
  saved in your browser's `localStorage` only, never sent to the server.
- A **"Copy trade parameters"** button that copies symbol, share count, entry,
  stop, and targets to your clipboard, to paste into TradingView's (or your
  broker's) order ticket yourself. Nothing is entered or submitted on any site
  automatically — there's no supported way to deep-link into a broker's order
  form, and placing even a paper-trading order isn't something this app (or an
  AI agent) should do on your behalf on a live third-party platform.
- An **"Open on TradingView ↗"** button in the detail panel that jumps straight to
  that ticker's TradingView chart.

This is a local single-user tool (Flask's dev server) — don't expose the port to
the network. `--no-scan` skips the initial scan (start blank, hit Refresh);
`--port` changes the port.

## One-off static scan

```bash
python3 scan.py --universe sp500 --top 40 --out dashboard.html --open
```

`--universe` accepts `sp500`, `nasdaq100`, `sp500+nasdaq100`, a path to a text/CSV
file of tickers, or a comma-separated list (`AAPL,MSFT,NVDA`).

Useful flags: `--min-price`, `--min-avg-volume` (liquidity filters), `--period`
(history window, default `1y`).

Both `scan.py` and `app.py` share the same scoring engine (`analysis.py`); `scan.py`
just writes a static `dashboard.html` snapshot instead of running a live server —
useful for `watch.py` or a cron job.

## Continuous watch mode

Re-scans on an interval and alerts (macOS notification + console) when a *new*
ticker crosses your score threshold:

```bash
python3 watch.py --universe sp500 --interval-min 20 --min-score 60 --open
```

By default it only scans during US market hours (9:30-16:00 ET, Mon-Fri); pass
`--all-hours` to disable that. It rewrites `dashboard.html` every cycle, so leave
it open in a browser tab and just refresh.

## Notes / limitations

- Data comes from Yahoo Finance via `yfinance` (free, ~15-20 min delayed, daily
  bars). Good for swing-trade-style setups, not intraday scalping.
- Ticker lists (S&P 500 / Nasdaq 100) are scraped from Wikipedia and cached for 24h
  in `.cache/`.
- The scoring thresholds (RSI bands, volume ratios, Bollinger percentiles, swing
  fractal width, etc.) are reasonable defaults, not a backtested strategy — tune
  them in `analysis.py` (`SWING_WINDOW` controls swing sensitivity: smaller = more,
  noisier swing points; larger = fewer, more significant ones) to match your own
  trading style before relying on them.
- Nothing here executes trades or gives personalized advice; always verify a flag
  yourself and manage your own risk/position sizing.
- News comes from Yahoo Finance's article feed for the ticker (headline, source,
  time, link) — it's not vetted or summarized, just surfaced for you to read.
- The TradingView link uses `tradingview.com/symbols/<TICKER>/`, which resolves
  for the vast majority of US-listed tickers; a handful of thinly-traded or
  dual-listed symbols may land on the wrong exchange.

## Hosting it as a real website

`app.py` runs equally well under a production WSGI server, which is how it's
deployed (this repo includes `render.yaml` for one-click Render deployment):

```bash
gunicorn --workers 1 --threads 4 --timeout 280 -b 0.0.0.0:$PORT app:app
```

Single worker is intentional — state lives in an in-memory dict shared via a
lock, not a database, so only one process can hold it; threads let that one
process still serve concurrent visitors without blocking behind a scan.

Since there's no CLI invocation in this mode, configuration comes from env
vars instead: `SCANNER_UNIVERSE`, `SCANNER_PERIOD`, `SCANNER_MIN_PRICE`,
`SCANNER_MIN_AVG_VOLUME`, `SCANNER_TOP`, `SCANNER_AUTO_REFRESH_SECONDS` (how
often the server rescans on its own during market hours, default 1200s/20min).

Public-deployment notes:
- The `/api/scan` endpoint ignores any client-supplied parameters (universe
  etc. are a server-config decision, not a visitor's) and rate-limits itself
  (20s cooldown, and won't start a second scan while one's in flight) so
  repeated Refresh clicks can't be used to hammer Yahoo Finance.
- The page auto-refreshes itself every `SCANNER_AUTO_REFRESH_SECONDS` during
  market hours, so it stays reasonably current without anyone clicking Refresh.
- Free hosting tiers that sleep on inactivity (e.g. Render's free plan) will
  cold-start on the next visit and run an initial scan; the page polls itself
  every few seconds while a scan is in progress so it fills in automatically.
- There's no login/access control — anyone with the URL can view it and click
  Refresh. Add a host-level password (e.g. Render's built-in basic auth, or a
  reverse proxy) if you want it private instead.
