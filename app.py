#!/usr/bin/env python3
"""Live, interactive Stock Setup Scanner: a Refresh button, click-through stock
detail (full indicator/setup breakdown + recent news), and a TradingView link.

Local dev: python3 app.py --universe sp500, then open http://127.0.0.1:5050
(the built-in Flask dev server is fine for this, single-user, loopback only).

Production (e.g. Railway): served by a WSGI server that imports this module -
`gunicorn --workers 1 --threads 4 --timeout 120 -b 0.0.0.0:$PORT app:app`.
Single worker is intentional: state lives in an in-memory dict (STATE) shared
via a lock, not a database, so only one process can hold it. Threads let that
one process still serve concurrent requests without blocking behind a scan.
Config comes from env vars in this mode (SCANNER_UNIVERSE, SCANNER_PERIOD,
SCANNER_MIN_PRICE, SCANNER_MIN_AVG_VOLUME, SCANNER_TOP,
SCANNER_AUTO_REFRESH_SECONDS) since there's no CLI invocation to pass args to.
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from flask import Flask, jsonify, request, Response

sys.path.insert(0, str(Path(__file__).parent))
from analysis import compute_indicators, compute_risk_levels, evaluate, SETUPS  # noqa: E402
from scan import (  # noqa: E402
    STYLE_CSS, SETUP_COLORS, run_scan, stats_html, table_rows_html,
    _structure_cell, flatten_ticker_columns,
)

app = Flask(__name__)

STATE = {
    "results": [],
    "by_ticker": {},
    "generated": None,
    "universe": "sp500",
    "top": 40,
    "period": "1y",
    "min_price": 5.0,
    "min_avg_volume": 300_000,
    "scanning": False,
    "error": None,
}
LOCK = threading.Lock()

ET = ZoneInfo("America/New_York")
SCAN_COOLDOWN_SECONDS = 20  # public anti-abuse: ignore rapid repeated Refresh clicks
AUTO_REFRESH_SECONDS = int(os.environ.get("SCANNER_AUTO_REFRESH_SECONDS", "1200"))  # 20 min


def _market_is_open(now=None) -> bool:
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def _do_scan(universe, period, min_price, min_avg_volume, top):
    with LOCK:
        STATE["scanning"] = True
        STATE["error"] = None
    try:
        results = run_scan(universe, period=period, min_price=min_price, min_avg_volume=min_avg_volume)
        with LOCK:
            STATE["results"] = results[:top]
            STATE["by_ticker"] = {r["ticker"]: r for r in results}  # keep all, not just top, for detail lookups
            STATE["generated"] = datetime.now(timezone.utc).astimezone()
            STATE["universe"], STATE["period"] = universe, period
            STATE["min_price"], STATE["min_avg_volume"], STATE["top"] = min_price, min_avg_volume, top
    except Exception as e:
        with LOCK:
            STATE["error"] = str(e)
    finally:
        with LOCK:
            STATE["scanning"] = False


def _generated_label():
    return STATE["generated"].strftime("%Y-%m-%d %H:%M %Z") if STATE["generated"] else "never (click Refresh)"


def _auto_refresh_loop():
    """Keep the public page fresh on its own during market hours, so visitors
    aren't relying on someone clicking Refresh. Runs forever in a daemon thread."""
    while True:
        time.sleep(AUTO_REFRESH_SECONDS)
        with LOCK:
            busy = STATE["scanning"]
            args = (STATE["universe"], STATE["period"], STATE["min_price"], STATE["min_avg_volume"], STATE["top"])
        if not busy and _market_is_open():
            _do_scan(*args)


def _bootstrap():
    """Configure STATE from env vars and start the initial scan + auto-refresh
    loop. Runs at import time so this also works under a production server
    (`gunicorn app:app`), which imports this module directly and never calls
    main() / the `python3 app.py` CLI path below."""
    universe = os.environ.get("SCANNER_UNIVERSE", STATE["universe"])
    period = os.environ.get("SCANNER_PERIOD", STATE["period"])
    min_price = float(os.environ.get("SCANNER_MIN_PRICE", STATE["min_price"]))
    min_avg_volume = float(os.environ.get("SCANNER_MIN_AVG_VOLUME", STATE["min_avg_volume"]))
    top = int(os.environ.get("SCANNER_TOP", STATE["top"]))
    STATE.update(universe=universe, period=period, min_price=min_price, min_avg_volume=min_avg_volume, top=top)
    threading.Thread(target=_do_scan, args=(universe, period, min_price, min_avg_volume, top), daemon=True).start()
    threading.Thread(target=_auto_refresh_loop, daemon=True).start()


if __name__ != "__main__":
    _bootstrap()


