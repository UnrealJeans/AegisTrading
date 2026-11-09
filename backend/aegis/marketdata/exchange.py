"""Public market-data access through CCXT (REST) and the Binance WebSocket stream."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import ccxt.async_support as ccxt
import websockets

from aegis.config import Settings
from aegis.core.logging import get_logger

log = get_logger(__name__)


class MarketDataError(Exception):
    pass


def _rewrite_urls(obj: Any, old: str, new: str) -> Any:
    if isinstance(obj, str):
        return obj.replace(old, new)
    if isinstance(obj, dict):
        return {k: _rewrite_urls(v, old, new) for k, v in obj.items()}
    return obj


def build_ccxt_binance(settings: Settings, *, source: str, api_key: str = "", secret: str = "") -> ccxt.binance:
    """Spot-only Binance client. `source` is "binance" or "binance_testnet"."""
    ex = ccxt.binance({
        "apiKey": api_key or None,
        "secret": secret or None,
        "enableRateLimit": True,
        "timeout": int(settings.exchange_timeout_seconds * 1000),
        "options": {
            "defaultType": "spot",
            "fetchMarkets": ["spot"],          # never touch futures/margin endpoints
            "fetchCurrencies": False,          # sapi endpoint; needs keys and is not on testnet
            "adjustForTimeDifference": True,
            "warnOnFetchOpenOrdersWithoutSymbol": False,
        },
    })
    ex.aiohttp_trust_env = True  # honour HTTP(S)_PROXY / NO_PROXY when the host routes egress through a proxy
    if source == "binance_testnet":
        ex.set_sandbox_mode(True)
        default = "https://testnet.binance.vision"
        if settings.binance_testnet_rest_url.rstrip("/") != default:
            ex.urls["api"] = _rewrite_urls(ex.urls["api"], default, settings.binance_testnet_rest_url.rstrip("/"))
    else:
        default = "https://api.binance.com"
        if settings.binance_rest_url.rstrip("/") != default:
            ex.urls["api"] = _rewrite_urls(ex.urls["api"], default, settings.binance_rest_url.rstrip("/"))
    return ex


def normalize_ticker(symbol: str, t: dict[str, Any]) -> dict[str, Any]:
    ts = (t.get("timestamp") or time.time() * 1000) / 1000
    return {
        "symbol": symbol, "last": t.get("last"), "bid": t.get("bid"), "ask": t.get("ask"),
        "quote_volume": t.get("quoteVolume"), "change_pct": t.get("percentage"), "ts": ts,
    }


class MarketDataClient:
    """Thin async wrapper so the rest of the system never imports ccxt directly."""

    def __init__(self, settings: Settings, exchange: Any | None = None) -> None:
        self.settings = settings
        self.source = settings.data_source
        self.ex = exchange or build_ccxt_binance(settings, source=self.source)
        self.id_to_symbol: dict[str, str] = {}

    async def close(self) -> None:
        await self.ex.close()

    async def _call(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kw: Any) -> Any:
        try:
            return await asyncio.wait_for(fn(*args, **kw), timeout=self.settings.exchange_timeout_seconds + 5)
        except TimeoutError as e:
            raise MarketDataError(f"timeout calling {getattr(fn, '__name__', fn)}") from e
        except ccxt.BaseError as e:
            raise MarketDataError(f"{type(e).__name__}: {e}") from e

    async def load_markets(self) -> list[dict[str, Any]]:
        markets = await self._call(self.ex.load_markets, True)
        out = []
        for sym, m in markets.items():
            if not m.get("spot") or m.get("quote") != self.settings.quote_asset:
                continue
            self.id_to_symbol[m["id"]] = sym
            lim, prec = m.get("limits") or {}, m.get("precision") or {}
            out.append({
                "symbol": sym, "base": m["base"], "quote": m["quote"], "active": bool(m.get("active", True)),
                "min_qty": (lim.get("amount") or {}).get("min"), "qty_step": prec.get("amount"),
                "price_tick": prec.get("price"), "min_notional": (lim.get("cost") or {}).get("min"),
            })
        return out

    async def fetch_tickers(self) -> dict[str, dict[str, Any]]:
        raw = await self._call(self.ex.fetch_tickers)
        q = "/" + self.settings.quote_asset
        return {s: normalize_ticker(s, t) for s, t in raw.items() if s.endswith(q) and t.get("last")}

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 300, since: int | None = None) -> list[list[float]]:
        return await self._call(self.ex.fetch_ohlcv, symbol, timeframe, since, limit)

    def timeframe_seconds(self, timeframe: str) -> int:
        return int(self.ex.parse_timeframe(timeframe))


async def binance_miniticker_stream(ws_base_url: str, *, stop: asyncio.Event) -> AsyncIterator[list[dict[str, Any]]]:
    """Yield batches from the all-market mini-ticker stream. Reconnects forever with capped backoff."""
    url = ws_base_url.rstrip("/") + "/ws/!miniTicker@arr"
    backoff = 1.0
    while not stop.is_set():
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**22) as ws:
                log.info("ws_connected", url=url)
                backoff = 1.0
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(raw)
                    if isinstance(data, list):
                        yield data
        except asyncio.CancelledError:
            raise
        except Exception as e:  # network errors, timeouts, protocol errors
            log.warning("ws_disconnected", error=f"{type(e).__name__}: {e}", retry_in=backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
