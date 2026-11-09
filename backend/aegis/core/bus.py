"""Redis: live event fan-out to the dashboard, ticker cache, service heartbeats, agent status and counters.

Redis holds only ephemeral/derivable state. Anything that must survive a crash lives in PostgreSQL.
"""
from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import redis.asyncio as aioredis

from aegis.config import get_settings
from aegis.core.timeutil import utcnow

EVENTS_CHANNEL = "aegis:events"
FEED_KEY = "aegis:feed"
TICKERS_KEY = "aegis:tickers"
FEED_MAX = 300


def _default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    if hasattr(o, "isoformat"):
        return o.isoformat()
    raise TypeError(type(o).__name__)


def dumps(obj: Any) -> str:
    return json.dumps(obj, default=_default, separators=(",", ":"))


class Bus:
    def __init__(self, client: aioredis.Redis) -> None:
        self.r = client

    @classmethod
    def from_url(cls, url: str | None = None) -> Bus:
        return cls(aioredis.from_url(url or get_settings().redis_url, decode_responses=True))

    async def close(self) -> None:
        await self.r.aclose()

    async def ping(self) -> bool:
        try:
            return bool(await self.r.ping())
        except Exception:
            return False

    # --- events --------------------------------------------------------------------------
    async def publish(self, kind: str, data: dict[str, Any], *, feed: bool = True) -> None:
        """Best effort: a Redis outage must never break trading logic."""
        msg = dumps({"type": kind, "ts": utcnow().isoformat(), "data": data})
        try:
            await self.r.publish(EVENTS_CHANNEL, msg)
            if feed:
                pipe = self.r.pipeline()
                pipe.lpush(FEED_KEY, msg)
                pipe.ltrim(FEED_KEY, 0, FEED_MAX - 1)
                await pipe.execute()
        except Exception:
            pass

    async def recent_feed(self, limit: int = 100) -> list[dict[str, Any]]:
        return [json.loads(m) for m in await self.r.lrange(FEED_KEY, 0, limit - 1)]

    async def subscribe(self) -> AsyncIterator[str]:
        pubsub = self.r.pubsub()
        await pubsub.subscribe(EVENTS_CHANNEL)
        try:
            async for msg in pubsub.listen():
                if msg.get("type") == "message":
                    yield msg["data"]
        finally:
            await pubsub.aclose()

    # --- tickers -------------------------------------------------------------------------
    async def set_tickers(self, tickers: dict[str, dict[str, Any]]) -> None:
        if tickers:
            await self.r.hset(TICKERS_KEY, mapping={s: dumps(t) for s, t in tickers.items()})

    async def get_ticker(self, symbol: str) -> dict[str, Any] | None:
        raw = await self.r.hget(TICKERS_KEY, symbol)
        return json.loads(raw) if raw else None

    async def get_tickers(self) -> dict[str, dict[str, Any]]:
        return {s: json.loads(v) for s, v in (await self.r.hgetall(TICKERS_KEY)).items()}

    # --- generic cache -------------------------------------------------------------------
    async def cache_set(self, key: str, value: Any, ttl: int) -> None:
        await self.r.set(f"aegis:cache:{key}", dumps(value), ex=ttl)

    async def cache_get(self, key: str) -> Any | None:
        raw = await self.r.get(f"aegis:cache:{key}")
        return json.loads(raw) if raw else None

    # --- heartbeats / status ----------------------------------------------------------------
    async def heartbeat(self, service: str, info: dict[str, Any] | None = None) -> None:
        try:
            await self.r.set(f"aegis:hb:{service}", dumps({"ts": time.time(), **(info or {})}), ex=90)
        except Exception:
            pass

    async def heartbeats(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        async for key in self.r.scan_iter("aegis:hb:*"):
            raw = await self.r.get(key)
            if raw:
                out[key.split(":")[-1]] = json.loads(raw)
        return out

    async def set_agent_status(self, agent: str, status: dict[str, Any]) -> None:
        try:
            await self.r.set(f"aegis:agent:{agent}", dumps({**status, "ts": time.time()}), ex=3600)
        except Exception:
            pass

    async def get_agent_status(self, agent: str) -> dict[str, Any] | None:
        raw = await self.r.get(f"aegis:agent:{agent}")
        return json.loads(raw) if raw else None

    # --- counters (rate limit, LLM budget) ---------------------------------------------------
    async def incr_window(self, key: str, ttl: int) -> int:
        pipe = self.r.pipeline()
        pipe.incr(key)
        pipe.expire(key, ttl, nx=True)
        res = await pipe.execute()
        return int(res[0])


_bus: Bus | None = None


def get_bus() -> Bus:
    global _bus
    if _bus is None:
        _bus = Bus.from_url()
    return _bus


def set_bus(bus: Bus | None) -> None:
    global _bus
    _bus = bus