def _json_safe(obj):
    """NaN/Infinity aren't valid JSON - jsonify emits them anyway (Python json
    extension), which breaks strict browser JSON.parse. Swap them for null."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def _tradingview_url(ticker: str) -> str:
    return f"https://www.tradingview.com/symbols/{ticker.upper()}/"


def fetch_news(ticker: str, limit: int = 6):
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception:
        return []
    items = []
    for entry in raw[:limit]:
        c = entry.get("content", entry)  # newer yfinance nests under "content"
        title = c.get("title")
        if not title:
            continue
        link = (c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or ""
        publisher = (c.get("provider") or {}).get("displayName", "")
        pub_date = c.get("pubDate") or c.get("displayTime") or ""
        items.append({"title": title, "link": link, "publisher": publisher, "published": pub_date})
    return items


def _build_detail(result: dict):
    f, ev = result["features"], result["eval"]
    setups_out = {}
    for name in SETUPS:
        data = ev["setups"].get(name, {"score": 0, "reasons": []})
        setups_out[name] = {
            "score": data["score"],
            "reasons": data["reasons"],
            "color": SETUP_COLORS.get(name),
        }
    struct = f["structure"]
    return {
        "ticker": result["ticker"],
        "price": f["close"],
        "change_1d_pct": f["change_1d_pct"],
        "change_5d_pct": f["change_5d_pct"],
        "best_setup": ev["best_setup"],
        "best_score": ev["best_score"],
        "setups": setups_out,
        "structure": struct,
        "structure_html": _structure_cell(struct),
        "indicators": {
            "sma20": f["sma20"], "sma50": f["sma50"], "sma200": f["sma200"],
            "rsi14": f["rsi14"], "atr_pct": f["atr_pct"], "bb_width_rank": f["bb_width_rank"],
            "vol_ratio": f["vol_ratio"], "vol20avg": f["vol20avg"],
            "high20": f["high20"], "high52": f["high52"],
            "pct_from_sma20": f["pct_from_sma20"],
        },
        "chart": {
            "dates": f["close_dates"],
            "open": f["open_series"],
            "high": f["high_series"],
            "low": f["low_series"],
            "close": f["close_series"],
        },
        "risk_levels": compute_risk_levels(f),
        "tradingview_url": _tradingview_url(result["ticker"]),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(PAGE_HTML, mimetype="text/html")


@app.route("/api/state")
def api_state():
    with LOCK:
        return jsonify({
            "scanning": STATE["scanning"],
            "error": STATE["error"],
            "universe": STATE["universe"],
            "generated_label": _generated_label(),
            "stats_html": stats_html(STATE["results"]),
            "rows_html": table_rows_html(STATE["results"], clickable=True),
        })


@app.route("/api/scan", methods=["POST"])
def api_scan():
    # Public-facing endpoint: deliberately ignores any client-supplied universe/
    # params (that's a server-side config decision, not a visitor's to make) and
    # rate-limits so repeated clicks can't hammer Yahoo Finance or this process.
    with LOCK:
        busy = STATE["scanning"]
        last = STATE["generated"]
        args = (STATE["universe"], STATE["period"], STATE["min_price"], STATE["min_avg_volume"], STATE["top"])
    if busy:
        return api_state()
    if last is not None:
        elapsed = (datetime.now(timezone.utc) - last).total_seconds()
        if elapsed < SCAN_COOLDOWN_SECONDS:
            return api_state()
    _do_scan(*args)
    return api_state()


@app.route("/api/stock/<ticker>")
def api_stock(ticker):
    ticker = ticker.upper().strip()
    with LOCK:
        cached = STATE["by_ticker"].get(ticker)
    if cached is None:
        try:
            df = yf.download(ticker, period=STATE["period"], interval="1d", progress=False, auto_adjust=True)
            df = flatten_ticker_columns(df, ticker)
        except Exception as e:
            return jsonify({"error": f"Could not fetch {ticker}: {e}"}), 502
        feats = compute_indicators(df) if not df.empty else None
        if feats is None:
            return jsonify({"error": f"Not enough price history for {ticker}"}), 404
        cached = {"ticker": ticker, "features": feats, "eval": evaluate(feats)}

    detail = _build_detail(cached)
    detail["news"] = fetch_news(ticker)
    return jsonify(_json_safe(detail))


PAGE_HTML = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Stock Setup Scanner</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
{STYLE_CSS}
  :root {{
    --bg-blob-1: rgba(42,120,214,0.10);
    --bg-blob-2: rgba(27,175,122,0.08);
    --bg-blob-3: rgba(74,58,167,0.07);
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --bg-blob-1: rgba(57,135,229,0.14);
      --bg-blob-2: rgba(25,158,112,0.12);
      --bg-blob-3: rgba(144,133,233,0.12);
    }}
  }}
  :root[data-theme="dark"] {{
    --bg-blob-1: rgba(57,135,229,0.14);
    --bg-blob-2: rgba(25,158,112,0.12);
    --bg-blob-3: rgba(144,133,233,0.12);
  }}
  .bg-anim {{
    position: fixed; inset: -10%; z-index: -1; pointer-events: none;
    background:
      radial-gradient(circle at 18% 22%, var(--bg-blob-1), transparent 42%),
      radial-gradient(circle at 82% 28%, var(--bg-blob-2), transparent 42%),
      radial-gradient(circle at 50% 85%, var(--bg-blob-3), transparent 45%);
    filter: blur(50px);
    animation: bgDrift 32s ease-in-out infinite alternate;
  }}
  @keyframes bgDrift {{
    0%   {{ transform: translate(0, 0) scale(1); }}
    50%  {{ transform: translate(2%, -3%) scale(1.06); }}
    100% {{ transform: translate(-3%, 2%) scale(1); }}
  }}
  @media (prefers-reduced-motion: reduce) {{
    .bg-anim {{ animation: none; }}
  }}
  .tabs {{
    display: flex; gap: 4px; margin: 0 0 14px; flex-wrap: wrap;
    border-bottom: 1px solid var(--gridline);
  }}
  .tab-btn {{
    padding: 8px 14px; border: none; background: none; color: var(--text-secondary);
    font-size: 13px; font-weight: 600; cursor: pointer; border-bottom: 2px solid transparent;
    margin-bottom: -1px; font-family: inherit;
  }}
  .tab-btn:hover {{ color: var(--text-primary); }}
  .tab-btn.active {{ color: var(--text-primary); border-bottom-color: var(--seq-500); }}
  .tab-count {{ color: var(--muted); font-weight: 500; margin-left: 4px; }}
  .refresh-btn {{
    display: inline-flex; align-items: center; gap: 6px; padding: 8px 16px;
    border-radius: 8px; border: 1px solid var(--border); background: var(--surface);
    color: var(--text-primary); font-size: 13px; font-weight: 600; cursor: pointer;
  }}
  .refresh-btn:hover {{ background: var(--page); }}
  .refresh-btn:disabled {{ opacity: 0.6; cursor: default; }}
  .refresh-btn .spin {{
    width: 12px; height: 12px; border-radius: 50%; border: 2px solid var(--border);
    border-top-color: var(--text-secondary); display: none; animation: spin 0.7s linear infinite;
  }}
  .refresh-btn.loading .spin {{ display: inline-block; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  tr.clickable {{ cursor: pointer; }}
  th[data-sort] {{ cursor: pointer; user-select: none; }}
  th[data-sort]:hover {{ color: var(--text-secondary); }}
  th.sort-asc::after {{ content: " \\25B2"; font-size: 9px; }}
  th.sort-desc::after {{ content: " \\25BC"; font-size: 9px; }}
  .error-banner {{
    display: none; margin: 10px 0; padding: 10px 14px; border-radius: 8px;
    background: rgba(208,59,59,0.12); border: 1px solid var(--bad); color: var(--bad); font-size: 13px;
  }}

  .modal-overlay {{
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.55);
    align-items: center; justify-content: center; padding: 24px; z-index: 50;
  }}
  .modal-overlay.open {{ display: flex; }}
  .modal {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
    max-width: 640px; width: 100%; max-height: 88vh; overflow-y: auto; padding: 22px 24px;
  }}
  .modal-head {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }}
  .modal-head h2 {{ margin: 0; font-size: 22px; }}
  .modal-price {{ font-size: 14px; color: var(--text-secondary); margin-top: 2px; }}
  .modal-close {{
    background: none; border: none; color: var(--muted); font-size: 20px; cursor: pointer;
    line-height: 1; padding: 4px;
  }}
  .tv-btn {{
    display: inline-flex; align-items: center; gap: 6px; padding: 7px 14px;
    border-radius: 8px; background: var(--seq-500); color: #fff; font-size: 13px; font-weight: 600;
    text-decoration: none;
  }}
  .modal-controls {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 10px; }}
  .chart-toggle {{ display: inline-flex; gap: 4px; }}
  .toggle-btn {{
    padding: 6px 12px; border-radius: 8px; border: 1px solid var(--border); background: var(--page);
    color: var(--text-secondary); font-size: 12.5px; font-weight: 600; cursor: pointer;
  }}
  .toggle-btn.active {{ background: var(--seq-500); color: #fff; border-color: var(--seq-500); }}
  .chart-svg {{ display: block; width: 100%; height: auto; }}
  .chart-note {{ font-size: 11px; color: var(--muted); margin-top: 6px; }}
  .modal-section {{ margin-top: 18px; }}
  .modal-section h3 {{
    font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
    margin: 0 0 8px;
  }}
  .setup-row {{
    display: flex; justify-content: space-between; gap: 10px; padding: 8px 0;
    border-bottom: 1px solid var(--gridline); font-size: 13px;
  }}
  .setup-row:last-child {{ border-bottom: none; }}
  .setup-name {{ font-weight: 600; }}
  .setup-reasons {{ color: var(--text-secondary); font-size: 12.5px; margin-top: 2px; }}
  .ind-grid {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 10px;
  }}
  .ind-tile {{ background: var(--page); border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }}
  .ind-tile .l {{ font-size: 11px; color: var(--muted); text-transform: uppercase; }}
  .ind-tile .v {{ font-size: 15px; font-weight: 600; font-variant-numeric: tabular-nums; margin-top: 2px; }}
  .news-item {{ padding: 9px 0; border-bottom: 1px solid var(--gridline); }}
  .news-item:last-child {{ border-bottom: none; }}
  .news-item a {{ color: var(--text-primary); text-decoration: none; font-size: 13.5px; font-weight: 500; }}
  .news-item a:hover {{ text-decoration: underline; }}
  .news-meta {{ color: var(--muted); font-size: 11.5px; margin-top: 2px; }}
  .modal-loading, .modal-empty {{ color: var(--muted); font-size: 13px; padding: 8px 0; }}
  .sizing-inputs {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 10px; }}
  .sizing-inputs label {{
    display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--muted);
    text-transform: uppercase; letter-spacing: 0.03em;
  }}
  .sizing-inputs input {{
    padding: 7px 9px; border-radius: 8px; border: 1px solid var(--border); background: var(--page);
    color: var(--text-primary); font-size: 14px; width: 130px; font-variant-numeric: tabular-nums;
  }}
  .sizing-inputs input:focus {{ outline: 2px solid var(--seq-500); outline-offset: 1px; }}
</style>
</head>
<body>
<div class="bg-anim" aria-hidden="true"></div>
<div class="wrap">
  <header>
    <div>
      <h1>Stock Setup Scanner</h1>
      <div class="meta">Universe: <span id="universe-label"></span> &middot; Generated <span id="generated-label"></span></div>
    </div>
    <button class="refresh-btn" id="refresh-btn" onclick="refresh()">
      <span class="spin"></span><span id="refresh-label">Refresh</span>
    </button>
  </header>
  <div class="disclaimer">
    Technical pattern screen only — not investment advice. Flags are based on price/volume rules
    (trend, momentum, volatility), not fundamentals, news, or risk profile. Verify independently
    before acting, and always use a stop-loss / position size that fits your own risk tolerance.
  </div>
  <div class="error-banner" id="error-banner"></div>

  <div class="stats" id="stats"></div>

  <div class="tabs" id="setup-tabs">
    <button class="tab-btn active" data-tab="All" onclick="setTab('All')">All <span class="tab-count">0</span></button>
    <button class="tab-btn" data-tab="Trend Pullback" onclick="setTab('Trend Pullback')">Trend Pullback <span class="tab-count">0</span></button>
    <button class="tab-btn" data-tab="Breakout" onclick="setTab('Breakout')">Breakout <span class="tab-count">0</span></button>
    <button class="tab-btn" data-tab="Volatility Squeeze" onclick="setTab('Volatility Squeeze')">Volatility Squeeze <span class="tab-count">0</span></button>
    <button class="tab-btn" data-tab="Structure Break" onclick="setTab('Structure Break')">Structure Break <span class="tab-count">0</span></button>
  </div>

  <table>
    <thead>
      <tr>
        <th data-sort="ticker">Ticker</th><th data-sort="price">Price</th><th data-sort="chg1d">1d</th>
        <th>60d trend</th><th data-sort="rsi">RSI14</th><th data-sort="volratio">Vol/avg</th>
        <th data-sort="structure">Structure</th><th data-sort="setup">Setup</th>
        <th data-sort="score">Score</th><th>Why</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>
  <footer>Click any row for full detail, news, and a TradingView link. Refresh re-scans live.</footer>
</div>

<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this) closeModal()">
  <div class="modal" id="modal"></div>
</div>

<script>
function fmt(n, d) {{ return (n === null || n === undefined || Number.isNaN(n)) ? "—" : n.toFixed(d); }}

async function loadState() {{
  const res = await fetch('/api/state');
  const data = await res.json();
  applyState(data);
  // If a scan is already running (e.g. this process just cold-started), poll
  // until it finishes so visitors see real data without clicking Refresh.
  if (data.scanning) {{
    setTimeout(loadState, 4000);
  }}
}}

function applyState(data) {{
  document.getElementById('universe-label').textContent = data.universe;
  document.getElementById('generated-label').textContent = data.generated_label;
  document.getElementById('stats').innerHTML = data.stats_html;
  document.getElementById('rows').innerHTML = data.rows_html;
  const banner = document.getElementById('error-banner');
  if (data.error) {{ banner.style.display = 'block'; banner.textContent = data.error; }}
  else {{ banner.style.display = 'none'; }}
  document.querySelectorAll('tr.clickable').forEach(tr => {{
    tr.addEventListener('click', () => openDetail(tr.dataset.ticker));
  }});
  applySort();
  applyTabFilter();
}}

let activeTab = 'All';
const TAB_NAMES = ['Trend Pullback', 'Breakout', 'Volatility Squeeze', 'Structure Break'];

function setTab(name) {{
  activeTab = name;
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.toggle('active', btn.dataset.tab === name));
  applyTabFilter();
}}

function applyTabFilter() {{
  const tbody = document.getElementById('rows');
  const rows = Array.from(tbody.querySelectorAll('tr[data-ticker]'));
  const counts = {{ All: rows.length }};
  TAB_NAMES.forEach(name => {{ counts[name] = 0; }});
  let visibleCount = 0;
  rows.forEach(tr => {{
    const setup = tr.dataset.setup;
    if (counts[setup] !== undefined) counts[setup]++;
    const show = activeTab === 'All' || setup === activeTab;
    tr.style.display = show ? '' : 'none';
    if (show) visibleCount++;
  }});

  let emptyRow = document.getElementById('tab-empty-row');
  if (rows.length > 0 && visibleCount === 0) {{
    if (!emptyRow) {{
      emptyRow = document.createElement('tr');
      emptyRow.id = 'tab-empty-row';
      emptyRow.innerHTML = '<td colspan="10" style="text-align:center;color:var(--muted);padding:24px;">' +
        'No stocks currently flagged for this setup.</td>';
      tbody.appendChild(emptyRow);
    }}
    emptyRow.style.display = '';
  }} else if (emptyRow) {{
    emptyRow.style.display = 'none';
  }}

  document.querySelectorAll('.tab-btn').forEach(btn => {{
    const c = btn.dataset.tab === 'All' ? counts.All : (counts[btn.dataset.tab] || 0);
    btn.querySelector('.tab-count').textContent = c;
  }});
}}

let sortKey = null, sortDir = 1;
const STRING_SORT_KEYS = new Set(['ticker', 'setup']);

function applySort() {{
  if (!sortKey) return;
  const tbody = document.getElementById('rows');
  const rows = Array.from(tbody.querySelectorAll('tr[data-ticker]'));
  if (!rows.length) return;
  const isString = STRING_SORT_KEYS.has(sortKey);
  rows.sort((a, b) => {{
    const va = a.dataset[sortKey], vb = b.dataset[sortKey];
    if (isString) return va.localeCompare(vb) * sortDir;
    return (parseFloat(va) - parseFloat(vb)) * sortDir;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}

document.querySelectorAll('th[data-sort]').forEach(th => {{
  th.addEventListener('click', () => {{
    const key = th.dataset.sort;
    if (sortKey === key) {{ sortDir *= -1; }} else {{ sortKey = key; sortDir = 1; }}
    document.querySelectorAll('th[data-sort]').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
    th.classList.add(sortDir === 1 ? 'sort-asc' : 'sort-desc');
    applySort();
  }});
}});

async function refresh() {{
  const btn = document.getElementById('refresh-btn');
  btn.classList.add('loading');
  btn.disabled = true;
  document.getElementById('refresh-label').textContent = 'Scanning…';
  try {{
    const res = await fetch('/api/scan', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{}})
    }});
    applyState(await res.json());
  }} catch (e) {{
    const banner = document.getElementById('error-banner');
    banner.style.display = 'block'; banner.textContent = 'Refresh failed: ' + e;
  }} finally {{
    btn.classList.remove('loading');
    btn.disabled = false;
    document.getElementById('refresh-label').textContent = 'Refresh';
  }}
}}

const SETUP_ORDER = ["Trend Pullback", "Breakout", "Volatility Squeeze", "Structure Break"];

function renderModal(d) {{
  const chgClass = d.change_1d_pct >= 0 ? 'pos' : 'neg';
  let setupsHtml = SETUP_ORDER.map(name => {{
    const s = d.setups[name];
    if (!s || s.score <= 0) return '';
    const color = s.color ? s.color[0] : '#888';
    return `<div class="setup-row">
      <div style="flex:1">
        <div class="setup-name" style="color:${{color}}">${{name}}</div>
        <div class="setup-reasons">${{s.reasons.join(' · ')}}</div>
      </div>
      <div style="font-weight:700;white-space:nowrap">${{s.score.toFixed(0)}}</div>
    </div>`;
  }}).join('') || '<div class="modal-empty">No setups currently triggered for this ticker.</div>';

  const ind = d.indicators;
  const indTiles = [
    ['SMA20', '$' + fmt(ind.sma20, 2)], ['SMA50', '$' + fmt(ind.sma50, 2)], ['SMA200', '$' + fmt(ind.sma200, 2)],
    ['RSI14', fmt(ind.rsi14, 0)], ['ATR%', fmt(ind.atr_pct, 1) + '%'], ['Vol/avg', fmt(ind.vol_ratio, 1) + 'x'],
    ['BB width pct-rank', fmt(ind.bb_width_rank, 2)], ['20d high', '$' + fmt(ind.high20, 2)], ['52w high', '$' + fmt(ind.high52, 2)],
  ].map(([l, v]) => `<div class="ind-tile"><div class="l">${{l}}</div><div class="v">${{v}}</div></div>`).join('');

  const rl = d.risk_levels;
  const riskTiles = rl ? [
    ['Entry ref.', '$' + fmt(d.price, 2), ''],
    ['Stop-loss', '$' + fmt(rl.stop_price, 2), '-' + fmt(rl.stop_pct, 1) + '% risk'],
    ['Target (2R)', '$' + fmt(rl.target_2r, 2), '+' + fmt(rl.target_2r_pct, 1) + '%'],
    ['Target (3R)', '$' + fmt(rl.target_3r, 2), '+' + fmt(rl.target_3r_pct, 1) + '%'],
  ].map(([l, v, sub]) => `<div class="ind-tile"><div class="l">${{l}}</div><div class="v">${{v}}</div>` +
    (sub ? `<div class="l" style="margin-top:2px">${{sub}}</div>` : '') + `</div>`).join('') : '';

  let newsHtml = '<div class="modal-empty">No recent news found.</div>';
  if (d.news && d.news.length) {{
    newsHtml = d.news.map(n => {{
      let when = '';
      if (n.published) {{ try {{ when = new Date(n.published).toLocaleString(); }} catch (e) {{}} }}
      return `<div class="news-item">
        <a href="${{n.link}}" target="_blank" rel="noopener">${{n.title}}</a>
        <div class="news-meta">${{n.publisher || ''}}${{when ? ' · ' + when : ''}}</div>
      </div>`;
    }}).join('');
  }}

  document.getElementById('modal').innerHTML = `
    <div class="modal-head">
      <div>
        <h2>${{d.ticker}}</h2>
        <div class="modal-price">$${{fmt(d.price, 2)}} <span class="${{chgClass}}">${{d.change_1d_pct >= 0 ? '+' : ''}}${{fmt(d.change_1d_pct, 1)}}% today</span> · 5d ${{d.change_5d_pct >= 0 ? '+' : ''}}${{fmt(d.change_5d_pct, 1)}}%</div>
        <div style="margin-top:6px">${{d.structure_html}}</div>
        <div class="modal-controls">
          <a class="tv-btn" href="${{d.tradingview_url}}" target="_blank" rel="noopener">Open on TradingView ↗</a>
          <div class="chart-toggle">
            <button class="toggle-btn" id="toggle-line" onclick="setChartMode('line')">Line</button>
            <button class="toggle-btn" id="toggle-candles" onclick="setChartMode('candles')">Candles</button>
          </div>
        </div>
      </div>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-section">
      <div id="chart-container"></div>
      <div class="chart-note">Dashed line: a naive linear projection of a smoothed 60-session trend, extended 15
      trading days ahead. It is a simple statistical extrapolation, not a forecast — prices can and do move very
      differently.</div>
    </div>
    <div class="modal-section">
      <h3>Setups</h3>
      ${{setupsHtml}}
    </div>
    <div class="modal-section">
      <h3>Indicators</h3>
      <div class="ind-grid">${{indTiles}}</div>
    </div>
    <div class="modal-section">
      <h3>Reference stop-loss / take-profit</h3>
      <div class="ind-grid">${{riskTiles}}</div>
      <div class="chart-note">${{rl ? rl.stop_method : ''}} · targets are simple 2x/3x risk-reward multiples of that
      stop distance. Fixed formula, applied the same way to every ticker — not personalized advice. Always size
      positions and set stops to fit your own risk tolerance.</div>
    </div>
    <div class="modal-section">
      <h3>Position sizing</h3>
      <div class="sizing-inputs">
        <label>Account size ($)<input type="number" id="acct-size" min="0" step="100"></label>
        <label>Risk per trade (%)<input type="number" id="acct-risk-pct" min="0.1" max="100" step="0.1"></label>
      </div>
      <div class="ind-grid" id="sizing-output"></div>
      <button class="toggle-btn" id="copy-trade-btn" onclick="copyTradeParams()" style="margin-top:10px">
        Copy trade parameters
      </button>
      <div class="chart-note">Copies symbol, share count, entry, stop, and targets to your clipboard so you can paste
      them into TradingView's (or your broker's) order ticket yourself — nothing is entered or submitted on any site
      automatically. Shares sized so a stop-out loses roughly your chosen % of account, using the stop-loss above.
      Saved only in your browser (localStorage) — never sent anywhere. Fixed arithmetic on numbers you enter, not
      personalized advice; round down for odd lots/broker minimums and verify before trading.</div>
    </div>
    <div class="modal-section">
      <h3>Recent news</h3>
      ${{newsHtml}}
    </div>
  `;
  currentChartData = d.chart;
  currentRiskLevels = d.risk_levels;
  currentEntryPrice = d.price;
  currentTicker = d.ticker;
  updateToggleButtons();
  initSizingInputs();
  renderChartInto(currentChartData);
}}

// --- Detail chart: line or candlesticks, with a naive linear trend projection ---
let chartMode = 'line';
let currentChartData = null;
let currentRiskLevels = null;
let currentEntryPrice = null;
let currentTicker = null;
let currentShares = 0;

// --- Position sizing (account risk %), saved locally in the browser only ---
function loadSizingPrefs() {{
  try {{
    return {{
      acct: localStorage.getItem('scannerAcctSize') || '10000',
      riskPct: localStorage.getItem('scannerRiskPct') || '1',
    }};
  }} catch (e) {{
    return {{ acct: '10000', riskPct: '1' }};
  }}
}}

function saveSizingPrefs(acct, riskPct) {{
  try {{
    localStorage.setItem('scannerAcctSize', acct);
    localStorage.setItem('scannerRiskPct', riskPct);
  }} catch (e) {{}}
}}

function initSizingInputs() {{
  const acctInput = document.getElementById('acct-size');
  const riskInput = document.getElementById('acct-risk-pct');
  if (!acctInput || !riskInput) return;
  const prefs = loadSizingPrefs();
  acctInput.value = prefs.acct;
  riskInput.value = prefs.riskPct;
  acctInput.addEventListener('input', computeSizing);
  riskInput.addEventListener('input', computeSizing);
  computeSizing();
}}

function computeSizing() {{
  const acctInput = document.getElementById('acct-size');
  const riskInput = document.getElementById('acct-risk-pct');
  const out = document.getElementById('sizing-output');
  if (!acctInput || !riskInput || !out) return;
  const acct = Math.max(0, parseFloat(acctInput.value) || 0);
  const riskPct = Math.max(0, parseFloat(riskInput.value) || 0);
  saveSizingPrefs(acct, riskPct);

  if (!currentRiskLevels || !currentEntryPrice) {{ out.innerHTML = ''; return; }}
  const riskPerShare = currentRiskLevels.risk_per_share;
  const riskAmount = acct * (riskPct / 100);
  const shares = riskPerShare > 0 ? Math.floor(riskAmount / riskPerShare) : 0;
  currentShares = shares;
  const positionValue = shares * currentEntryPrice;
  const pctOfAcct = acct > 0 ? (positionValue / acct * 100) : 0;

  const pctStyle = pctOfAcct > 100 ? ' style="color:var(--bad)"' : '';
  const pctNote = pctOfAcct > 100
    ? '<div class="l" style="margin-top:2px;color:var(--bad)">exceeds account size</div>' : '';
  out.innerHTML = [
    ['Shares to buy', shares.toLocaleString()],
    ['$ at risk', '$' + riskAmount.toFixed(2)],
    ['Position value', '$' + positionValue.toFixed(2)],
  ].map(([l, v]) => `<div class="ind-tile"><div class="l">${{l}}</div><div class="v">${{v}}</div></div>`).join('')
    + `<div class="ind-tile"><div class="l">% of account</div><div class="v"${{pctStyle}}>${{fmt(pctOfAcct, 1)}}%</div>${{pctNote}}</div>`;
}}

function legacyCopyFallback(text) {{
  // Async Clipboard API can be blocked (permissions policy, insecure context, older
  // browsers) - fall back to a hidden textarea + execCommand, which works much more
  // broadly despite being deprecated. If even that fails, the caller shows the text
  // to the user directly so they can copy it by hand.
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {{ ok = document.execCommand('copy'); }} catch (e) {{ ok = false; }}
  document.body.removeChild(ta);
  return ok;
}}

async function copyTradeParams() {{
  const btn = document.getElementById('copy-trade-btn');
  if (!currentTicker || !currentRiskLevels || !currentEntryPrice) return;
  const rl = currentRiskLevels;
  const text = `${{currentTicker}}  qty=${{currentShares}}  entry=$${{currentEntryPrice.toFixed(2)}}  ` +
    `stop=$${{rl.stop_price.toFixed(2)}}  target1(2R)=$${{rl.target_2r.toFixed(2)}}  target2(3R)=$${{rl.target_3r.toFixed(2)}}`;
  const showResult = ok => {{
    if (!btn) return;
    const original = 'Copy trade parameters';
    btn.textContent = ok ? 'Copied ✓' : 'Copy failed — select manually';
    setTimeout(() => {{ btn.textContent = original; }}, 1500);
  }};

  let ok = false;
  try {{
    await navigator.clipboard.writeText(text);
    ok = true;
  }} catch (e) {{
    ok = legacyCopyFallback(text);
  }}
  showResult(ok);
  if (!ok) {{
    window.prompt('Copy failed automatically — copy this manually:', text);
  }}
}}

function setChartMode(mode) {{
  chartMode = mode;
  updateToggleButtons();
  if (currentChartData) renderChartInto(currentChartData);
}}

function updateToggleButtons() {{
  const lineBtn = document.getElementById('toggle-line');
  const candleBtn = document.getElementById('toggle-candles');
  if (!lineBtn || !candleBtn) return;
  lineBtn.classList.toggle('active', chartMode === 'line');
  candleBtn.classList.toggle('active', chartMode === 'candles');
}}

function linearRegression(values) {{
  const n = values.length;
  let sumX = 0, sumY = 0, sumXY = 0, sumXX = 0;
  for (let i = 0; i < n; i++) {{
    sumX += i; sumY += values[i]; sumXY += i * values[i]; sumXX += i * i;
  }}
  const denom = (n * sumXX - sumX * sumX) || 1;
  const slope = (n * sumXY - sumX * sumY) / denom;
  const intercept = (sumY - slope * sumX) / n;
  return {{ slope, intercept }};
}}

function addTradingDays(dateStr, n) {{
  const d = new Date(dateStr + 'T00:00:00');
  let added = 0;
  while (added < n) {{
    d.setDate(d.getDate() + 1);
    if (d.getDay() !== 0 && d.getDay() !== 6) added++;
  }}
  return d.toISOString().slice(0, 10);
}}

function computeProjection(closes, dates, windowN, projN) {{
  const win = closes.slice(-windowN);
  // Fit the slope on a lightly smoothed version of the window, not raw daily
  // closes. A short window of raw closes often lands entirely inside a pullback
  // or consolidation (exactly what setups like Trend Pullback are detecting),
  // so its own slope can point the opposite way from the larger trend and from
  // the setup score. Smoothing plus a longer window makes the fitted line track
  // the dominant trend instead of that recent noise.
  const smoothN = 5;
  const smoothed = win.map((_, i) => {{
    const start = Math.max(0, i - smoothN + 1);
    const slice = win.slice(start, i + 1);
    return slice.reduce((a, b) => a + b, 0) / slice.length;
  }});
  const {{ slope, intercept }} = linearRegression(smoothed);
  const lastIdx = smoothed.length - 1;
  const lastClose = closes[closes.length - 1];
  const fittedLast = slope * lastIdx + intercept;
  const offset = lastClose - fittedLast;  // anchor the projection to the actual last close
  const values = [], projDates = [];
  let d = dates[dates.length - 1];
  for (let k = 1; k <= projN; k++) {{
    values.push(slope * (lastIdx + k) + intercept + offset);
    d = addTradingDays(d, 1);
    projDates.push(d);
  }}
  return {{ values, dates: projDates }};
}}

function buildAxisAndDivider(allDates, n, xAt, plotH, histN) {{
  const tickIdx = [...new Set([0, Math.floor((n - 1) * 0.25), Math.floor((n - 1) * 0.5),
                                Math.floor((n - 1) * 0.75), n - 1])];
  let svg = '';
  tickIdx.forEach(i => {{
    const xi = xAt(i);
    svg += `<line x1="${{xi.toFixed(1)}}" y1="0" x2="${{xi.toFixed(1)}}" y2="${{plotH}}" stroke="var(--gridline)" stroke-width="1"/>`;
    const anchor = i === 0 ? 'start' : i === n - 1 ? 'end' : 'middle';
    svg += `<text x="${{xi.toFixed(1)}}" y="${{plotH + 12}}" font-size="9" ` +
      `font-family="system-ui,-apple-system,sans-serif" fill="var(--muted)" text-anchor="${{anchor}}">` +
      `${{allDates[i].slice(5)}}</text>`;
  }});
  const bx = xAt(histN - 1);
  svg += `<line x1="${{bx.toFixed(1)}}" y1="0" x2="${{bx.toFixed(1)}}" y2="${{plotH}}" ` +
    `stroke="var(--muted)" stroke-width="1" stroke-dasharray="2,2"/>`;
  return svg;
}}

function buildRiskLinesSvg(riskLevels, yAt, width) {{
  if (!riskLevels) return '';
  const rows = [
    {{ price: riskLevels.stop_price, color: 'var(--bad)', label: 'Stop', dash: '3,2', opacity: 1 }},
    {{ price: riskLevels.target_2r, color: 'var(--good)', label: '2R', dash: '3,2', opacity: 1 }},
    {{ price: riskLevels.target_3r, color: 'var(--good)', label: '3R', dash: '2,3', opacity: 0.55 }},
  ];
  return rows.map(r => {{
    const y = yAt(r.price);
    return `<line x1="2" y1="${{y.toFixed(1)}}" x2="${{width - 2}}" y2="${{y.toFixed(1)}}" stroke="${{r.color}}" ` +
      `stroke-width="1" stroke-dasharray="${{r.dash}}" opacity="${{r.opacity}}"/>` +
      `<text x="${{width - 4}}" y="${{(y - 3).toFixed(1)}}" font-size="9" text-anchor="end" ` +
      `font-family="system-ui,-apple-system,sans-serif" fill="${{r.color}}" opacity="${{r.opacity}}">` +
      `${{r.label}} $${{r.price.toFixed(2)}}</text>`;
  }}).join('');
}}

function renderChartInto(chart) {{
  const container = document.getElementById('chart-container');
  if (!container) return;
  if (!chart || !chart.close || chart.close.length < 2) {{
    container.innerHTML = '<div class="modal-empty">Not enough history for a chart.</div>';
    return;
  }}
  container.innerHTML = chartMode === 'candles' ? buildCandleChart(chart) : buildLineChart(chart);
}}

function buildLineChart(chart) {{
  const width = 560, plotH = 140, axisH = 16;
  const closes = chart.close, dates = chart.dates;
  const histN = closes.length;
  const proj = computeProjection(closes, dates, 60, 15);
  const allValues = closes.concat(proj.values);
  const allDates = dates.concat(proj.dates);
  const n = allValues.length;
  let lo = Math.min(...allValues), hi = Math.max(...allValues);
  if (currentRiskLevels) {{
    lo = Math.min(lo, currentRiskLevels.stop_price);
    hi = Math.max(hi, currentRiskLevels.target_3r);
  }}
  const span = (hi - lo) || 1;
  const xAt = i => (i / (n - 1)) * (width - 4) + 2;
  const yAt = v => plotH - 2 - ((v - lo) / span) * (plotH - 4);

  const axisSvg = buildAxisAndDivider(allDates, n, xAt, plotH, histN);
  const riskSvg = buildRiskLinesSvg(currentRiskLevels, yAt, width);
  const histPts = closes.map((v, i) => `${{xAt(i).toFixed(1)}},${{yAt(v).toFixed(1)}}`).join(' ');
  const up = closes[closes.length - 1] >= closes[0];
  const color = up ? 'var(--good)' : 'var(--muted-line)';
  const projPts = [`${{xAt(histN - 1).toFixed(1)}},${{yAt(closes[histN - 1]).toFixed(1)}}`]
    .concat(proj.values.map((v, k) => `${{xAt(histN + k).toFixed(1)}},${{yAt(v).toFixed(1)}}`))
    .join(' ');

  return `<svg viewBox="0 0 ${{width}} ${{plotH + axisH}}" class="chart-svg" preserveAspectRatio="none">` +
    axisSvg + riskSvg +
    `<polyline points="${{histPts}}" fill="none" stroke="${{color}}" stroke-width="1.75" ` +
    `stroke-linejoin="round" stroke-linecap="round"/>` +
    `<polyline points="${{projPts}}" fill="none" stroke="var(--seq-500)" stroke-width="1.5" ` +
    `stroke-dasharray="4,3" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}}

function buildCandleChart(chart) {{
  const width = 560, plotH = 140, axisH = 16;
  const {{ open, high, low, close, dates }} = chart;
  const histN = close.length;
  const proj = computeProjection(close, dates, 60, 15);
  const allDates = dates.concat(proj.dates);
  const n = histN + proj.values.length;
  let lo = Math.min(Math.min(...low), Math.min(...proj.values));
  let hi = Math.max(Math.max(...high), Math.max(...proj.values));
  if (currentRiskLevels) {{
    lo = Math.min(lo, currentRiskLevels.stop_price);
    hi = Math.max(hi, currentRiskLevels.target_3r);
  }}
  const span = (hi - lo) || 1;
  const xAt = i => (i / (n - 1)) * (width - 4) + 2;
  const yAt = v => plotH - 2 - ((v - lo) / span) * (plotH - 4);
  const slotW = (width - 4) / (n - 1);
  const bodyW = Math.max(1, Math.min(6, slotW * 0.7));

  const axisSvg = buildAxisAndDivider(allDates, n, xAt, plotH, histN);
  const riskSvg = buildRiskLinesSvg(currentRiskLevels, yAt, width);
  let candles = '';
  for (let i = 0; i < histN; i++) {{
    const xi = xAt(i);
    const isUp = close[i] >= open[i];
    const color = isUp ? 'var(--good)' : 'var(--bad)';
    const yHigh = yAt(high[i]), yLow = yAt(low[i]);
    const yOpen = yAt(open[i]), yClose = yAt(close[i]);
    const bodyTop = Math.min(yOpen, yClose), bodyBot = Math.max(yOpen, yClose);
    candles += `<line x1="${{xi.toFixed(1)}}" y1="${{yHigh.toFixed(1)}}" x2="${{xi.toFixed(1)}}" y2="${{yLow.toFixed(1)}}" ` +
      `stroke="${{color}}" stroke-width="1"/>`;
    candles += `<rect x="${{(xi - bodyW / 2).toFixed(1)}}" y="${{bodyTop.toFixed(1)}}" width="${{bodyW.toFixed(1)}}" ` +
      `height="${{Math.max(0.6, bodyBot - bodyTop).toFixed(1)}}" fill="${{color}}"/>`;
  }}
  const projPts = [`${{xAt(histN - 1).toFixed(1)}},${{yAt(close[histN - 1]).toFixed(1)}}`]
    .concat(proj.values.map((v, k) => `${{xAt(histN + k).toFixed(1)}},${{yAt(v).toFixed(1)}}`))
    .join(' ');

  return `<svg viewBox="0 0 ${{width}} ${{plotH + axisH}}" class="chart-svg" preserveAspectRatio="none">` +
    axisSvg + riskSvg + candles +
    `<polyline points="${{projPts}}" fill="none" stroke="var(--seq-500)" stroke-width="1.5" ` +
    `stroke-dasharray="4,3" stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}}

async function openDetail(ticker) {{
  const overlay = document.getElementById('modal-overlay');
  document.getElementById('modal').innerHTML = '<div class="modal-loading">Loading ' + ticker + '…</div>';
  overlay.classList.add('open');
  try {{
    const res = await fetch('/api/stock/' + encodeURIComponent(ticker));
    const data = await res.json();
    if (data.error) {{
      document.getElementById('modal').innerHTML = '<div class="modal-loading">' + data.error + '</div>';
      return;
    }}
    renderModal(data);
  }} catch (e) {{
    document.getElementById('modal').innerHTML = '<div class="modal-loading">Failed to load ' + ticker + ': ' + e + '</div>';
  }}
}}

function closeModal() {{
  document.getElementById('modal-overlay').classList.remove('open');
}}
document.addEventListener('keydown', e => {{ if (e.key === 'Escape') closeModal(); }});

loadState();
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", default="sp500")
    ap.add_argument("--period", default="1y")
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--min-avg-volume", type=float, default=300_000)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--no-scan", action="store_true", help="don't run an initial scan on startup")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser once ready")
    args = ap.parse_args()

    STATE.update(universe=args.universe, period=args.period, min_price=args.min_price,
                 min_avg_volume=args.min_avg_volume, top=args.top)

    if not args.no_scan:
        threading.Thread(
            target=_do_scan,
            args=(args.universe, args.period, args.min_price, args.min_avg_volume, args.top),
            daemon=True,
        ).start()
        threading.Thread(target=_auto_refresh_loop, daemon=True).start()

    if args.open:
        threading.Timer(1.2, lambda: webbrowser.open(f"http://127.0.0.1:{args.port}")).start()

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()
