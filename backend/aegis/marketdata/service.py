"""Market data service: tickers, candles, universe selection, caching, persistence and staleness detection."""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.core.timeutil import utcnow
from aegis.db.models import Asset, Candle
from aegis.db.session import session_scope
from aegis.marketdata.exchange import MarketDataClient, binance_miniticker_stream
from aegis.marketdata.scanner import Candidate, analyse_symbol, rank_candidates, select_universe

log = get_logger(__name__)
UNIVERSE_KEY = "universe"
MD_STATUS_KEY = "md_status"


class StaleDataError(Exception):
    """Raised instead of returning an old price. Callers must not trade on stale data."""


def _insert(session: AsyncSession, table: Any) -> Any:
    return (postgresql.insert if session.bind.dialect.name == "postgresql" else sqlite.insert)(table)


# ---------------------------------------------------------------------------------------------------
# Read side: used by every service (API, orchestrator, trading engine). Needs only Redis + DB.
# ---------------------------------------------------------------------------------------------------
class MarketDataReader:
    def __init__(self, settings: Settings, bus: Bus) -> None:
        self.settings, self.bus = settings, bus

    async def get_ticker(self, symbol: str, *, max_age: float | None = None) -> dict[str, Any]:
        t = await self.bus.get_ticker(symbol)
        max_age = self.settings.stale_ticker_seconds if max_age is None else max_age
        if not t or not t.get("last"):
            raise StaleDataError(f"no ticker for {symbol}")
        age = time.time() - float(t.get("ts") or 0)
        if age > max_age:
            raise StaleDataError(f"ticker for {symbol} is {age:.0f}s old (limit {max_age:.0f}s)")
        return t

    async def get_price(self, symbol: str, *, max_age: float | None = None) -> Decimal:
        return Decimal(str((await self.get_ticker(symbol, max_age=max_age))["last"]))

    async def get_universe(self) -> list[str]:
        return await self.bus.cache_get(UNIVERSE_KEY) or []

    async def status(self) -> dict[str, Any]:
        return await self.bus.cache_get(MD_STATUS_KEY) or {"healthy": False, "reason": "market data service has not reported"}

    async def get_candles(self, symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
        async with session_scope() as s:
            rows = (await s.execute(
                select(Candle).where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.open_time.desc()).limit(limit)
            )).scalars().all()
        data = [
            {"open_time": r.open_time, "open": float(r.open), "high": float(r.high), "low": float(r.low),
             "close": float(r.close), "volume": float(r.volume)} for r in reversed(rows)
        ]
        return pd.DataFrame(data, columns=["open_time", "open", "high", "low", "close", "volume"])

    async def get_asset(self, symbol: str) -> Asset | None:
        async with session_scope() as s:
            return (await s.execute(select(Asset).where(Asset.symbol == symbol))).scalar_one_or_none()

    async def scan(self, timeframe: str = "15m", limit: int = 8) -> list[Candidate]:
        """Deterministic scan of the tracked universe using cached tickers and stored candles."""
        tickers = await self.bus.get_tickers()
        now, found = time.time(), []
        for symbol in await self.get_universe():
            t = tickers.get(symbol)
            if not t or now - float(t.get("ts") or 0) > self.settings.stale_ticker_seconds * 4:
                continue
            c = analyse_symbol(symbol, t, await self.get_candles(symbol, timeframe, 60))
            if c:
                found.append(c)
        return rank_candidates(found, limit)


