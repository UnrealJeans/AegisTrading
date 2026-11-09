"""Shared deterministic context builders used by both orchestration and the trading engine."""
from __future__ import annotations

import time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import RiskParams, Settings
from aegis.core.bus import Bus
from aegis.core.state import get_risk_params
from aegis.marketdata.service import MarketDataReader
from aegis.risk.engine import MarketContext, PortfolioState
from aegis.trading.portfolio import compute_portfolio


def _dec(v: object) -> Decimal | None:
    return None if v is None else Decimal(str(v))


async def build_market_context(reader: MarketDataReader, symbol: str) -> MarketContext:
    t = await reader.bus.get_ticker(symbol) or {}
    asset = await reader.get_asset(symbol)
    fresh = bool(t.get("last")) and time.time() - float(t.get("ts") or 0) <= reader.settings.stale_ticker_seconds
    bid, ask = _dec(t.get("bid")), _dec(t.get("ask"))
    spread = (ask - bid) / ((ask + bid) / 2) * 10000 if bid and ask and bid > 0 and ask >= bid else None
    return MarketContext(
        last_price=_dec(t.get("last")), price_is_fresh=fresh, spread_bps=spread, quote_volume_24h=_dec(t.get("quote_volume")),
        qty_step=asset.qty_step if asset else None, min_qty=asset.min_qty if asset else None,
        min_notional=asset.min_notional if asset else None, asset_active=bool(asset and asset.active),
    )


async def load_portfolio(session: AsyncSession, settings: Settings, bus: Bus) -> tuple[PortfolioState, RiskParams]:
    params = await get_risk_params(session, settings)
    tickers = await bus.get_tickers()
    portfolio = await compute_portfolio(session, params, settings.trading_mode.value, tickers, stale_after=settings.stale_ticker_seconds * 2)
    return portfolio, params
