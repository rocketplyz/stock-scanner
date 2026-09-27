"""Ticker universe loading, with local caching so we don't hit Wikipedia/GitHub every run."""
import json
import time
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).parent / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL_SECONDS = 24 * 3600  # refresh constituent lists once a day
HTTP_TIMEOUT = 15  # seconds - without this, a slow/blocked host hangs forever (no default in requests)
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; stock-scanner/1.0)"}

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
NASDAQ100_WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
SP500_CSV_FALLBACK = (
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
)


def _get_html(url: str) -> str:
    resp = requests.get(url, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
    resp.raise_for_status()
    return resp.text


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _load_cache(name: str):
    p = _cache_path(name)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    if time.time() - data.get("ts", 0) > CACHE_TTL_SECONDS:
        return None
    return data.get("tickers")


def _save_cache(name: str, tickers):
    _cache_path(name).write_text(json.dumps({"ts": time.time(), "tickers": tickers}))


def _clean(tickers):
    # Yahoo uses '-' instead of '.' for share classes (e.g. BRK.B -> BRK-B)
    return sorted({t.strip().upper().replace(".", "-") for t in tickers if t and isinstance(t, str)})


def get_sp500():
    cached = _load_cache("sp500")
    if cached:
        return cached
    try:
        tables = pd.read_html(StringIO(_get_html(SP500_WIKI_URL)))
        tickers = _clean(tables[0]["Symbol"].tolist())
    except Exception:
        csv_text = _get_html(SP500_CSV_FALLBACK)
        df = pd.read_csv(StringIO(csv_text))
        col = "Symbol" if "Symbol" in df.columns else df.columns[0]
        tickers = _clean(df[col].tolist())
    _save_cache("sp500", tickers)
    return tickers


def get_nasdaq100():
    cached = _load_cache("nasdaq100")
    if cached:
        return cached
    tables = pd.read_html(StringIO(_get_html(NASDAQ100_WIKI_URL)))
    tickers = []
    for t in tables:
        if "Ticker" in t.columns:
            tickers = t["Ticker"].tolist()
            break
        if "Symbol" in t.columns:
            tickers = t["Symbol"].tolist()
            break
    if not tickers:
        raise RuntimeError(
            "Couldn't find a Ticker/Symbol column in any table on the Nasdaq-100 Wikipedia "
            "page - it may have been restructured. Use --universe sp500, or a comma list / "
            "file of tickers instead."
        )
    tickers = _clean(tickers)
    _save_cache("nasdaq100", tickers)
    return tickers


def load_from_file(path):
    text = Path(path).read_text()
    raw = [chunk for line in text.splitlines() for chunk in line.replace(",", " ").split()]
    return _clean(raw)


def resolve_universe(spec: str):
    """spec is one of: 'sp500', 'nasdaq100', 'sp500+nasdaq100', a path to a file, or a comma list."""
    spec = spec.strip()
    parts = [p.strip() for p in spec.split("+")]
    tickers = set()
    for part in parts:
        low = part.lower()
        if low == "sp500":
            tickers |= set(get_sp500())
        elif low in ("nasdaq100", "ndx"):
            tickers |= set(get_nasdaq100())
        elif Path(part).exists():
            tickers |= set(load_from_file(part))
        elif "," in part:
            tickers |= set(_clean(part.split(",")))
        else:
            tickers.add(part.upper())
    return sorted(tickers)
