from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from aegis.marketdata import indicators as ind
from aegis.marketdata.scanner import analyse_symbol, eligible_symbol, rank_candidates, select_universe
from tests.conftest import make_candles


def test_ema_matches_recursive_definition():
    s = pd.Series([float(x) for x in range(1, 31)])
    e = ind.ema(s, 10)
    k = 2 / 11
    expected = s.iloc[0]
    for v in s.iloc[1:]:
        expected = v * k + expected * (1 - k)
    assert e.iloc[-1] == pytest.approx(expected)
    assert np.isnan(e.iloc[8]) and not np.isnan(e.iloc[9])


def test_rsi_extremes_and_reference_value():
    up = pd.Series(np.linspace(100, 200, 60))
    down = pd.Series(np.linspace(200, 100, 60))
    assert ind.rsi(up).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(down).iloc[-1] == pytest.approx(0.0, abs=1e-9)
    # Wilder's worked example (first 15 closes) -> RSI ~70.46 with SMA seeding; EWM seeding differs slightly early on,
    # so compare against an independent recursive implementation instead.
    closes = pd.Series([44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28,
                        46.00, 46.03, 46.41, 46.22, 45.64, 46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57])
    d = closes.diff().dropna()
    ag = al = 0.0
    for i, x in enumerate(d):
        g, l_ = max(x, 0), max(-x, 0)
        if i == 0:
            ag, al = g, l_
        else:
            ag, al = ag + (g - ag) / 14, al + (l_ - al) / 14
    assert ind.rsi(closes).iloc[-1] == pytest.approx(100 - 100 / (1 + ag / al))
    assert 0 < ind.rsi(closes).iloc[-1] < 100


def test_macd_and_bollinger_identities():
    close = pd.DataFrame(make_candles(200))["close"]
    m = ind.macd(close)
    assert m["hist"].iloc[-1] == pytest.approx(m["macd"].iloc[-1] - m["signal"].iloc[-1])
    assert m["macd"].iloc[-1] == pytest.approx(ind.ema(close, 12).iloc[-1] - ind.ema(close, 26).iloc[-1])
    bb = ind.bollinger(close)
    win = close.iloc[-20:]
    assert bb["mid"].iloc[-1] == pytest.approx(win.mean())
    assert bb["upper"].iloc[-1] == pytest.approx(win.mean() + 2 * win.std(ddof=0))
    assert bb["lower"].iloc[-1] < bb["mid"].iloc[-1] < bb["upper"].iloc[-1]


def test_atr_constant_range():
    df = pd.DataFrame({"high": [11.0] * 40, "low": [9.0] * 40, "close": [10.0] * 40})
    assert ind.atr(df).iloc[-1] == pytest.approx(2.0)


def test_support_resistance_brackets_price():
    x = np.arange(200)
    close = 100 + 5 * np.sin(x / 8)
    df = pd.DataFrame({"high": close + 0.2, "low": close - 0.2, "close": close})
    lv = ind.support_resistance(df)
    last = close[-1]
    assert lv["support"] and lv["resistance"]
    assert all(s < last for s in lv["support"]) and all(r > last for r in lv["resistance"])
    assert max(lv["resistance"]) == pytest.approx(105.2, abs=0.3)


def test_snapshot_uptrend_and_insufficient_data():
    df = pd.DataFrame(make_candles(300, drift=0.002))
    snap = ind.compute_snapshot(df)
    assert snap["sufficient_data"] and snap["ema"]["trend"] == "up"
    assert snap["ema"]["ema20"] > snap["ema"]["ema50"] > snap["ema"]["ema200"]
    assert snap["rsi14"] > 50 and snap["volatility"]["atr14"] > 0
    assert ind.compute_snapshot(df.iloc[:20]) == {"sufficient_data": False, "bars": 20}
    import json
    json.dumps(snap)  # must be JSON serialisable (no NaN objects / numpy types)


def test_scanner_detects_volume_breakout_and_ignores_quiet_market():
    ticker = {"last": 0, "bid": None, "ask": None, "quote_volume": 5e8, "change_pct": 4.0}
    hot = pd.DataFrame(make_candles(60, drift=0.0, noise=0.001, final_volume_mult=5.0, final_jump=0.03))
    t = {**ticker, "last": hot["close"].iloc[-1], "bid": hot["close"].iloc[-1] * 0.9999, "ask": hot["close"].iloc[-1] * 1.0001}
    c = analyse_symbol("SOL/USDT", t, hot)
    assert c is not None and c.breakout == "up" and c.volume_ratio > 4 and c.spread_bps == pytest.approx(2.0, abs=0.1)
    quiet = pd.DataFrame(make_candles(60, drift=0.0, noise=0.0005))
    assert analyse_symbol("ADA/USDT", {**ticker, "last": quiet["close"].iloc[-1], "change_pct": 0.2}, quiet) is None
    assert analyse_symbol("X/USDT", t, hot.iloc[:10]) is None  # not enough history
    wide = analyse_symbol("THIN/USDT", {**t, "bid": t["last"] * 0.99, "ask": t["last"] * 1.01}, hot)
    assert wide.score < c.score and any("wide spread" in s for s in wide.signals)
    assert [x.symbol for x in rank_candidates([wide, c])] == ["SOL/USDT", "THIN/USDT"]


def test_universe_selection_filters():
    tickers = {
        "BTC/USDT": {"last": 1, "quote_volume": 9e9}, "USDC/USDT": {"last": 1, "quote_volume": 8e9},
        "ETH/USDT": {"last": 1, "quote_volume": 5e9}, "TINY/USDT": {"last": 1, "quote_volume": 10},
        "ETH/BTC": {"last": 1, "quote_volume": 9e9}, "BTCUP/USDT": {"last": 1, "quote_volume": 9e9},
    }
    assert select_universe(tickers, "USDT", 1e6, 10) == ["BTC/USDT", "ETH/USDT"]
    assert select_universe(tickers, "USDT", 1e6, 1) == ["BTC/USDT"]
    assert eligible_symbol("SUPER/USDT", "USDT") and not eligible_symbol("BTCDOWN/USDT", "USDT")
