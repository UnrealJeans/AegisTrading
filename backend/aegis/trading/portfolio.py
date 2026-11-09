"""Portfolio accounting derived from the position ledger (the engine's source of truth)."""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import RiskParams
from aegis.core.timeutil import day_start
from aegis.db.models import ApprovedStatus, ApprovedTrade, PortfolioSnapshot, Position, PositionStatus, Side
from aegis.risk.engine import D0, PortfolioState

LIVE_POSITION_STATES = (PositionStatus.OPEN, PositionStatus.CLOSING)


def unrealized_pnl(p: Position, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Mark-to-market net of the entry fee already paid and the estimated exit fee."""
    return (price - p.entry_price) * p.quantity - p.entry_fee - price * p.quantity * fee_rate


async def compute_portfolio(
    session: AsyncSession, params: RiskParams, mode: str, tickers: dict[str, dict[str, Any]], *, stale_after: float = 60.0,
) -> PortfolioState:
    open_pos = (await session.execute(
        select(Position).where(Position.mode == mode, Position.status.in_(LIVE_POSITION_STATES))
    )).scalars().all()
    realized = (await session.execute(
        select(func.coalesce(func.sum(Position.realized_pnl), 0)).where(Position.mode == mode, Position.status == PositionStatus.CLOSED)
    )).scalar_one()
    realized = Decimal(str(realized))

    cash = params.starting_capital + realized
    value = unreal = D0
    stale: list[str] = []
    now = time.time()
    for p in open_pos:
        cash -= p.entry_price * p.quantity + p.entry_fee
        t = tickers.get(p.symbol) or {}
        if t.get("last") and now - float(t.get("ts") or 0) <= stale_after:
            price = Decimal(str(t["last"]))
        else:  # never invent a mark: fall back to cost and flag it
            price = p.entry_price
            stale.append(p.symbol)
        value += price * p.quantity
        unreal += unrealized_pnl(p, price, params.fee_rate)
    equity = cash + value

    pending = (await session.execute(
        select(ApprovedTrade).where(ApprovedTrade.status.in_([ApprovedStatus.PENDING, ApprovedStatus.EXECUTING]), ApprovedTrade.side == Side.BUY)
    )).scalars().all()
    reserved = sum((a.quantity * a.entry_price for a in pending), D0)

    today = day_start()
    prev = (await session.execute(
        select(PortfolioSnapshot.equity).where(PortfolioSnapshot.mode == mode, PortfolioSnapshot.created_at < today)
        .order_by(PortfolioSnapshot.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if prev is None:  # first trading day: earliest snapshot of today, else starting capital
        prev = (await session.execute(
            select(PortfolioSnapshot.equity).where(PortfolioSnapshot.mode == mode).order_by(PortfolioSnapshot.created_at.asc()).limit(1)
        )).scalar_one_or_none()
    day_start_equity = Decimal(str(prev)) if prev is not None else params.starting_capital
    peak = (await session.execute(select(func.max(PortfolioSnapshot.equity)).where(PortfolioSnapshot.mode == mode))).scalar_one_or_none()
    peak_equity = max(Decimal(str(peak)) if peak is not None else params.starting_capital, equity, params.starting_capital)
    drawdown = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else D0

    return PortfolioState(
        starting_capital=params.starting_capital, cash=cash, positions_value=value, equity=equity, realized_pnl=realized,
        unrealized_pnl=unreal, day_start_equity=day_start_equity, daily_pnl=equity - day_start_equity,
        peak_equity=peak_equity, drawdown_pct=drawdown, exposure=value + reserved,
        open_positions=len(open_pos) + len(pending),
        open_symbols=[p.symbol for p in open_pos] + [a.symbol for a in pending], stale_marks=stale,
    )


async def max_drawdown_pct(session: AsyncSession, mode: str) -> Decimal:
    """Largest historical peak-to-trough decline across stored snapshots."""
    rows = (await session.execute(
        select(PortfolioSnapshot.equity).where(PortfolioSnapshot.mode == mode).order_by(PortfolioSnapshot.created_at.asc())
    )).scalars().all()
    peak = worst = D0
    for e in rows:
        e = Decimal(str(e))
        peak = max(peak, e)
        if peak > 0:
            worst = max(worst, (peak - e) / peak * 100)
    return worst
