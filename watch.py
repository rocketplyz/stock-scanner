#!/usr/bin/env python3
"""Continuously re-scan the universe and alert on newly-flagged setups.

Example:
    python3 watch.py --universe sp500 --interval-min 20 --min-score 60
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from scan import run_scan, render_html

ET = ZoneInfo("America/New_York")


def market_is_open(now=None) -> bool:
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_t <= now <= close_t


def notify(title: str, message: str):
    if sys.platform == "darwin":
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=False)
    else:
        print(f"[ALERT] {title}: {message}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--universe", default="sp500")
    ap.add_argument("--period", default="1y")
    ap.add_argument("--min-price", type=float, default=5.0)
    ap.add_argument("--min-avg-volume", type=float, default=300_000)
    ap.add_argument("--min-score", type=float, default=60.0, help="only alert/list setups at or above this score")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--out", default="dashboard.html")
    ap.add_argument("--interval-min", type=float, default=20.0)
    ap.add_argument("--market-hours-only", action="store_true", default=True)
    ap.add_argument("--all-hours", dest="market_hours_only", action="store_false")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser on first run")
    args = ap.parse_args()

    seen = set()
    first = True
    print(f"Watching {args.universe} every {args.interval_min} min "
          f"({'market hours only' if args.market_hours_only else 'all hours'}). Ctrl+C to stop.")

    while True:
        if args.market_hours_only and not market_is_open():
            print(f"[{datetime.now(ET):%H:%M %Z}] market closed, sleeping...")
            time.sleep(min(args.interval_min * 60, 600))
            continue

        ts = datetime.now(ET).strftime("%H:%M:%S %Z")
        print(f"\n[{ts}] scanning {args.universe}...")
        try:
            results = run_scan(args.universe, period=args.period, min_price=args.min_price,
                                min_avg_volume=args.min_avg_volume)
        except Exception as e:
            print(f"  scan failed: {e}", file=sys.stderr)
            time.sleep(args.interval_min * 60)
            continue

        qualifying = [r for r in results if r["eval"]["best_score"] >= args.min_score]
        new_ones = [r for r in qualifying if r["ticker"] not in seen]
        seen = {r["ticker"] for r in qualifying}

        top = results[: args.top]
        render_html(top, Path(args.out), universe_label=args.universe)
        print(f"  {len(qualifying)} flagged >= {args.min_score}; {len(new_ones)} new this cycle; dashboard updated.")

        for r in new_ones:
            ev = r["eval"]
            best = ev["setups"][ev["best_setup"]]
            msg = f"{r['ticker']} @ ${r['features']['close']:.2f} — {ev['best_setup']} ({ev['best_score']:.0f}): {best['reasons'][0]}"
            print(f"  NEW: {msg}")
            notify("Stock Setup Flagged", msg)

        if first and args.open:
            webbrowser.open(f"file://{Path(args.out).resolve()}")
        first = False

        time.sleep(args.interval_min * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
