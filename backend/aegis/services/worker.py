"""Background worker: market data (WebSocket + REST), candle history, news ingestion, staleness detection, notifications."""
from __future__ import annotations

import asyncio
import json
import time

import httpx
from sqlalchemy import select

from aegis.config import Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.core.state import get_notification_config, record_system_event
from aegis.db.models import Position, PositionStatus, Severity
from aegis.db.session import session_scope
from aegis.marketdata.news import NewsService
from aegis.marketdata.service import MarketDataService
from aegis.services.runtime import run_service, supervise

log = get_logger(__name__)


async def held_symbols(settings: Settings) -> list[str]:
    async with session_scope() as s:
        return list((await s.execute(select(Position.symbol).where(Position.mode == settings.trading_mode.value,
                                                                  Position.status.in_([PositionStatus.OPEN, PositionStatus.CLOSING])))).scalars())


class Notifier:
    """Pushes important events to Telegram and/or a webhook. Delivery failures are logged, never raised."""

    def __init__(self, settings: Settings, bus: Bus) -> None:
        self.settings, self.bus, self.http = settings, bus, httpx.AsyncClient(timeout=10)

    def wanted(self, cfg, event: dict) -> str | None:  # type: ignore[no-untyped-def]
        kind, d = event.get("type"), event.get("data", {})
        mode = self.settings.trading_mode.value.upper()
        if kind == "execution" and cfg.on_trade and d.get("status") == "FILLED":
            pnl = f" PnL {float(d['realized_pnl']):+.2f}" if d.get("realized_pnl") is not None else ""
            return f"[{mode}] {d.get('purpose')} {d.get('symbol')} qty {d.get('quantity')} @ {d.get('price')}{pnl} {d.get('reason') or ''}".strip()
        if kind == "risk_event" and cfg.on_risk_event and d.get("severity") in ("WARNING", "CRITICAL"):
            return f"[{mode}] RISK {d.get('severity')}: {d.get('message')}"
        if kind == "system_event" and cfg.on_system_error and d.get("severity") == "CRITICAL":
            return f"[{mode}] SYSTEM {d.get('service')}: {d.get('message')}"
        if kind == "control" and cfg.on_risk_event:
            return f"[{mode}] Trading status -> {d.get('status')} {d.get('reason') or ''}".strip()
        return None

    async def send(self, text: str) -> None:
        token, chat = self.settings.telegram_bot_token.get_secret_value(), self.settings.telegram_chat_id
        hook = self.settings.notify_webhook_url.get_secret_value()
        try:
            if token and chat:
                await self.http.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text[:4000]})
            if hook:
                await self.http.post(hook, json={"text": text[:4000], "source": "aegis"})
        except Exception as e:
            log.warning("notification_failed", error=type(e).__name__)

    async def run(self, stop: asyncio.Event) -> None:
        async for raw in self.bus.subscribe():
            if stop.is_set():
                return
            async with session_scope() as s:
                cfg = await get_notification_config(s)
            if not cfg.enabled:
                continue
            text = self.wanted(cfg, json.loads(raw))
            if text:
                await self.send(text)


async def main(settings: Settings, bus: Bus, stop: asyncio.Event) -> None:
    md = MarketDataService(settings, bus)
    news = NewsService(settings, bus)
    notifier = Notifier(settings, bus)
    state = {"unhealthy_since": 0.0, "alerted": False}

    async def on_md_error(e: Exception) -> None:
        async with session_scope() as s:
            await record_system_event(s, bus, "worker", "market_data_error", f"{type(e).__name__}: {e}"[:500], severity=Severity.WARNING)

    async def tickers() -> None:
        await md.refresh_tickers()

    async def candles() -> None:
        await md.ingest_universe_candles(await held_symbols(settings))

    async def status() -> None:
        st = await md.report_status()
        await bus.heartbeat("worker", {"market_data_healthy": st["healthy"], "universe": st["universe"]})
        if st["healthy"]:
            if state["alerted"]:
                async with session_scope() as s:
                    await record_system_event(s, bus, "worker", "market_data_recovered", "market data is fresh again")
            state.update(unhealthy_since=0.0, alerted=False)
        else:
            state["unhealthy_since"] = state["unhealthy_since"] or time.time()
            if not state["alerted"] and time.time() - state["unhealthy_since"] > 60:
                state["alerted"] = True
                async with session_scope() as s:
                    await record_system_event(s, bus, "worker", "market_data_outage", f"market data stale/missing: {st['reason']}", severity=Severity.CRITICAL)

    async def refresh_news() -> None:
        bases = {s.split("/")[0] for s in md.universe}
        if bases:
            n = await news.refresh(bases)
            log.info("news_refreshed", new_items=n)

    async def stream() -> None:
        await md.run_stream(stop, watch=lambda: held_symbols(settings))

    # markets must load first: the stream needs the exchange-id -> symbol map
    while not stop.is_set():
        try:
            await md.refresh_markets()
            await md.refresh_tickers()
            break
        except Exception as e:
            log.warning("initial_market_load_failed", error=f"{type(e).__name__}: {e}")
            await md.report_status()
            await bus.heartbeat("worker", {"market_data_healthy": False})
            await asyncio.sleep(10)

    tasks = [
        asyncio.create_task(supervise(stop, "stream", stream, 5)),
        asyncio.create_task(supervise(stop, "tickers", tickers, 60, on_error=on_md_error)),
        asyncio.create_task(supervise(stop, "candles", candles, 60, on_error=on_md_error)),
        asyncio.create_task(supervise(stop, "status", status, 10)),
        asyncio.create_task(supervise(stop, "markets", md.refresh_markets, 3600, on_error=on_md_error)),
        asyncio.create_task(supervise(stop, "news", refresh_news, 600)),
        asyncio.create_task(supervise(stop, "prune", md.prune_candles, 86400)),
        asyncio.create_task(supervise(stop, "notifier", lambda: notifier.run(stop), 5)),
    ]
    await stop.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await md.close()
    await news.close()
    await notifier.http.aclose()


if __name__ == "__main__":
    run_service("worker", main)
