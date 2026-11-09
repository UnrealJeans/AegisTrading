"""Agent orchestration service: runs LangGraph cycles on a schedule and on operator request."""
from __future__ import annotations

import asyncio
import time

from aegis.api.app import RUN_CYCLE_KEY
from aegis.config import Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.llm.providers import LLMRouter
from aegis.marketdata.service import MarketDataReader
from aegis.orchestration.runner import CycleRunner
from aegis.services.runtime import run_service, sleep_or_stop

log = get_logger(__name__)


async def main(settings: Settings, bus: Bus, stop: asyncio.Event) -> None:
    router = LLMRouter(settings, bus)
    runner = CycleRunner(settings, bus, MarketDataReader(settings, bus), router)
    await runner.recover()
    log.info("orchestrator_ready", providers=router.available(), interval=settings.cycle_interval_seconds)
    next_run = time.time() + 30  # let market data warm up after a cold start
    while not stop.is_set():
        await bus.heartbeat("orchestrator", {"providers": router.available()})
        manual = await bus.r.getdel(RUN_CYCLE_KEY)
        if manual or time.time() >= next_run:
            try:
                result = await runner.run_cycle("manual" if manual else "schedule")
                log.info("cycle_result", **{k: v for k, v in result.items() if k not in ("results", "scout")})
            except Exception:
                log.exception("cycle_crashed")
            next_run = time.time() + settings.cycle_interval_seconds
        if await sleep_or_stop(stop, 2):
            break
    await router.close()


if __name__ == "__main__":
    run_service("orchestrator", main)
