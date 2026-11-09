"""Trading engine service."""
from __future__ import annotations

import asyncio

from aegis.config import Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.core.state import get_risk_params, record_system_event
from aegis.db.models import Severity
from aegis.db.session import session_scope
from aegis.marketdata.service import MarketDataReader
from aegis.services.runtime import run_service, sleep_or_stop
from aegis.trading.adapters import ModeNotAllowed, build_adapter
from aegis.trading.engine import TradingEngine

log = get_logger(__name__)


async def main(settings: Settings, bus: Bus, stop: asyncio.Event) -> None:
    reader = MarketDataReader(settings, bus)
    async with session_scope() as s:
        params = await get_risk_params(s, settings)
    try:
        adapter = build_adapter(settings, reader, params)
    except ModeNotAllowed as e:
        # Stay up (so the dashboard can show why) but never trade.
        async with session_scope() as s:
            await record_system_event(s, bus, "trading-engine", "mode_refused", str(e), severity=Severity.CRITICAL)
        log.error("trading_mode_refused", reason=str(e))
        while not await sleep_or_stop(stop, 10):
            await bus.heartbeat("trading-engine", {"mode": settings.trading_mode.value, "refused": str(e)})
        return
    if hasattr(adapter, "load_markets"):
        while not stop.is_set():
            try:
                await adapter.load_markets()
                break
            except Exception as e:
                log.warning("exchange_markets_unavailable", error=f"{type(e).__name__}: {e}")
                if await sleep_or_stop(stop, 10):
                    return
    engine = TradingEngine(settings, bus, reader, adapter)
    try:
        await engine.run(stop)
    finally:
        await adapter.close()


if __name__ == "__main__":
    run_service("trading-engine", main)
