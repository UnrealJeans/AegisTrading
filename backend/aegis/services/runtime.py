"""Shared process lifecycle: logging, dependency wait, signal handling, graceful shutdown."""
from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable

from sqlalchemy import text

from aegis.config import Settings, get_settings
from aegis.core.bus import Bus, get_bus
from aegis.core.logging import configure_logging, get_logger
from aegis.db.session import dispose_engine, init_engine, session_scope

log = get_logger(__name__)


async def wait_for_dependencies(bus: Bus, attempts: int = 60) -> None:
    for i in range(attempts):
        try:
            async with session_scope() as s:
                await s.execute(text("select 1"))
            if await bus.ping():
                return
        except Exception as e:
            log.warning("waiting_for_dependencies", attempt=i + 1, error=f"{type(e).__name__}: {e}")
        await asyncio.sleep(2)
    raise RuntimeError("database/redis not reachable")


def run_service(name: str, main: Callable[[Settings, Bus, asyncio.Event], Awaitable[None]]) -> None:
    settings = get_settings()
    configure_logging(name, settings.log_level, settings.log_json)

    async def runner() -> None:
        init_engine(settings.database_url)
        bus = get_bus()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await wait_for_dependencies(bus)
        log.info("service_started", mode=settings.trading_mode.value)
        try:
            await main(settings, bus, stop)
        finally:
            log.info("service_stopping")
            await bus.close()
            await dispose_engine()

    asyncio.run(runner())


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> bool:
    """Returns True if shutdown was requested."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
        return True
    except TimeoutError:
        return False


async def supervise(stop: asyncio.Event, name: str, fn: Callable[[], Awaitable[object]], interval: float, *, on_error: Callable[[Exception], Awaitable[None]] | None = None) -> None:
    """Run `fn` forever every `interval` seconds; one failing iteration never kills the service."""
    while not stop.is_set():
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("task_failed", task=name)
            if on_error:
                try:
                    await on_error(e)
                except Exception:
                    log.exception("on_error_failed", task=name)
        if await sleep_or_stop(stop, interval):
            return
