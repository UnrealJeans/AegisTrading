"""Trading engine: the only component that sends orders.

Safety properties
* Never acts on LLM output directly: every approved trade is re-validated by the deterministic risk engine against
  a fresh price immediately before execution.
* Idempotent: one deterministic client order id per intent, persisted (UNIQUE) *before* the order is sent.
  An order whose outcome is unknown is resolved by lookup on the venue and is never blindly resent.
* Restart-safe: all state lives in PostgreSQL; `recover()` resolves anything left in flight by a crash.
* Fail-closed: stale data, venue errors and ledger mismatches suspend new entries. Protective exits keep working.
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import RiskParams, Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.core.state import (
    Control,
    TradingStatus,
    get_control,
    get_risk_params,
    record_risk_event,
    record_system_event,
    set_control,
    suspend_trading,
)
from aegis.core.timeutil import utcnow
from aegis.db.models import (
    ApprovedStatus,
    ApprovedTrade,
    Order,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    PositionStatus,
    ProposalStatus,
    RejectedTrade,
    Severity,
    Side,
    TradeProposal,
)
from aegis.db.session import session_scope
from aegis.marketdata.service import MarketDataReader, StaleDataError
from aegis.risk.engine import PortfolioState, ProposalInput, limit_breaches
from aegis.risk.engine import evaluate_entry as risk_evaluate_entry
from aegis.trading.adapters.base import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeRejected,
    ExchangeTimeout,
    OrderRequest,
    OrderResult,
)
from aegis.trading.context import build_market_context, load_portfolio

log = get_logger(__name__)
RECONCILE_TOLERANCE = Decimal("0.002")
MAX_EXIT_ATTEMPTS = 5


def entry_client_id(approved_trade_id: int) -> str:
    return f"aegis-e-{approved_trade_id}"


def exit_client_id(position_id: int, attempt: int) -> str:
    return f"aegis-x-{position_id}-{attempt}"


class TradingEngine:
    def __init__(self, settings: Settings, bus: Bus, reader: MarketDataReader, adapter: ExchangeAdapter) -> None:
        self.settings, self.bus, self.reader, self.adapter = settings, bus, reader, adapter
        self.mode = settings.trading_mode.value
        self._warned: dict[str, float] = {}
        self._last_snapshot = 0.0
        self._last_reconcile = 0.0

    # ================================================================================================
    # main loop
    # ================================================================================================
    async def tick(self) -> None:
        async with session_scope() as s:
            control = await get_control(s)
            params = await get_risk_params(s, self.settings)
        if hasattr(self.adapter, "params"):
            self.adapter.params = params  # paper fills follow runtime-edited fee/slippage
        await self.resolve_unknown_orders()
        await self.monitor_positions(control)
        await self.process_approved()
        now = time.time()
        if now - self._last_snapshot >= 60:
            await self.snapshot()
        if now - self._last_reconcile >= 300:
            await self.reconcile()

    async def run(self, stop: asyncio.Event, interval: float = 2.0) -> None:
        await self.recover()
        while not stop.is_set():
            try:
                await self.tick()
                await self.bus.heartbeat("trading-engine", {"mode": self.mode, "adapter": self.adapter.name})
            except Exception as e:
                log.exception("engine_tick_failed")
                await self._critical("engine_error", f"trading engine loop error: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass

    # ================================================================================================
    # recovery
    # ================================================================================================
    async def recover(self) -> None:
        """Run at startup. Resolves orders in flight and re-arms monitoring of open positions."""
        resolved = await self.resolve_unknown_orders(min_age=0.0)
        async with session_scope() as s:
            # approvals claimed (EXECUTING) by a process that died before it persisted an order: nothing was sent
            stuck = (await s.execute(
                select(ApprovedTrade).where(ApprovedTrade.status == ApprovedStatus.EXECUTING)
                .where(~select(Order.id).where(Order.approved_trade_id == ApprovedTrade.id).exists())
            )).scalars().all()
            for a in stuck:
                a.status, a.status_reason = ApprovedStatus.FAILED, "engine restarted before the order was created"
            open_count = (await s.execute(select(func.count()).select_from(Position).where(
                Position.mode == self.mode, Position.status.in_([PositionStatus.OPEN, PositionStatus.CLOSING])))).scalar_one()
            await record_system_event(s, self.bus, "trading-engine", "startup_recovery",
                                      f"recovered: {resolved} in-flight orders resolved, {len(stuck)} stuck approvals failed, {open_count} open positions under management",
                                      detail={"mode": self.mode, "adapter": self.adapter.name})
        await self.reconcile()

    async def resolve_unknown_orders(self, min_age: float = 20.0) -> int:
        """Orders still SUBMITTED have an unknown outcome (timeout or crash). Ask the venue by client order id."""
        async with session_scope() as s:
            ids = [(o.id, o.symbol, o.client_order_id) for o in (await s.execute(
                select(Order).where(Order.status == OrderStatus.SUBMITTED, Order.mode == self.mode))).scalars()
                if (utcnow() - o.created_at).total_seconds() >= min_age]
        n = 0
        for order_id, symbol, cid in ids:
            try:
                result = await self.adapter.fetch_order(symbol, cid)
            except ExchangeError as e:
                await self._warn(f"resolve:{cid}", "order_lookup_failed", f"cannot resolve order {cid}: {e}", symbol)
                continue
            async with session_scope() as s:
                order = await s.get(Order, order_id, with_for_update=True)
                if order is None or order.status != OrderStatus.SUBMITTED:
                    continue
                if result is None:
                    await self._apply_failure(s, order, "venue has no record of this order; it was never placed", definitive=True)
                elif result.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED):
                    continue  # still working on the venue
                else:
                    await self._apply_result(s, order, result)
                n += 1
        return n

    # ================================================================================================
    # entries
    # ================================================================================================
    async def process_approved(self) -> int:
        async with session_scope() as s:
            ids = list((await s.execute(select(ApprovedTrade.id).where(ApprovedTrade.status == ApprovedStatus.PENDING)
                                        .order_by(ApprovedTrade.id))).scalars())
        done = 0
        for aid in ids:
            try:
                done += 1 if await self.execute_approved(aid) else 0
            except Exception as e:
                log.exception("execute_approved_failed", approved_trade_id=aid)
                await self._critical("execution_error", f"unexpected error executing approved trade {aid}: {type(e).__name__}: {e}")
        return done

    async def execute_approved(self, approved_id: int) -> bool:
        """Claim -> re-validate -> persist intent -> send -> apply. Returns True if an order was filled."""
        # ---- phase 1: claim + validate + persist intent (one transaction) --------------------------
        async with session_scope() as s:
            a = (await s.execute(select(ApprovedTrade).where(ApprovedTrade.id == approved_id).with_for_update(skip_locked=True))).scalar_one_or_none()
            if a is None or a.status != ApprovedStatus.PENDING:
                return False  # someone else has it, or it was already handled
            if a.side == Side.SELL:
                pos = (await s.execute(select(Position).where(Position.symbol == a.symbol, Position.mode == self.mode,
                                                               Position.status == PositionStatus.OPEN))).scalar_one_or_none()
                a.status, a.executed_at = (ApprovedStatus.EXECUTED, utcnow()) if pos else (ApprovedStatus.REJECTED_AT_EXECUTION, None)
                a.status_reason = None if pos else "no open position to close"
                pos_id = pos.id if pos else None
            else:
                pos_id = None
                reject = await self._pre_trade_checks(s, a)
                if reject:
                    status, reasons = reject
                    a.status, a.status_reason = status, "; ".join(reasons)
                    await self._reject_proposal(s, a, reasons)
                    await self.bus.publish("execution", {"approved_trade_id": a.id, "symbol": a.symbol, "status": status, "reasons": reasons})
                    return False
                price = await self.reader.get_price(a.symbol)
                order = Order(client_order_id=entry_client_id(a.id), exchange=self.adapter.name, mode=self.mode, symbol=a.symbol, side=Side.BUY,
                              purpose="ENTRY", quantity=a.quantity, reference_price=price, status=OrderStatus.SUBMITTED, approved_trade_id=a.id)
                s.add(order)
                a.status = ApprovedStatus.EXECUTING
                await s.flush()
                order_id = order.id
        if pos_id is not None:
            return await self.close_position(pos_id, "AGENT_EXIT")
        if a.side == Side.SELL:
            return False
        # ---- phase 2: send (intent is durable; a crash from here on is resolved by client order id) ----
        return await self._send(order_id)

    async def _pre_trade_checks(self, s: AsyncSession, a: ApprovedTrade) -> tuple[ApprovedStatus, list[str]] | None:
        if utcnow() >= a.expires_at:
            return ApprovedStatus.EXPIRED, ["approval expired before execution"]
        control = await get_control(s)
        market = await build_market_context(self.reader, a.symbol)
        portfolio, params = await load_portfolio(s, self.settings, self.bus)
        decision = risk_evaluate_entry(
            ProposalInput(symbol=a.symbol, side=a.side, entry_price=a.entry_price, stop_loss=a.stop_loss, take_profit=a.take_profit),
            market, portfolio, params, trading_allowed=control.allows_new_entries, requested_quantity=a.quantity, exclude_self=True,
        )
        if not decision.approved:
            return ApprovedStatus.REJECTED_AT_EXECUTION, decision.reasons
        if decision.quantity < a.quantity:
            a.quantity = decision.quantity  # the engine may only shrink
        return None

    async def _reject_proposal(self, s: AsyncSession, a: ApprovedTrade, reasons: list[str]) -> None:
        prop = await s.get(TradeProposal, a.proposal_id)
        if prop is not None:
            prop.status = ProposalStatus.REJECTED
            exists = (await s.execute(select(RejectedTrade.id).where(RejectedTrade.proposal_id == prop.id))).scalar_one_or_none()
            if exists is None:
                s.add(RejectedTrade(proposal_id=prop.id, symbol=a.symbol, stage="execution", reasons=reasons))

    # ================================================================================================
    # order transport
    # ================================================================================================
    async def _send(self, order_id: int) -> bool:
        async with session_scope() as s:
            order = await s.get(Order, order_id)
            assert order is not None
            req = OrderRequest(client_order_id=order.client_order_id, symbol=order.symbol, side=Side(order.side),
                               quantity=order.quantity, reference_price=order.reference_price)
        try:
            result = await self.adapter.create_market_order(req)
        except ExchangeTimeout as e:
            # Unknown outcome: leave SUBMITTED. resolve_unknown_orders() will look it up; it is never resent.
            await self._warn(f"timeout:{req.client_order_id}", "exchange_timeout", f"order {req.client_order_id} outcome unknown: {e}", req.symbol,
                             severity=Severity.CRITICAL)
            return False
        except ExchangeRejected as e:
            async with session_scope() as s:
                order = await s.get(Order, order_id, with_for_update=True)
                assert order is not None
                await self._apply_failure(s, order, str(e), definitive=True)
            return False
        async with session_scope() as s:
            order = await s.get(Order, order_id, with_for_update=True)
            assert order is not None
            if order.status != OrderStatus.SUBMITTED:
                return False
            return await self._apply_result(s, order, result)

    async def _apply_result(self, s: AsyncSession, order: Order, r: OrderResult) -> bool:
        order.exchange_order_id, order.filled_quantity, order.avg_fill_price, order.fee = r.exchange_order_id, r.filled_quantity, r.avg_price, r.fee_quote
        if r.status != OrderStatus.FILLED or r.filled_quantity <= 0 or r.avg_price is None:
            await self._apply_failure(s, order, f"order ended {r.status} with fill {r.filled_quantity}", definitive=True)
            return False
        order.status = OrderStatus.FILLED
        if order.purpose == "ENTRY":
            a = await s.get(ApprovedTrade, order.approved_trade_id)
            assert a is not None
            pos = Position(symbol=order.symbol, mode=self.mode, status=PositionStatus.OPEN, approved_trade_id=a.id, quantity=r.net_quantity,
                           entry_price=r.avg_price, stop_loss=a.stop_loss, take_profit=a.take_profit, entry_fee=r.fee_quote)
            s.add(pos)
            a.status, a.executed_at = ApprovedStatus.EXECUTED, utcnow()
            await s.flush()
            order.position_id = pos.id
            prop = await s.get(TradeProposal, a.proposal_id)
            if prop is not None:
                prop.evidence = {**(prop.evidence or {}), "execution": {
                    "order_id": order.id, "client_order_id": order.client_order_id, "mode": self.mode, "venue": self.adapter.name,
                    "filled_quantity": str(r.filled_quantity), "avg_price": str(r.avg_price), "fee": str(r.fee_quote), "position_id": pos.id}}
            await self.bus.publish("execution", {"status": "FILLED", "purpose": "ENTRY", "symbol": order.symbol, "quantity": r.net_quantity,
                                                 "price": r.avg_price, "fee": r.fee_quote, "position_id": pos.id, "mode": self.mode})
            if pos.stop_loss >= r.avg_price:  # extreme slippage through the stop: flatten rather than hold an invalid position
                await record_risk_event(s, self.bus, "fill_through_stop", f"{order.symbol} filled at {r.avg_price}, at or below stop {pos.stop_loss}",
                                        severity=Severity.CRITICAL, symbol=order.symbol)
        else:
            pos = await s.get(Position, order.position_id, with_for_update=True)
            assert pos is not None
            sold = min(r.filled_quantity, pos.quantity)
            pos.exit_price, pos.exit_fee = r.avg_price, r.fee_quote
            pos.realized_pnl = (r.avg_price - pos.entry_price) * sold - pos.entry_fee - r.fee_quote
            pos.status, pos.close_reason, pos.closed_at = PositionStatus.CLOSED, order.close_reason, utcnow()
            if sold < pos.quantity * (1 - RECONCILE_TOLERANCE):
                await record_risk_event(s, self.bus, "partial_exit", f"{order.symbol}: sold {sold} of {pos.quantity}; remainder needs manual review",
                                        severity=Severity.CRITICAL, symbol=order.symbol)
            await self.bus.publish("execution", {"status": "FILLED", "purpose": "EXIT", "symbol": order.symbol, "quantity": sold, "price": r.avg_price,
                                                 "fee": r.fee_quote, "position_id": pos.id, "reason": order.close_reason, "realized_pnl": pos.realized_pnl,
                                                 "mode": self.mode})
        self._last_snapshot = 0.0  # refresh portfolio right away
        return True

    async def _apply_failure(self, s: AsyncSession, order: Order, reason: str, *, definitive: bool) -> None:
        order.status, order.error = OrderStatus.REJECTED if definitive else OrderStatus.FAILED, reason[:1000]
        if order.purpose == "ENTRY":
            a = await s.get(ApprovedTrade, order.approved_trade_id)
            if a is not None:
                a.status, a.status_reason = ApprovedStatus.FAILED, reason[:1000]
            await record_risk_event(s, self.bus, "entry_failed", f"entry order for {order.symbol} failed: {reason}", symbol=order.symbol)
        else:
            pos = await s.get(Position, order.position_id, with_for_update=True)
            if pos is not None and pos.status == PositionStatus.CLOSING:
                pos.status = PositionStatus.OPEN  # monitoring will try again with a new client order id
            attempts = (await s.execute(select(func.count()).select_from(Order).where(Order.position_id == order.position_id, Order.purpose == "EXIT"))).scalar_one()
            await record_risk_event(s, self.bus, "exit_failed", f"exit order for {order.symbol} failed (attempt {attempts}): {reason}",
                                    severity=Severity.CRITICAL, symbol=order.symbol)
            if await suspend_trading(s, f"exit order failed for {order.symbol}: {reason}"):
                await self.bus.publish("control", {"status": TradingStatus.SUSPENDED, "reason": "exit order failed"})
        await self.bus.publish("execution", {"status": order.status, "purpose": order.purpose, "symbol": order.symbol, "error": reason})

    # ================================================================================================
    # exits
    # ================================================================================================
    async def monitor_positions(self, control: Control | None = None) -> int:
        async with session_scope() as s:
            control = control or await get_control(s)
            positions = [(p.id, p.symbol, p.stop_loss, p.take_profit) for p in (await s.execute(
                select(Position).where(Position.mode == self.mode, Position.status == PositionStatus.OPEN))).scalars()]
        closed = 0
        liquidate = control.status == TradingStatus.EMERGENCY_STOP and control.liquidate
        for pid, symbol, stop, target in positions:
            try:
                price = await self.reader.get_price(symbol)
            except StaleDataError as e:
                await self._warn(f"stale:{symbol}", "stale_price_open_position", f"cannot evaluate stop/target for {symbol}: {e}", symbol, severity=Severity.CRITICAL)
                if not liquidate:
                    continue
                price = None
            reason = "EMERGENCY" if liquidate else "STOP_LOSS" if price is not None and price <= stop else "TAKE_PROFIT" if price is not None and price >= target else None
            if reason and await self.close_position(pid, reason):
                closed += 1
        return closed

    async def close_position(self, position_id: int, reason: str) -> bool:
        async with session_scope() as s:
            pos = (await s.execute(select(Position).where(Position.id == position_id).with_for_update(skip_locked=True))).scalar_one_or_none()
            if pos is None or pos.status != PositionStatus.OPEN:
                return False
            attempts = (await s.execute(select(func.count()).select_from(Order).where(Order.position_id == pos.id, Order.purpose == "EXIT"))).scalar_one()
            if attempts >= MAX_EXIT_ATTEMPTS:
                await self._warn(f"exit_giveup:{pos.id}", "exit_attempts_exhausted",
                                 f"{pos.symbol}: {attempts} exit attempts failed; manual intervention required", pos.symbol, severity=Severity.CRITICAL)
                return False
            t = await self.bus.get_ticker(pos.symbol) or {}
            ref = Decimal(str(t["last"])) if t.get("last") else pos.entry_price
            order = Order(client_order_id=exit_client_id(pos.id, attempts + 1), exchange=self.adapter.name, mode=self.mode, symbol=pos.symbol,
                          side=Side.SELL, purpose="EXIT", quantity=pos.quantity, reference_price=ref, status=OrderStatus.SUBMITTED,
                          position_id=pos.id, close_reason=reason)
            s.add(order)
            pos.status = PositionStatus.CLOSING
            await s.flush()
            order_id = order.id
        return await self._send(order_id)

    # ================================================================================================
    # portfolio, limits, reconciliation
    # ================================================================================================
    async def snapshot(self) -> PortfolioState:
        self._last_snapshot = time.time()
        async with session_scope() as s:
            portfolio, params = await load_portfolio(s, self.settings, self.bus)
            s.add(PortfolioSnapshot(mode=self.mode, equity=portfolio.equity, cash=portfolio.cash, positions_value=portfolio.positions_value,
                                    realized_pnl=portfolio.realized_pnl, unrealized_pnl=portfolio.unrealized_pnl,
                                    open_positions=len(portfolio.open_symbols), drawdown_pct=portfolio.drawdown_pct))
            await self._enforce_limits(s, portfolio, params)
        await self.bus.publish("portfolio", portfolio.model_dump(mode="json"), feed=False)
        return portfolio

    async def _enforce_limits(self, s: AsyncSession, portfolio: PortfolioState, params: RiskParams) -> None:
        breaches = limit_breaches(portfolio, params)
        if not breaches:
            return
        reason = "; ".join(breaches)
        if await suspend_trading(s, reason):
            await record_risk_event(s, self.bus, "limit_breach", f"new trading suspended: {reason}", severity=Severity.CRITICAL,
                                    detail={"equity": str(portfolio.equity), "daily_pnl": str(portfolio.daily_pnl), "drawdown_pct": str(portfolio.drawdown_pct)})
            await self.bus.publish("control", {"status": TradingStatus.SUSPENDED, "reason": reason})

    async def reconcile(self) -> list[str]:
        """Compare the venue's balances with our ledger. Any shortfall suspends trading (never auto-'fixes')."""
        self._last_reconcile = time.time()
        try:
            balances = await self.adapter.fetch_balances()
        except ExchangeError as e:
            await self._warn("reconcile", "reconcile_failed", f"could not fetch balances: {e}", None)
            return [f"balance fetch failed: {e}"]
        if balances is None:
            return []  # paper: the ledger is the account
        problems: list[str] = []
        async with session_scope() as s:
            positions = (await s.execute(select(Position).where(Position.mode == self.mode, Position.status.in_([PositionStatus.OPEN, PositionStatus.CLOSING])))).scalars().all()
            need: dict[str, Decimal] = {}
            for p in positions:
                base = p.symbol.split("/")[0]
                need[base] = need.get(base, Decimal(0)) + p.quantity
            for base, qty in need.items():
                have = balances.get(base, Decimal(0))
                if have < qty * (1 - RECONCILE_TOLERANCE):
                    problems.append(f"{base}: ledger holds {qty}, exchange balance is {have}")
            if problems:
                msg = "exchange balances do not cover ledger positions: " + "; ".join(problems)
                await record_risk_event(s, self.bus, "reconcile_mismatch", msg, severity=Severity.CRITICAL)
                if await suspend_trading(s, msg):
                    await self.bus.publish("control", {"status": TradingStatus.SUSPENDED, "reason": msg})
        return problems

    # ================================================================================================
    # helpers
    # ================================================================================================
    async def _warn(self, key: str, kind: str, message: str, symbol: str | None, *, severity: Severity = Severity.WARNING, every: float = 300.0) -> None:
        """Rate-limited risk event so a persistent condition does not flood the log."""
        now = time.time()
        if now - self._warned.get(key, 0.0) < every:
            return
        self._warned[key] = now
        async with session_scope() as s:
            await record_risk_event(s, self.bus, kind, message, severity=severity, symbol=symbol)

    async def _critical(self, kind: str, message: str) -> None:
        """Unexpected engine failure: record it and stop opening new positions until an operator resumes."""
        try:
            async with session_scope() as s:
                await record_system_event(s, self.bus, "trading-engine", kind, message, severity=Severity.CRITICAL)
                if await suspend_trading(s, message):
                    await self.bus.publish("control", {"status": TradingStatus.SUSPENDED, "reason": message})
        except Exception:
            log.exception("failed_to_record_critical")


async def operator_set_status(session: AsyncSession, bus: Bus, status: TradingStatus, *, by: str, reason: str = "", liquidate: bool = False) -> Control:
    c = await set_control(session, status, reason=reason, by=by, liquidate=liquidate)
    await record_system_event(session, bus, "api", "control_change", f"trading status set to {status} by {by}" + (f": {reason}" if reason else ""),
                              severity=Severity.WARNING if status == TradingStatus.EMERGENCY_STOP else Severity.INFO, detail=c.model_dump(mode="json"))
    await bus.publish("control", c.model_dump(mode="json"))
    return c
