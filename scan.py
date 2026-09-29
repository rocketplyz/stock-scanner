#!/usr/bin/env python3
"""Scan a stock universe and flag tickers with a good technical setup/entry point.

Not financial advice — this is a rules-based technical pattern screener.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).parent))
from analysis import evaluate, compute_indicators  # noqa: E402
from universe import resolve_universe  # noqa: E402

CHUNK_SIZE = 150
YF_TIMEOUT = 30  # seconds - yfinance/requests has no default, so a hung/blocked host hangs forever without this


def flatten_ticker_columns(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """yfinance returns MultiIndex columns (field, ticker) even for a single
    ticker in recent versions - collapse that down to plain OHLCV columns."""
    if not isinstance(df.columns, pd.MultiIndex):
        return df
    for level in range(df.columns.nlevels):
        if ticker in df.columns.get_level_values(level):
            return df.xs(ticker, axis=1, level=level)
    df = df.copy()
    df.columns = df.columns.get_level_values(0)
    return df


def fetch_data(tickers: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """Batch-download OHLCV for many tickers. Returns {ticker: df} for tickers with data."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), CHUNK_SIZE):
        chunk = tickers[i : i + CHUNK_SIZE]
        for attempt in range(2):
            try:
                data = yf.download(
                    chunk, period=period, interval="1d", group_by="ticker",
                    progress=False, threads=True, auto_adjust=True, timeout=YF_TIMEOUT,
                )
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(3)
                    continue
                print(f"  ! chunk fetch failed ({e}); skipping {len(chunk)} tickers", file=sys.stderr)
                data = None
        if data is None or data.empty:
            continue
        if isinstance(data.columns, pd.MultiIndex):
            for t in chunk:
                if t in data.columns.get_level_values(0):
                    sub = data[t].dropna(how="all")
                    if not sub.empty:
                        out[t] = sub
        elif len(chunk) == 1:
            out[chunk[0]] = flatten_ticker_columns(data, chunk[0]).dropna(how="all")
        print(f"  fetched {min(i + CHUNK_SIZE, len(tickers))}/{len(tickers)} tickers", file=sys.stderr)
    return out


def _compute_one(ticker, df, min_price, min_avg_volume, timeout=15):
    """compute_indicators+evaluate for one ticker, with its own hard timeout so a
    single pathological ticker's data can't hang (or silently take minutes on) the
    whole batch - isolates the failure to that ticker (skipped, logged) instead."""
    box = {}

    def target():
        feats = compute_indicators(df)
        if feats is None:
            return
        if feats["close"] < min_price:
            return
        if not pd.isna(feats.get("vol20avg", float("nan"))) and feats["vol20avg"] < min_avg_volume:
            return
        ev = evaluate(feats)
        if ev["best_setup"] is None:
            return
        box["result"] = {"ticker": ticker, "features": feats, "eval": ev}

    t = threading.Thread(target=target, daemon=True)
    start = time.time()
    t.start()
    t.join(timeout)
    elapsed = time.time() - start
    if t.is_alive():
        print(f"  ! {ticker}: compute exceeded {timeout}s, skipping", file=sys.stderr)
        return None
    if elapsed > 2:
        print(f"  ! {ticker}: compute took {elapsed:.1f}s", file=sys.stderr)
    return box.get("result")


