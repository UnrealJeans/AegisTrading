"""Deterministic market scanner. Cheap filter that decides whether the (paid) LLM agents run at all."""
from __future__ import annotations

from typing import Any

import pandas as pd
from pydantic import BaseModel

STABLE_BASES = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "AEUR", "USD1", "XUSD", "PAXG", "WBTC", "WBETH", "BFUSD", "USDE", "RLUSD", "XAUT", "PYUSD", "USDS"}
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


class Candidate(BaseModel):
    symbol: str
    score: float
    last_price: float
    change_24h_pct: float
    quote_volume_24h: float
    spread_bps: float | None
    volume_ratio: float | None        # last closed bar volume / 20-bar average
    volume_zscore: float | None
    breakout: str                     # "up" | "down" | "none" (close beyond prior 20-bar range)
    range_position: float | None      # 0..1 position of close inside the 20-bar range
    change_1h_pct: float | None
    signals: list[str]


def eligible_symbol(symbol: str, quote: str) -> bool:
    base, _, q = symbol.partition("/")
    if q != quote or base in STABLE_BASES:
        return False
    return not any(base.endswith(s) and len(base) > len(s) + 2 for s in LEVERAGED_SUFFIXES)


def select_universe(tickers: dict[str, dict[str, Any]], quote: str, min_quote_volume: float, max_symbols: int,
                    exclude_bases: frozenset[str] = frozenset()) -> list[str]:
    rows = [
        (s, float(t.get("quote_volume") or 0.0)) for s, t in tickers.items()
        if eligible_symbol(s, quote) and s.split("/")[0] not in exclude_bases and float(t.get("quote_volume") or 0.0) >= min_quote_volume and t.get("last")
    ]
    rows.sort(key=lambda x: -x[1])
    return [s for s, _ in rows[:max_symbols]]


def analyse_symbol(symbol: str, ticker: dict[str, Any], candles: pd.DataFrame, lookback: int = 20) -> Candidate | None:
    """Score one symbol from its 24h ticker and recent closed candles (oldest first)."""
    last = float(ticker.get("last") or 0.0)
    if last <= 0 or len(candles) < lookback + 2:
        return None
    close, high, low, vol = (candles[c].astype(float) for c in ("close", "high", "low", "volume"))
    prior_vol = vol.iloc[-lookback - 1 : -1]
    mean_v, std_v = float(prior_vol.mean()), float(prior_vol.std(ddof=0))
    ratio = float(vol.iloc[-1]) / mean_v if mean_v > 0 else None
    z = (float(vol.iloc[-1]) - mean_v) / std_v if std_v > 0 else None
    hi, lo, c = float(high.iloc[-lookback - 1 : -1].max()), float(low.iloc[-lookback - 1 : -1].min()), float(close.iloc[-1])
    breakout = "up" if c > hi else "down" if c < lo else "none"
    pos = (c - lo) / (hi - lo) if hi > lo else None
    bars_1h = max(1, min(len(close) - 1, 4))
    ch1h = (c / float(close.iloc[-1 - bars_1h]) - 1) * 100
    bid, ask = ticker.get("bid"), ticker.get("ask")
    spread = (float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2) * 1e4 if bid and ask and float(bid) > 0 else None
    ch24 = float(ticker.get("change_pct") or 0.0)

    signals: list[str] = []
    score = 0.0
    if ratio is not None and ratio >= 2.0:
        signals.append(f"volume {ratio:.1f}x 20-bar average")
        score += min(ratio, 6.0)
    if z is not None and z >= 3.0:
        signals.append(f"volume z-score {z:.1f}")
        score += 1.0
    if breakout != "none":
        signals.append(f"{breakout}side breakout of {lookback}-bar range")
        score += 2.5
    if abs(ch1h) >= 1.5:
        signals.append(f"{ch1h:+.2f}% move in the last hour")
        score += min(abs(ch1h), 5.0) * 0.6
    if abs(ch24) >= 6.0:
        signals.append(f"{ch24:+.1f}% over 24h")
        score += 1.0
    if not signals:
        return None
    if spread is not None and spread > 15:  # illiquid books are penalised, not hidden
        signals.append(f"wide spread {spread:.1f} bps")
        score *= 0.5
    return Candidate(
        symbol=symbol, score=round(score, 3), last_price=last, change_24h_pct=ch24,
        quote_volume_24h=float(ticker.get("quote_volume") or 0.0), spread_bps=round(spread, 2) if spread is not None else None,
        volume_ratio=round(ratio, 3) if ratio is not None else None, volume_zscore=round(z, 3) if z is not None else None,
        breakout=breakout, range_position=round(pos, 3) if pos is not None else None, change_1h_pct=round(ch1h, 3), signals=signals,
    )


def rank_candidates(candidates: list[Candidate], limit: int = 8) -> list[Candidate]:
    return sorted(candidates, key=lambda c: -c.score)[:limit]
