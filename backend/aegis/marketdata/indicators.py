"""Deterministic technical indicators computed from real OHLCV data (pandas/NumPy). No LLM involvement."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

MIN_BARS = 50


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(avg_loss != 0.0, 100.0)  # no losses in window -> RSI 100
    return out.where(avg_gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper, lower = mid + num_std * std, mid - num_std * std
    width = (upper - lower) / mid
    pct_b = (close - lower) / (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": width, "pct_b": pct_b})


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def realized_volatility(close: pd.Series, period: int = 20) -> pd.Series:
    """Rolling standard deviation of log returns (per bar, not annualised)."""
    return np.log(close / close.shift(1)).rolling(period).std(ddof=0)


def support_resistance(df: pd.DataFrame, window: int = 5, max_levels: int = 3, tolerance: float = 0.005) -> dict[str, list[float]]:
    """Swing-pivot levels, clustered within `tolerance`, split around the last close."""
    highs, lows, last = df["high"].to_numpy(), df["low"].to_numpy(), float(df["close"].iloc[-1])
    pivots: list[float] = []
    for i in range(window, len(df) - window):
        if highs[i] == highs[i - window : i + window + 1].max():
            pivots.append(float(highs[i]))
        if lows[i] == lows[i - window : i + window + 1].min():
            pivots.append(float(lows[i]))
    clusters: list[list[float]] = []
    for p in sorted(pivots):
        if clusters and abs(p - clusters[-1][-1]) / clusters[-1][-1] <= tolerance:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    levels = [(sum(c) / len(c), len(c)) for c in clusters]
    supports = sorted((lv for lv in levels if lv[0] < last), key=lambda x: -x[0])[:max_levels]
    resistances = sorted((lv for lv in levels if lv[0] > last), key=lambda x: x[0])[:max_levels]
    return {"support": [round(s[0], 10) for s in supports], "resistance": [round(r[0], 10) for r in resistances]}


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def compute_snapshot(df: pd.DataFrame) -> dict[str, Any]:
    """Full indicator snapshot for the latest closed bar. `df` needs open/high/low/close/volume, oldest first."""
    n = len(df)
    if n < MIN_BARS:
        return {"sufficient_data": False, "bars": n}
    close, vol = df["close"].astype(float), df["volume"].astype(float)
    last = float(close.iloc[-1])
    m, bb = macd(close), bollinger(close)
    e20, e50, e200 = ema(close, 20), ema(close, 50), ema(close, 200)
    r, a, rv = rsi(close), atr(df.astype({"high": float, "low": float, "close": float})), realized_volatility(close)
    vol_avg = vol.rolling(20).mean()
    hist = m["hist"]
    cross = "none"
    if len(hist.dropna()) >= 2:
        prev, cur = hist.iloc[-2], hist.iloc[-1]
        cross = "bullish" if prev <= 0 < cur else "bearish" if prev >= 0 > cur else "none"
    e20v, e50v, e200v = _f(e20.iloc[-1]), _f(e50.iloc[-1]), _f(e200.iloc[-1])
    if e20v and e50v and e200v:
        trend = "up" if e20v > e50v > e200v else "down" if e20v < e50v < e200v else "mixed"
    elif e20v and e50v:
        trend = "up" if e20v > e50v else "down"
    else:
        trend = "unknown"
    atr_v = _f(a.iloc[-1])
    return {
        "sufficient_data": True,
        "bars": n,
        "last_close": last,
        "rsi14": _f(r.iloc[-1]),
        "macd": {"macd": _f(m["macd"].iloc[-1]), "signal": _f(m["signal"].iloc[-1]), "hist": _f(hist.iloc[-1]), "cross": cross},
        "ema": {"ema20": e20v, "ema50": e50v, "ema200": e200v, "trend": trend,
                "price_vs_ema20_pct": _f((last / e20v - 1) * 100) if e20v else None},
        "bollinger": {k: _f(bb[k].iloc[-1]) for k in ("upper", "mid", "lower", "width", "pct_b")},
        "volume": {"last": _f(vol.iloc[-1]), "avg20": _f(vol_avg.iloc[-1]),
                   "ratio": _f(vol.iloc[-1] / vol_avg.iloc[-1]) if _f(vol_avg.iloc[-1]) else None},
        "momentum": {"roc10_pct": _f((last / float(close.iloc[-11]) - 1) * 100),
                     "roc30_pct": _f((last / float(close.iloc[-31]) - 1) * 100)},
        "volatility": {"atr14": atr_v, "atr_pct": _f(atr_v / last * 100) if atr_v else None,
                       "realized_vol_pct": _f(rv.iloc[-1] * 100)},
        "levels": support_resistance(df.astype({"high": float, "low": float, "close": float})),
    }