def run_scan(universe_spec: str, period="1y", min_price=5.0, min_avg_volume=300_000):
    tickers = resolve_universe(universe_spec)
    print(f"Universe: {len(tickers)} tickers ({universe_spec})", file=sys.stderr)
    price_data = fetch_data(tickers, period=period)
    print(f"Got data for {len(price_data)}/{len(tickers)} tickers", file=sys.stderr)

    results = []
    for i, (ticker, df) in enumerate(price_data.items(), 1):
        r = _compute_one(ticker, df, min_price, min_avg_volume)
        if r is not None:
            results.append(r)
        if i % 20 == 0:
            print(f"  computed {i}/{len(price_data)}", file=sys.stderr)
    print(f"Computed all {len(price_data)} tickers, {len(results)} flagged", file=sys.stderr)

    results.sort(key=lambda r: r["eval"]["best_score"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# HTML dashboard rendering
# ---------------------------------------------------------------------------

SETUP_COLORS = {
    "Trend Pullback": ("#2a78d6", "#3987e5"),      # categorical slot 1 blue
    "Breakout": ("#1baf7a", "#199e70"),            # categorical slot 3 aqua
    "Volatility Squeeze": ("#eda100", "#c98500"),  # categorical slot 4 yellow
    "Structure Break": ("#e87ba4", "#d55181"),     # categorical slot 5 magenta
}

STRUCTURE_LABELS = {
    "uptrend": ("HH · HL", "pos"),
    "downtrend": ("LH · LL", "neg"),
    "mixed": ("Mixed", "muted-text"),
}


def _structure_cell(struct: dict):
    label, cls = STRUCTURE_LABELS.get(struct.get("bias"), ("—", "muted-text"))
    event = struct.get("event")
    event_html = f'<div class="structure-event">{event}</div>' if event and event.startswith("Bullish") else ""
    return f'<span class="{cls}">{label}</span>{event_html}'


def _sparkline_svg(values, dates=None, width=120, height=32, show_axis=False):
    """A price line chart. With show_axis=True (the stock-detail modal), draws a
    light timeline below the line: gridlines + MM-DD date labels at 5 points."""
    if not values or len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    axis_h = 16 if (show_axis and dates) else 0
    total_h = height + axis_h

    def x_at(idx):
        return (idx / (n - 1)) * (width - 4) + 2

    def y_at(v):
        return height - 2 - ((v - lo) / span) * (height - 4)

    pts = [f"{x_at(i):.1f},{y_at(v):.1f}" for i, v in enumerate(values)]
    up = values[-1] >= values[0]
    color = "var(--good)" if up else "var(--muted-line)"

    axis_svg = ""
    if axis_h:
        tick_idx = sorted({0, (n - 1) // 4, (n - 1) // 2, (3 * (n - 1)) // 4, n - 1})
        lines, labels = [], []
        for i in tick_idx:
            xi = x_at(i)
            lines.append(f'<line x1="{xi:.1f}" y1="0" x2="{xi:.1f}" y2="{height:.1f}" '
                          f'stroke="var(--gridline)" stroke-width="1"/>')
            anchor = "start" if i == 0 else "end" if i == n - 1 else "middle"
            label = dates[i][5:]  # MM-DD
            labels.append(f'<text x="{xi:.1f}" y="{height + 12}" font-size="9" '
                           f'font-family="system-ui,-apple-system,sans-serif" '
                           f'fill="var(--muted)" text-anchor="{anchor}">{label}</text>')
        axis_svg = "".join(lines) + "".join(labels)

    return (
        f'<svg viewBox="0 0 {width} {total_h}" width="{width}" height="{total_h}" '
        f'class="spark" preserveAspectRatio="none">'
        f'{axis_svg}'
        f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" '
        f'stroke-width="1.75" stroke-linejoin="round" stroke-linecap="round"/></svg>'
    )


def _score_bar(score):
    pct = max(0, min(100, score))
    return (
        f'<div class="scorebar-track"><div class="scorebar-fill" style="width:{pct}%"></div></div>'
        f'<span class="scorebar-num">{pct:.0f}</span>'
    )


def _setup_badges(ev):
    badges = []
    for name, data in sorted(ev["setups"].items(), key=lambda kv: -kv[1]["score"]):
        if data["score"] <= 0:
            continue
        light, dark = SETUP_COLORS[name]
        badges.append(
            f'<span class="badge" style="--badge-light:{light};--badge-dark:{dark}">'
            f'{name} · {data["score"]:.0f}</span>'
        )
    return "".join(badges)


def row_html(r: dict, clickable: bool = False) -> str:
    f, ev = r["features"], r["eval"]
    best = ev["setups"][ev["best_setup"]]
    full_reason = "; ".join(best["reasons"])
    short_reason = best["reasons"][0] if best["reasons"] else ""
    chg = f["change_1d_pct"]
    chg_class = "pos" if chg >= 0 else "neg"
    row_attrs = ""
    if clickable:
        struct_rank = {"uptrend": 2, "mixed": 1, "downtrend": 0}.get(f["structure"].get("bias"), 1)
        vol_ratio = 0 if pd.isna(f["vol_ratio"]) else f["vol_ratio"]
        row_attrs = (
            f' class="clickable" data-ticker="{r["ticker"]}" data-price="{f["close"]:.4f}"'
            f' data-chg1d="{chg:.4f}" data-rsi="{f["rsi14"]:.2f}" data-volratio="{vol_ratio:.4f}"'
            f' data-structure="{struct_rank}" data-setup="{ev["best_setup"]}" data-score="{ev["best_score"]:.2f}"'
        )
    return f"""
            <tr{row_attrs}>
              <td class="ticker">{r['ticker']}</td>
              <td class="num">${f['close']:.2f}</td>
              <td class="num {chg_class}">{chg:+.1f}%</td>
              <td>{_sparkline_svg(f['close_series'][-60:])}</td>
              <td class="num">{f['rsi14']:.0f}</td>
              <td class="num">{f['vol_ratio']:.1f}x</td>
              <td>{_structure_cell(f['structure'])}</td>
              <td>{_setup_badges(ev)}</td>
              <td class="score">{_score_bar(ev['best_score'])}</td>
              <td class="reason" title="{full_reason}">{short_reason}</td>
            </tr>"""


def stats_html(results: list) -> str:
    n_flagged = len(results)
    n_breakout = sum(1 for r in results if r["eval"]["best_setup"] == "Breakout")
    n_pullback = sum(1 for r in results if r["eval"]["best_setup"] == "Trend Pullback")
    n_squeeze = sum(1 for r in results if r["eval"]["best_setup"] == "Volatility Squeeze")
    n_structure = sum(1 for r in results if r["eval"]["best_setup"] == "Structure Break")
    return f"""
    <div class="stat"><div class="n">{n_flagged}</div><div class="l">Flagged</div></div>
    <div class="stat"><div class="n">{n_breakout}</div><div class="l">Breakouts</div></div>
    <div class="stat"><div class="n">{n_pullback}</div><div class="l">Trend Pullbacks</div></div>
    <div class="stat"><div class="n">{n_squeeze}</div><div class="l">Volatility Squeezes</div></div>
    <div class="stat"><div class="n">{n_structure}</div><div class="l">Structure Breaks</div></div>"""


def table_rows_html(results: list, clickable: bool = False) -> str:
    if not results:
        return '<tr><td colspan="10" style="text-align:center;color:var(--muted);padding:24px;">No setups flagged this run.</td></tr>'
    return "".join(row_html(r, clickable=clickable) for r in results)


STYLE_CSS = """
  :root {
    color-scheme: light;
    --surface: #fcfcfb;
    --page: #f9f9f7;
    --text-primary: #0b0b0b;
    --text-secondary: #52514e;
    --muted: #898781;
    --gridline: #e1e0d9;
    --border: rgba(11,11,11,0.10);
    --good: #0ca30c;
    --bad: #d03b3b;
    --muted-line: #898781;
    --seq-500: #256abf;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --surface: #1a1a19;
      --page: #0d0d0d;
      --text-primary: #ffffff;
      --text-secondary: #c3c2b7;
      --muted: #898781;
      --gridline: #2c2c2a;
      --border: rgba(255,255,255,0.10);
      --good: #0ca30c;
      --bad: #e66767;
      --muted-line: #898781;
      --seq-500: #3987e5;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--page); color: var(--text-primary);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 1180px; margin: 0 auto; padding: 28px 20px 60px; }
  header { margin-bottom: 18px; display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .meta { color: var(--text-secondary); font-size: 13px; }
  .disclaimer {
    margin-top: 10px; padding: 10px 14px; border-radius: 8px;
    background: var(--surface); border: 1px solid var(--border);
    color: var(--text-secondary); font-size: 12.5px;
  }
  .stats { display: flex; gap: 10px; margin: 16px 0 20px; flex-wrap: wrap; }
  .stat {
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 10px 16px; min-width: 110px;
  }
  .stat .n { font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .stat .l { color: var(--text-secondary); font-size: 12px; }
  table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: 10px; overflow: hidden; }
  th, td { padding: 9px 10px; border-bottom: 1px solid var(--gridline); text-align: left; vertical-align: middle; }
  th {
    color: var(--muted); font-weight: 500; font-size: 12px; text-transform: uppercase;
    letter-spacing: 0.03em; position: sticky; top: 0; background: var(--surface);
  }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  td.ticker { font-weight: 600; }
  .pos { color: var(--good); }
  .neg { color: var(--bad); }
  .muted-text { color: var(--muted); }
  .structure-event { font-size: 11px; color: var(--muted); white-space: nowrap; }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px; font-size: 11.5px;
    margin: 1px 3px 1px 0; border: 1px solid var(--badge-light);
    color: var(--badge-light); white-space: nowrap;
  }
  :root:not([data-theme="light"]) .badge { color: var(--badge-dark); border-color: var(--badge-dark); }
  td.reason {
    color: var(--text-secondary); font-size: 12.5px; max-width: 280px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: help;
  }
  td.score { white-space: nowrap; min-width: 130px; }
  .scorebar-track {
    display: inline-block; width: 70px; height: 6px; background: var(--gridline);
    border-radius: 3px; overflow: hidden; vertical-align: middle;
  }
  .scorebar-fill { height: 100%; background: var(--seq-500); }
  .scorebar-num { margin-left: 6px; font-variant-numeric: tabular-nums; font-weight: 600; font-size: 12.5px; }
  .spark { display: block; }
  tbody tr:hover { background: var(--page); }
  footer { margin-top: 16px; color: var(--muted); font-size: 12px; }
"""


def _short_universe_label(universe_label: str) -> str:
    if "," in universe_label:
        n = len([t for t in universe_label.split(",") if t.strip()])
        return f"custom list ({n} tickers)"
    return universe_label


def render_html(results, out_path: Path, universe_label: str):
    universe_label = _short_universe_label(universe_label)
    generated = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Stock Setup Scanner</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>{STYLE_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Stock Setup Scanner</h1>
      <div class="meta">Universe: {universe_label} &middot; Generated {generated}</div>
    </div>
  </header>
  <div class="disclaimer">
    Technical pattern screen only — not investment advice. Flags are based on price/volume rules
    (trend, momentum, volatility), not fundamentals, news, or risk profile. Verify independently
    before acting, and always use a stop-loss / position size that fits your own risk tolerance.
  </div>

  <div class="stats">{stats_html(results)}</div>

  <table>
    <thead>
      <tr>
        <th>Ticker</th><th>Price</th><th>1d</th><th>60d trend</th><th>RSI14</th>
        <th>Vol/avg</th><th>Structure</th><th>Setup</th><th>Score</th><th>Why</th>
      </tr>
    </thead>
    <tbody>{table_rows_html(results)}</tbody>
  </table>
  <footer>Re-run the scanner to refresh, or use app.py for a live, clickable dashboard.</footer>
</div>
</body>
</html>"""
    out_path.write_text(html)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", default="sp500", help="sp500 | nasdaq100 | sp500+nasdaq100 | path/to/file.txt | AAPL,MSFT,...")
    ap.add_argument("--period", default="1y", help="yfinance period for history (default 1y)")
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--min-avg-volume", type=float, default=300_000)
    ap.add_argument("--top", type=int, default=40, help="max rows to show in console/dashboard")
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser when done")
    args = ap.parse_args()

    results = run_scan(args.universe, period=args.period, min_price=args.min_price, min_avg_volume=args.min_avg_volume)
    top = results[: args.top]

    print(f"\n{'TICKER':<8}{'PRICE':>10}{'SETUP':<22}{'SCORE':>7}   WHY")
    for r in top:
        f, ev = r["features"], r["eval"]
        best = ev["setups"][ev["best_setup"]]
        why = best["reasons"][0] if best["reasons"] else ""
        print(f"{r['ticker']:<8}{f['close']:>10.2f}  {ev['best_setup']:<20}{ev['best_score']:>7.1f}   {why}")

    out_path = Path(args.out)
    render_html(top, out_path, universe_label=args.universe)
    print(f"\nDashboard written to {out_path.resolve()}")
    if args.open:
        webbrowser.open(f"file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
