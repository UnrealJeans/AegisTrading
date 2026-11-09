from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import fakeredis.aioredis
import numpy as np
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from aegis.config import Settings
from aegis.core.bus import Bus, set_bus
from aegis.core.state import TradingStatus, set_control
from aegis.db import models  # noqa: F401
from aegis.db.base import Base
from aegis.db.models import Asset, Candle
from aegis.db.session import make_engine, session_scope, set_sessionmaker
from aegis.llm.providers import LLMError, LLMResponse, LLMRouter
from aegis.marketdata.service import MD_STATUS_KEY, UNIVERSE_KEY, MarketDataReader


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = dict(
        secret_key="x" * 40, admin_password="correct-horse-battery", llm_default_provider="fake", llm_max_retries=1,
        agent_timeout_seconds=5, llm_timeout_seconds=2, cors_origins="http://testserver", environment="test",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[async_sessionmaker]:
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/test.db"
    engine = make_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    set_sessionmaker(factory)
    yield factory
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def bus() -> AsyncIterator[Bus]:
    b = Bus(fakeredis.aioredis.FakeRedis(decode_responses=True))
    set_bus(b)
    yield b
    await b.close()
    set_bus(None)


@pytest.fixture
def reader(settings: Settings, bus: Bus, db) -> MarketDataReader:
    return MarketDataReader(settings, bus)


def make_candles(n: int = 300, start: float = 100.0, drift: float = 0.001, noise: float = 0.002, seed: int = 7,
                 final_volume_mult: float = 1.0, final_jump: float = 0.0) -> list[dict[str, float]]:
    """Deterministic synthetic OHLCV for tests only."""
    rng = np.random.default_rng(seed)
    out, price = [], start
    for i in range(n):
        o = price
        c = o * (1 + drift + rng.normal(0, noise))
        if i == n - 1:
            c = o * (1 + final_jump) if final_jump else c
        h, lo = max(o, c) * (1 + abs(rng.normal(0, noise / 2))), min(o, c) * (1 - abs(rng.normal(0, noise / 2)))
        v = 1000 * (1 + abs(rng.normal(0, 0.1))) * (final_volume_mult if i == n - 1 else 1.0)
        out.append({"open": o, "high": h, "low": lo, "close": c, "volume": v})
        price = c
    return out


async def set_ticker(bus: Bus, symbol: str, price: float, *, age: float = 0.0, spread_bps: float = 2.0, quote_volume: float = 5e8, change_pct: float = 3.0) -> None:
    half = price * spread_bps / 2e4
    await bus.set_tickers({symbol: {"symbol": symbol, "last": price, "bid": price - half, "ask": price + half,
                                    "quote_volume": quote_volume, "change_pct": change_pct, "ts": time.time() - age}})


async def seed_market(bus: Bus, symbol: str = "BTC/USDT", *, candles: list[dict[str, float]] | None = None, timeframes=("15m", "1h", "4h"),
                      qty_step: str = "0.00001", min_notional: str = "5") -> float:
    candles = candles or make_candles(final_volume_mult=4.0, final_jump=0.02)
    base, quote = symbol.split("/")
    minutes = {"15m": 15, "1h": 60, "4h": 240}
    async with session_scope() as s:
        s.add(Asset(symbol=symbol, base=base, quote=quote, active=True, min_qty=Decimal(qty_step), qty_step=Decimal(qty_step),
                    price_tick=Decimal("0.01"), min_notional=Decimal(min_notional)))
        for tf in timeframes:
            t0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=minutes[tf] * (len(candles) + 1))
            for i, c in enumerate(candles):
                s.add(Candle(symbol=symbol, timeframe=tf, open_time=t0 + timedelta(minutes=minutes[tf] * i),
                             **{k: Decimal(str(round(v, 8))) for k, v in c.items()}))
    last = candles[-1]["close"]
    await set_ticker(bus, symbol, last)
    universe = (await bus.cache_get(UNIVERSE_KEY) or []) + [symbol]
    await bus.cache_set(UNIVERSE_KEY, universe, ttl=3600)
    await bus.cache_set(MD_STATUS_KEY, {"healthy": True, "reason": ""}, ttl=3600)
    return last


async def start_trading() -> None:
    async with session_scope() as s:
        await set_control(s, TradingStatus.RUNNING, by="test")


MARKERS = {
    "scout": "You are SCOUT", "news": "You are NEWS & SENTIMENT", "technical": "You are TECHNICAL ANALYST",
    "analyst": "You are ANALYST", "skeptic": "You are SKEPTIC", "risk_manager": "You are RISK MANAGER",
}


class ScriptedLLM:
    """Test double for the provider HTTP boundary. Records every call so tests can assert on LLM usage."""
    name = "fake"

    def __init__(self, scripts: dict[str, Any]) -> None:
        self.scripts = scripts
        self.calls: list[dict[str, Any]] = []

    def agent_of(self, system: str) -> str:
        return next(a for a, m in MARKERS.items() if m in system)

    def count(self, agent: str) -> int:
        return sum(1 for c in self.calls if c["agent"] == agent)

    async def complete(self, *, system: str, user: str, model: str, temperature: float, timeout: float, json_mode: bool = True) -> LLMResponse:
        agent = self.agent_of(system)
        self.calls.append({"agent": agent, "user": user, "model": model})
        script = self.scripts.get(agent)
        if script is None:
            raise LLMError(f"no script for {agent}")
        if isinstance(script, list):
            script = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(script, Callable):  # type: ignore[arg-type]
            script = script(user)
        if isinstance(script, Exception):
            raise script
        return LLMResponse(content=script if isinstance(script, str) else json.dumps(script), model=model, prompt_tokens=10, completion_tokens=5)


def input_of(user_prompt: str) -> dict[str, Any]:
    body = user_prompt.split("INPUT DATA (JSON):\n", 1)[1].split("\n\nOUTPUT JSON SCHEMA:", 1)[0]
    return json.loads(body)


def router_with(settings: Settings, bus: Bus | None, llm: ScriptedLLM) -> LLMRouter:
    return LLMRouter(settings, bus, providers={"fake": llm})