# ---------------------------------------------------------------------------------------------------
# Write side: runs only inside the worker service.
# ---------------------------------------------------------------------------------------------------
class MarketDataService(MarketDataReader):
    def __init__(self, settings: Settings, bus: Bus, client: MarketDataClient | None = None) -> None:
        super().__init__(settings, bus)
        self.client = client or MarketDataClient(settings)
        self.tickers: dict[str, dict[str, Any]] = {}
        self.universe: list[str] = []
        self.last_ws_message = 0.0
        self.last_rest_ok = 0.0
        self._last_candle_fetch: dict[tuple[str, str], float] = {}

    async def close(self) -> None:
        await self.client.close()

    async def refresh_markets(self) -> int:
        markets = await self.client.load_markets()
        async with session_scope() as s:
            existing = {a.symbol: a for a in (await s.execute(select(Asset))).scalars()}
            for m in markets:
                a = existing.get(m["symbol"])
                if a is None:
                    a = Asset(symbol=m["symbol"], base=m["base"], quote=m["quote"], exchange=self.client.source)
                    s.add(a)
                a.active, a.min_qty, a.qty_step = m["active"], m["min_qty"], m["qty_step"]
                a.price_tick, a.min_notional, a.updated_at = m["price_tick"], m["min_notional"], utcnow()
            listed = {m["symbol"] for m in markets}
            for sym, a in existing.items():
                if sym not in listed:
                    a.active = False
        log.info("markets_refreshed", count=len(markets))
        return len(markets)

    async def refresh_tickers(self) -> int:
        """Full 24h tickers over REST (bid/ask + volume). Also recomputes the tracked universe."""
        fresh = await self.client.fetch_tickers()
        for sym, t in fresh.items():
            self.tickers[sym] = {**self.tickers.get(sym, {}), **{k: v for k, v in t.items() if v is not None}}
        self.last_rest_ok = time.time()
        await self.bus.set_tickers({s: self.tickers[s] for s in fresh})
        self.universe = select_universe(
            self.tickers, self.settings.quote_asset, float(self.settings.min_quote_volume_24h), self.settings.universe_max_symbols,
            frozenset(b.strip().upper() for b in self.settings.universe_exclude.split(",") if b.strip()),
        )
        await self.bus.cache_set(UNIVERSE_KEY, self.universe, ttl=3600)
        return len(fresh)

    def apply_ws_batch(self, batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Merge a mini-ticker batch into the in-memory book; returns the changed tickers."""
        changed: dict[str, dict[str, Any]] = {}
        for m in batch:
            sym = self.client.id_to_symbol.get(m.get("s", ""))
            if not sym:
                continue
            try:
                last, open_ = float(m["c"]), float(m["o"])
            except (KeyError, ValueError):
                continue
            if last <= 0:
                continue
            t = self.tickers.setdefault(sym, {"symbol": sym})
            t.update(last=last, quote_volume=float(m.get("q") or 0), ts=float(m.get("E", time.time() * 1000)) / 1000,
                     change_pct=(last / open_ - 1) * 100 if open_ > 0 else None)
            changed[sym] = t
        if changed:
            self.last_ws_message = time.time()
        return changed

    async def run_stream(self, stop: asyncio.Event, watch: Any = None) -> None:
        """Consume the WebSocket stream; push tracked prices to the dashboard at most every 2s."""
        ws_url = self.settings.binance_testnet_ws_url if self.client.source == "binance_testnet" else self.settings.binance_ws_url
        last_push = 0.0
        async for batch in binance_miniticker_stream(ws_url, stop=stop):
            changed = self.apply_ws_batch(batch)
            await self.bus.set_tickers(changed)
            if time.time() - last_push >= 2.0:
                last_push = time.time()
                symbols = set(self.universe[:10]) | (set(await watch()) if watch else set())
                prices = {s: self.tickers[s]["last"] for s in symbols if s in self.tickers}
                if prices:
                    await self.bus.publish("prices", prices, feed=False)

    async def ingest_candles(self, symbol: str, timeframe: str, limit: int = 300) -> int:
        """Fetch and upsert CLOSED candles only (the forming bar is excluded so indicators never repaint)."""
        rows = await self.client.fetch_ohlcv(symbol, timeframe, limit=limit)
        tf_ms = self.client.timeframe_seconds(timeframe) * 1000
        now_ms = time.time() * 1000
        values = [
            {"symbol": symbol, "timeframe": timeframe, "open_time": datetime.fromtimestamp(r[0] / 1000, UTC),
             "open": Decimal(str(r[1])), "high": Decimal(str(r[2])), "low": Decimal(str(r[3])),
             "close": Decimal(str(r[4])), "volume": Decimal(str(r[5]))}
            for r in rows if r[0] + tf_ms <= now_ms
        ]
        if not values:
            return 0
        async with session_scope() as s:
            for i in range(0, len(values), 200):
                stmt = _insert(s, Candle).values(values[i : i + 200])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["symbol", "timeframe", "open_time"],
                    set_={c: getattr(stmt.excluded, c) for c in ("open", "high", "low", "close", "volume")},
                )
                await s.execute(stmt)
        return len(values)

    async def ingest_universe_candles(self, extra_symbols: list[str] | None = None) -> int:
        """Backfill 300 bars the first time, then top up once per closed bar."""
        sem, total, now = asyncio.Semaphore(4), 0, time.time()
        symbols = list(dict.fromkeys([*self.universe, *(extra_symbols or [])]))

        async def one(symbol: str, tf: str) -> int:
            tf_s = self.client.timeframe_seconds(tf)
            last = self._last_candle_fetch.get((symbol, tf))
            if last is not None and int(last // tf_s) == int(now // tf_s):
                return 0  # no new closed bar since the last fetch
            async with sem:
                try:
                    n = await self.ingest_candles(symbol, tf, limit=300 if last is None else 5)
                    self._last_candle_fetch[(symbol, tf)] = now
                    return n
                except Exception as e:
                    log.warning("candle_ingest_failed", symbol=symbol, timeframe=tf, error=str(e))
                    return 0

        for n in await asyncio.gather(*(one(s, tf) for s in symbols for tf in self.settings.timeframes)):
            total += n
        return total

    async def report_status(self) -> dict[str, Any]:
        """Detect stale/missing data and publish health. Consumers refuse to trade when unhealthy."""
        now = time.time()
        ws_age = now - self.last_ws_message if self.last_ws_message else None
        rest_age = now - self.last_rest_ok if self.last_rest_ok else None
        fresh = sum(1 for s in self.universe if now - float(self.tickers.get(s, {}).get("ts") or 0) <= self.settings.stale_ticker_seconds)
        healthy = bool(self.universe) and fresh >= max(1, len(self.universe) // 2)
        reason = "" if healthy else ("no tradable universe yet" if not self.universe else f"only {fresh}/{len(self.universe)} tickers are fresh")
        status = {"healthy": healthy, "reason": reason, "source": self.client.source, "universe": len(self.universe),
                  "fresh_tickers": fresh, "ws_age_s": ws_age, "rest_age_s": rest_age, "ts": now}
        await self.bus.cache_set(MD_STATUS_KEY, status, ttl=60)
        return status

    async def prune_candles(self, keep_days: int = 120) -> None:
        cutoff = datetime.fromtimestamp(time.time() - keep_days * 86400, UTC)
        async with session_scope() as s:
            await s.execute(delete(Candle).where(Candle.open_time < cutoff, Candle.timeframe.in_(["1m", "5m", "15m"])))
