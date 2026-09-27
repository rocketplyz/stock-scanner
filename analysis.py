"""Technical indicators and setup scoring.

This is a pattern screener, not investment advice: it flags stocks whose price
action matches a few well-known technical templates (trend pullback, breakout,
volatility squeeze). It does not know anything about the company, valuation,
news, or risk tolerance.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

MIN_ROWS = 210  # need ~200 sessions for the 200-day SMA plus a little buffer
SWING_WINDOW = 5  # bars on each side required to confirm a swing high/low (11-bar fractal)


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def find_swings(df: pd.DataFrame, window: int = SWING_WINDOW):
    """Fractal swing points: a bar is a swing high/low if it's the extreme of a
    `window`-bar window on both sides. Returns a chronological list of
    (position, 'H'|'L', price) with consecutive same-type swings collapsed to
    the most extreme one, so highs and lows strictly alternate."""
    n = len(df)
    span = window * 2 + 1
    if n < span:
        return []

    roll_max = df["High"].rolling(span, center=True).max()
    roll_min = df["Low"].rolling(span, center=True).min()
    is_high = (df["High"] == roll_max) & roll_max.notna()
    is_low = (df["Low"] == roll_min) & roll_min.notna()

    candidates = []
    high_vals, low_vals = df["High"].values, df["Low"].values
    for pos in range(n):
        if is_high.iloc[pos]:
            candidates.append((pos, "H", float(high_vals[pos])))
        if is_low.iloc[pos]:
            candidates.append((pos, "L", float(low_vals[pos])))
    candidates.sort(key=lambda c: c[0])

    alternating = []
    for c in candidates:
        if not alternating or c[1] != alternating[-1][1]:
            alternating.append(c)
        else:
            last = alternating[-1]
            more_extreme = (c[1] == "H" and c[2] > last[2]) or (c[1] == "L" and c[2] < last[2])
            if more_extreme:
                alternating[-1] = c
    return alternating


def classify_structure(df: pd.DataFrame, window: int = SWING_WINDOW):
    """Market structure from swing highs/lows: HH/HL = uptrend, LH/LL = downtrend,
    plus break-of-structure (BOS, trend continuation) / change-of-character
    (CHoCH, first break the other way) against the most recent confirmed swings."""
    swings = find_swings(df, window)
    highs = [(pos, price) for pos, kind, price in swings if kind == "H"]
    lows = [(pos, price) for pos, kind, price in swings if kind == "L"]

    high_trend = ("HH" if highs[-1][1] > highs[-2][1] else "LH") if len(highs) >= 2 else None
    low_trend = ("HL" if lows[-1][1] > lows[-2][1] else "LL") if len(lows) >= 2 else None

    if high_trend == "HH" and low_trend == "HL":
        bias = "uptrend"
    elif high_trend == "LH" and low_trend == "LL":
        bias = "downtrend"
    else:
        bias = "mixed"

    last_swing_high = highs[-1][1] if highs else None
    last_swing_low = lows[-1][1] if lows else None
    close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2]) if len(df) > 1 else close

    # Only flag a break on the bar where the crossover actually happens - otherwise
    # a stock that ran through an old swing high months ago would flag forever.
    event = None
    if last_swing_high is not None and close > last_swing_high and prev_close <= last_swing_high:
        event = "Bullish BOS" if bias == "uptrend" else "Bullish CHoCH"
    elif last_swing_low is not None and close < last_swing_low and prev_close >= last_swing_low:
        event = "Bearish BOS" if bias == "downtrend" else "Bearish CHoCH"

    return {
        "bias": bias,
        "high_trend": high_trend,
        "low_trend": low_trend,
        "last_swing_high": last_swing_high,
        "last_swing_low": last_swing_low,
        "event": event,
    }


def compute_indicators(df: pd.DataFrame) -> dict | None:
    """df: OHLCV for one ticker, indexed by date ascending. Returns latest-bar features."""
    df = df.dropna(subset=["Close", "Volume"])
    if len(df) < MIN_ROWS:
        return None

    close = df["Close"]
    vol = df["Volume"]

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    vol20 = vol.rolling(20).mean()
    bb_std = close.rolling(20).std()
    bb_width = (4 * bb_std) / sma20  # (upper-lower)/mid, upper/lower = sma20 +/- 2*std
    bb_width_rank = bb_width.rolling(100).apply(lambda s: pd.Series(s).rank(pct=True).iloc[-1], raw=False)
    high20_excl_today = close.shift(1).rolling(20).max()
    high52_excl_today = close.shift(1).rolling(252).max()
    rsi14 = rsi(close, 14)
    atr14 = atr(df, 14)
    sma50_slope = sma50 - sma50.shift(20)

    i = -1  # latest bar
    if pd.isna(sma200.iloc[i]) or pd.isna(bb_width_rank.iloc[i]):
        return None

    last_close = float(close.iloc[i])
    last_vol = float(vol.iloc[i])
    v20 = float(vol20.iloc[i]) if vol20.iloc[i] else np.nan
    structure = classify_structure(df)

    return {
        "structure": structure,
        "close": last_close,
        "change_1d_pct": float((close.iloc[i] / close.iloc[i - 1] - 1) * 100) if len(close) > 1 else 0.0,
        "change_5d_pct": float((close.iloc[i] / close.iloc[i - 5] - 1) * 100) if len(close) > 5 else 0.0,
        "volume": last_vol,
        "vol20avg": v20,
        "vol_ratio": (last_vol / v20) if v20 and not np.isnan(v20) else np.nan,
        "sma20": float(sma20.iloc[i]),
        "sma50": float(sma50.iloc[i]),
        "sma200": float(sma200.iloc[i]),
        "sma50_slope_up": bool(sma50_slope.iloc[i] > 0),
        "rsi14": float(rsi14.iloc[i]),
        "atr14": float(atr14.iloc[i]),
        "atr_pct": float(atr14.iloc[i] / last_close * 100),
        "bb_width_rank": float(bb_width_rank.iloc[i]),  # 0 = tightest range in 100d, 1 = widest
        "high20": float(high20_excl_today.iloc[i]) if not pd.isna(high20_excl_today.iloc[i]) else np.nan,
        "high52": float(high52_excl_today.iloc[i]) if not pd.isna(high52_excl_today.iloc[i]) else np.nan,
        "pct_from_sma20": float((last_close - sma20.iloc[i]) / sma20.iloc[i] * 100),
        "close_series": close.tail(150).tolist(),  # for sparklines (row view uses the last 60 of this)
        "close_dates": [d.strftime("%Y-%m-%d") for d in close.tail(150).index],
        "open_series": df["Open"].tail(150).tolist(),
        "high_series": df["High"].tail(150).tolist(),
        "low_series": df["Low"].tail(150).tolist(),
    }


def _clip01(x):
    return max(0.0, min(1.0, x))


def score_trend_pullback(f: dict):
    trend_up = f["close"] > f["sma50"] > f["sma200"] and f["sma50_slope_up"]
    if not trend_up:
        return 0, []
    reasons = ["Uptrend: price > 50d SMA > 200d SMA, 50d SMA rising"]
    score = 40

    closeness = 1 - _clip01(abs(f["pct_from_sma20"]) / 6)  # full credit within ~0-6% of the 20d SMA
    score += 20 * closeness
    if abs(f["pct_from_sma20"]) <= 3:
        reasons.append(f"Pulled back to within {abs(f['pct_from_sma20']):.1f}% of the 20d SMA")

    rsi_fit = 1 - _clip01(abs(f["rsi14"] - 45) / 20)  # sweet spot ~45, ok 25-65
    score += 20 * rsi_fit
    if 35 <= f["rsi14"] <= 55:
        reasons.append(f"RSI cooled to {f['rsi14']:.0f} (not overbought, not oversold)")

    if not np.isnan(f["vol_ratio"]):
        vol_calm = _clip01((1.1 - f["vol_ratio"]) / 0.6)  # best when well under 1.0
        score += 20 * vol_calm
        if f["vol_ratio"] < 0.9:
            reasons.append(f"Volume drying up on the pullback ({f['vol_ratio']:.1f}x avg)")

    struct = f.get("structure") or {}
    last_swing_low = struct.get("last_swing_low")
    if last_swing_low is not None:
        if f["close"] < last_swing_low:
            score *= 0.5  # pullback has broken the last swing low - structure may be turning, not a clean dip
            reasons.append(f"Caution: closed below the last swing low (${last_swing_low:.2f}) — structure may be turning")
        elif struct.get("low_trend") == "HL":
            score = min(100, score + 10)
            reasons.append("Structure intact: still making higher lows")

    return round(_clip01(score / 100) * 100, 1), reasons


def score_breakout(f: dict):
    if np.isnan(f.get("high20", np.nan)) or f["close"] <= f["high20"]:
        return 0, []
    reasons = [f"Closed above the prior 20-day high (${f['high20']:.2f})"]
    score = 40

    if not np.isnan(f["vol_ratio"]):
        vol_boost = _clip01((f["vol_ratio"] - 1.0) / 2.0)  # scales up to 3x volume
        score += 30 * vol_boost
        if f["vol_ratio"] >= 1.5:
            reasons.append(f"Volume confirming: {f['vol_ratio']:.1f}x the 20-day average")
        elif f["vol_ratio"] < 1.1:
            reasons.append("Caution: breakout on light volume")

    was_tight = _clip01((0.5 - f["bb_width_rank"]) / 0.5)  # rewards squeeze rank below 0.5
    score += 30 * was_tight
    if f["bb_width_rank"] <= 0.3:
        reasons.append("Broke out of a tight, low-volatility range")

    return round(_clip01(score / 100) * 100, 1), reasons


def score_squeeze(f: dict):
    if f["bb_width_rank"] > 0.25 or f["close"] <= f["sma50"]:
        return 0, []
    reasons = [f"Bollinger Band width at its tightest in ~100 days (pct-rank {f['bb_width_rank']:.2f})"]
    score = 35

    tightness = _clip01((0.25 - f["bb_width_rank"]) / 0.25)
    score += 35 * tightness

    if not np.isnan(f.get("high20", np.nan)) and f["high20"]:
        dist_to_trigger = (f["high20"] - f["close"]) / f["close"] * 100
        proximity = _clip01((5 - max(dist_to_trigger, 0)) / 5)
        score += 30 * proximity
        if dist_to_trigger <= 3:
            reasons.append(f"Coiling just {dist_to_trigger:.1f}% under the 20-day high — watch for the trigger")

    reasons.append("In an uptrend bias (price above 50d SMA)")
    return round(_clip01(score / 100) * 100, 1), reasons


def score_structure_break(f: dict):
    struct = f.get("structure") or {}
    event = struct.get("event")
    if event not in ("Bullish BOS", "Bullish CHoCH"):
        return 0, []

    last_high = struct["last_swing_high"]
    if event == "Bullish BOS":
        score = 50
        reasons = [
            f"Bullish break of structure: closed above the last swing high (${last_high:.2f}), "
            f"continuing the higher-high/higher-low uptrend"
        ]
    else:
        score = 45
        reasons = [
            f"Bullish change of character: closed above the last swing high (${last_high:.2f}), "
            f"first break higher after a {struct['bias']} structure"
        ]

    if not np.isnan(f.get("vol_ratio", np.nan)):
        vol_boost = _clip01((f["vol_ratio"] - 1.0) / 2.0)
        score += 30 * vol_boost
        if f["vol_ratio"] >= 1.5:
            reasons.append(f"Volume confirming: {f['vol_ratio']:.1f}x the 20-day average")

    if struct.get("low_trend") == "HL":
        score += 20
        reasons.append("Higher-low sequence still intact underneath")

    return round(_clip01(score / 100) * 100, 1), reasons


SETUPS = {
    "Trend Pullback": score_trend_pullback,
    "Breakout": score_breakout,
    "Volatility Squeeze": score_squeeze,
    "Structure Break": score_structure_break,
}


def evaluate(f: dict):
    """Returns dict: setup_name -> (score, reasons); plus best_setup/best_score."""
    results = {}
    for name, fn in SETUPS.items():
        score, reasons = fn(f)
        results[name] = {"score": score, "reasons": reasons}
    best_name = max(results, key=lambda k: results[k]["score"])
    best_score = results[best_name]["score"]
    return {
        "setups": results,
        "best_setup": best_name if best_score > 0 else None,
        "best_score": best_score,
    }


def compute_risk_levels(f: dict):
    """Fixed-formula stop-loss / take-profit reference levels for a hypothetical
    long entry near the current price. Applied identically to every ticker - a
    technical calculation, not personalized advice. Stop uses the last confirmed
    swing low when there is a clean one nearby, otherwise a 2x-ATR volatility
    stop; targets are simple 2R/3R multiples of the resulting risk."""
    close = f["close"]
    atr = f["atr14"] if f["atr14"] > 0 else close * 0.02
    last_swing_low = (f.get("structure") or {}).get("last_swing_low")

    if last_swing_low is not None and last_swing_low < close * 0.995:
        stop_price = last_swing_low - 0.25 * atr
        stop_method = "Below the last confirmed swing low"
    else:
        stop_price = close - 2.0 * atr
        stop_method = "2x ATR(14) below price (no clean recent swing low)"

    if stop_price >= close:  # guard against a degenerate case
        stop_price = close - atr
        stop_method = "1x ATR(14) below price (fallback)"

    risk = close - stop_price
    return {
        "stop_price": stop_price,
        "stop_method": stop_method,
        "stop_pct": risk / close * 100,
        "risk_per_share": risk,
        "target_2r": close + 2 * risk,
        "target_3r": close + 3 * risk,
        "target_2r_pct": 2 * risk / close * 100,
        "target_3r_pct": 3 * risk / close * 100,
    }
