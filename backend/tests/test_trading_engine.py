from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal as D

import ccxt.async_support as ccxt
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aegis.config import RiskParams
from aegis.core.state import TradingStatus, get_control, set_control
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
    RiskEvent,
    Side,
    TradeProposal,
)
from aegis.db.session import session_scope
from aegis.trading.adapters import ModeNotAllowed, build_adapter
from aegis.trading.adapters.base import (
    ExchangeAdapter,
    ExchangeRejected,
    ExchangeTimeout,
    OrderRequest,
    OrderResult,
)
from aegis.trading.adapters.binance import BinanceSpotAdapter
from aegis.trading.adapters.paper import PaperAdapter
from aegis.trading.context import load_portfolio
from aegis.trading.engine import TradingEngine, entry_client_id
from tests.conftest import make_settings, seed_market, set_ticker, start_trading

SYM = "BTC/USDT"


async def approve(price: float, *, qty: str = "1", symbol: str = SYM, stop_pct: float = 0.97, tp_pct: float = 1.09, ttl: int = 300, side: Side = Side.BUY) -> int:
    p = D(str(price))
    async with session_scope() as s:
        prop = TradeProposal(symbol=symbol, side=side, entry_price=p, stop_loss=p * D(str(stop_pct)), take_profit=p * D(str(tp_pct)),
                             risk_reward=D(3), status=ProposalStatus.APPROVED, evidence={})
        s.add(prop)
        await s.flush()
        a = ApprovedTrade(proposal_id=prop.id, symbol=symbol, side=side, quantity=D(qty), entry_price=p, stop_loss=prop.stop_loss,
                          take_profit=prop.take_profit, risk_amount=D(10), expires_at=utcnow() + timedelta(seconds=ttl))
        s.add(a)
        await s.flush()
        return a.id


def paper_engine(settings, bus, reader) -> TradingEngine:
    return TradingEngine(settings, bus, reader, PaperAdapter(reader, RiskParams.from_settings(settings)))


class FakeVenue(ExchangeAdapter):
    """Scriptable venue with its own order book of record, to simulate timeouts/rejections/crashes."""
    name = "fake_venue"

    def __init__(self, price: float) -> None:
        self.price, self.orders, self.sent, self.mode = D(str(price)), {}, [], "ok"
        self.balances: dict[str, D] | None = None

    def _fill(self, req: OrderRequest) -> OrderResult:
        r = OrderResult(exchange_order_id=f"v-{len(self.orders) + 1}", status=OrderStatus.FILLED, filled_quantity=req.quantity,
                        net_quantity=req.quantity, avg_price=self.price, fee_quote=req.quantity * self.price * D("0.001"))
        self.orders[req.client_order_id] = r
        return r

    async def create_market_order(self, req: OrderRequest) -> OrderResult:
        self.sent.append(req.client_order_id)
        if self.mode == "timeout_lost":
            raise ExchangeTimeout("request timed out")           # never reached the venue
        if self.mode == "timeout_filled":
            self._fill(req)
            raise ExchangeTimeout("response lost")               # reached the venue, reply lost
        if self.mode == "insufficient":
            raise ExchangeRejected("InsufficientFunds: Account has insufficient balance")
        return self._fill(req)

    async def fetch_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        if self.mode == "down":
            raise ExchangeTimeout("venue unreachable")
        return self.orders.get(client_order_id)

    async def fetch_balances(self):
        return self.balances


async def one(model):
    async with session_scope() as s:
        return (await s.execute(select(model))).scalar_one()


async def all_(model):
    async with session_scope() as s:
        return (await s.execute(select(model).order_by(model.id))).scalars().all()


# ---------------------------------------------------------------------------------------------------
async def test_paper_entry_fills_at_real_price_with_slippage_and_fees(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    aid = await approve(last)
    eng = paper_engine(settings, bus, reader)
    assert await eng.execute_approved(aid) is True
    pos, order, a = await one(Position), await one(Order), await one(ApprovedTrade)
    expected = D(str(last)) * D("1.0005")
    assert pos.entry_price == pytest.approx(expected) and pos.quantity == D(1) and pos.status == PositionStatus.OPEN and pos.mode == "paper"
    assert pos.entry_fee == pytest.approx(expected * D("0.001"))
    assert (order.status, order.client_order_id, order.purpose, order.position_id) == (OrderStatus.FILLED, entry_client_id(aid), "ENTRY", pos.id)
    assert a.status == ApprovedStatus.EXECUTED
    prop = await one(TradeProposal)
    assert prop.evidence["execution"]["position_id"] == pos.id and prop.evidence["execution"]["mode"] == "paper"
    pf = await eng.snapshot()
    assert pf.cash == pytest.approx(D(1000) - expected - pos.entry_fee) and pf.equity < D(1000) and pf.open_symbols == [SYM]


async def test_duplicate_order_prevention(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    aid = await approve(last)
    eng = paper_engine(settings, bus, reader)
    results = [await eng.execute_approved(aid) for _ in range(3)]
    results += list(await asyncio.gather(*(eng.execute_approved(aid) for _ in range(3))))
    assert results.count(True) == 1
    assert len(await all_(Order)) == 1 and len(await all_(Position)) == 1
    # the database itself refuses a second order for the same intent, and a second position for the same approval
    with pytest.raises(IntegrityError):
        async with session_scope() as s:
            s.add(Order(client_order_id=entry_client_id(aid), exchange="x", mode="paper", symbol=SYM, side=Side.BUY, purpose="ENTRY",
                        quantity=D(1), reference_price=D(1)))
    with pytest.raises(IntegrityError):
        async with session_scope() as s:
            s.add(Position(symbol=SYM, mode="paper", approved_trade_id=aid, quantity=D(1), entry_price=D(1), stop_loss=D(1), take_profit=D(2)))


async def test_stop_loss_and_take_profit_exits_with_pnl(settings, bus, db, reader):
    last = await seed_market(bus)
    await seed_market(bus, "ETH/USDT")
    await start_trading()
    eng = paper_engine(settings, bus, reader)
    await eng.execute_approved(await approve(last))
    await eng.execute_approved(await approve(last, symbol="ETH/USDT"))
    assert await eng.monitor_positions() == 0  # price inside the bracket: nothing happens
    await set_ticker(bus, SYM, last * 0.96)           # through the stop
    await set_ticker(bus, "ETH/USDT", last * 1.10)    # through the target
    assert await eng.monitor_positions() == 2
    btc, eth = await all_(Position)
    assert (btc.status, btc.close_reason, eth.status, eth.close_reason) == (PositionStatus.CLOSED, "STOP_LOSS", PositionStatus.CLOSED, "TAKE_PROFIT")
    entry = D(str(last)) * D("1.0005")
    exit_ = D(str(last * 0.96)) * D("0.9995")
    assert btc.exit_price == pytest.approx(exit_)
    assert btc.realized_pnl == pytest.approx((exit_ - entry) - entry * D("0.001") - exit_ * D("0.001"))
    assert btc.realized_pnl < 0 < eth.realized_pnl
    pf = await eng.snapshot()
    assert pf.open_positions == 0 and pf.realized_pnl == btc.realized_pnl + eth.realized_pnl and pf.cash == pf.equity == D(1000) + pf.realized_pnl
    assert await eng.monitor_positions() == 0 and len(await all_(Order)) == 4  # closed positions are never sold twice


async def test_execution_time_revalidation(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    eng = paper_engine(settings, bus, reader)
    moved = await approve(last)
    await set_ticker(bus, SYM, last * 1.02)  # market ran away from the approved entry
    assert await eng.execute_approved(moved) is False
    await set_ticker(bus, SYM, last)
    expired = await approve(last, ttl=-1)
    assert await eng.execute_approved(expired) is False
    async with session_scope() as s:
        await set_control(s, TradingStatus.PAUSED, by="t")
    paused = await approve(last)
    assert await eng.execute_approved(paused) is False
    statuses = {a.id: (a.status, a.status_reason) for a in await all_(ApprovedTrade)}
    assert statuses[moved][0] == ApprovedStatus.REJECTED_AT_EXECUTION and "entry_near_market" in statuses[moved][1]
    assert statuses[expired][0] == ApprovedStatus.EXPIRED
    assert statuses[paused][0] == ApprovedStatus.REJECTED_AT_EXECUTION and "trading_enabled" in statuses[paused][1]
    assert await all_(Order) == [] and {r.stage for r in await all_(RejectedTrade)} == {"execution"}


async def test_exchange_timeout_is_resolved_by_lookup_never_resent(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    venue = FakeVenue(last)
    eng = TradingEngine(settings, bus, reader, venue)
    # (a) reply lost but the venue filled it
    venue.mode = "timeout_filled"
    a1 = await approve(last)
    assert await eng.execute_approved(a1) is False
    assert (await one(Order)).status == OrderStatus.SUBMITTED and await all_(Position) == []
    venue.mode = "down"
    assert await eng.resolve_unknown_orders(min_age=0) == 0  # venue unreachable: keep waiting, do not guess
    venue.mode = "ok"
    await eng.process_approved()
    assert venue.sent == [entry_client_id(a1)], "an order with unknown outcome must not be sent again"
    assert await eng.resolve_unknown_orders(min_age=0) == 1
    pos = await one(Position)
    assert pos.status == PositionStatus.OPEN and pos.approved_trade_id == a1 and (await one(Order)).status == OrderStatus.FILLED
    # (b) request never arrived
    await seed_market(bus, "ETH/USDT")
    venue.mode = "timeout_lost"
    a2 = await approve(last, symbol="ETH/USDT")
    await eng.execute_approved(a2)
    venue.mode = "ok"
    await eng.resolve_unknown_orders(min_age=0)
    orders = await all_(Order)
    assert orders[1].status == OrderStatus.REJECTED and "never placed" in orders[1].error
    assert (await all_(ApprovedTrade))[1].status == ApprovedStatus.FAILED and len(await all_(Position)) == 1
    assert any(e.kind == "exchange_timeout" for e in await all_(RiskEvent))


async def test_insufficient_balance_rejection(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    venue = FakeVenue(last)
    venue.mode = "insufficient"
    eng = TradingEngine(settings, bus, reader, venue)
    assert await eng.execute_approved(await approve(last)) is False
    order, a = await one(Order), await one(ApprovedTrade)
    assert order.status == OrderStatus.REJECTED and "insufficient balance" in order.error
    assert a.status == ApprovedStatus.FAILED and await all_(Position) == []
    assert (await one(RiskEvent)).kind == "entry_failed"
    # ledger-level: an approval far larger than the limits allow is shrunk by the engine before any order is sent
    venue.mode = "ok"
    await seed_market(bus, "ETH/USDT")
    big = await approve(last, qty="50", symbol="ETH/USDT")
    assert await eng.execute_approved(big) is True
    pos = await one(Position)
    assert pos.quantity < D(50) and pos.quantity * pos.entry_price <= D(301)


async def test_market_data_outage_blocks_entries_and_flags_open_positions(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    eng = paper_engine(settings, bus, reader)
    await eng.execute_approved(await approve(last))
    await set_ticker(bus, SYM, last * 0.5, age=600)  # last known price is 10 minutes old (and below the stop)
    assert await eng.monitor_positions() == 0, "must not trade on a stale price"
    assert (await one(Position)).status == PositionStatus.OPEN
    ev = await one(RiskEvent)
    assert ev.kind == "stale_price_open_position" and ev.severity == "CRITICAL"
    await seed_market(bus, "ETH/USDT")
    await set_ticker(bus, "ETH/USDT", last, age=600)
    blocked = await approve(last, symbol="ETH/USDT")
    assert await eng.execute_approved(blocked) is False
    assert "market_data_fresh" in (await all_(ApprovedTrade))[1].status_reason
    pf = await eng.snapshot()
    assert pf.stale_marks == [SYM]  # marked at cost, flagged, not invented
    await set_ticker(bus, SYM, last * 0.5)  # feed recovers -> the stop executes immediately
    assert await eng.monitor_positions() == 1


async def test_daily_loss_limit_suspends_trading(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    eng = paper_engine(settings, bus, reader)
    async with session_scope() as s:  # yesterday closed at 1000
        s.add(PortfolioSnapshot(mode="paper", equity=D(1000), cash=D(1000), positions_value=D(0), realized_pnl=D(0), unrealized_pnl=D(0),
                                created_at=utcnow() - timedelta(days=1)))
        s.add(Position(symbol="SOL/USDT", mode="paper", status=PositionStatus.CLOSED, quantity=D(1), entry_price=D(100), stop_loss=D(60),
                       take_profit=D(200), exit_price=D(65), realized_pnl=D(-35), close_reason="STOP_LOSS", closed_at=utcnow()))
    pf = await eng.snapshot()
    assert pf.daily_pnl == D(-35) and pf.daily_loss_pct == D("3.5")
    async with session_scope() as s:
        control = await get_control(s)
    assert control.status == TradingStatus.SUSPENDED and "daily loss" in control.reason
    assert any(e.kind == "limit_breach" for e in await all_(RiskEvent))
    assert await eng.execute_approved(await approve(last)) is False and await all_(Order) == []
    await eng.snapshot()
    assert sum(1 for e in await all_(RiskEvent) if e.kind == "limit_breach") == 1  # recorded once, not every minute


async def test_restart_during_open_position(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    eng1 = paper_engine(settings, bus, reader)
    await eng1.execute_approved(await approve(last))
    del eng1  # process dies
    eng2 = paper_engine(settings, bus, reader)
    await eng2.recover()
    pos = await one(Position)
    assert pos.status == PositionStatus.OPEN
    async with session_scope() as s:
        assert (await get_control(s)).status == TradingStatus.RUNNING
    await set_ticker(bus, SYM, last * 0.9)
    await eng2.tick()
    pos = await one(Position)
    assert pos.status == PositionStatus.CLOSED and pos.close_reason == "STOP_LOSS"


async def test_crash_between_intent_and_send_and_mid_exit(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    venue = FakeVenue(last)
    # crash after the order intent committed but before/while sending: order SUBMITTED, approval EXECUTING
    a1 = await approve(last)
    async with session_scope() as s:
        a = await s.get(ApprovedTrade, a1)
        a.status = ApprovedStatus.EXECUTING
        s.add(Order(client_order_id=entry_client_id(a1), exchange="fake_venue", mode="paper", symbol=SYM, side=Side.BUY, purpose="ENTRY",
                    quantity=D(1), reference_price=D(str(last)), approved_trade_id=a1))
    venue._fill(OrderRequest(client_order_id=entry_client_id(a1), symbol=SYM, side=Side.BUY, quantity=D(1), reference_price=D(str(last))))
    # crash after claiming an approval but before any order row existed
    a2 = await approve(last, symbol="ETH/USDT")
    async with session_scope() as s:
        (await s.get(ApprovedTrade, a2)).status = ApprovedStatus.EXECUTING
    eng = TradingEngine(settings, bus, reader, venue)
    await eng.recover()
    pos = await one(Position)
    assert pos.approved_trade_id == a1 and venue.sent == [], "recovery adopts the venue fill without sending anything"
    approvals = await all_(ApprovedTrade)
    assert approvals[0].status == ApprovedStatus.EXECUTED and approvals[1].status == ApprovedStatus.FAILED

    # crash mid-exit: position CLOSING with an exit order that never reached the venue
    async with session_scope() as s:
        p = await s.get(Position, pos.id)
        p.status = PositionStatus.CLOSING
        s.add(Order(client_order_id=f"aegis-x-{pos.id}-1", exchange="fake_venue", mode="paper", symbol=SYM, side=Side.SELL, purpose="EXIT",
                    quantity=p.quantity, reference_price=D(str(last)), position_id=p.id, close_reason="STOP_LOSS"))
    eng = TradingEngine(settings, bus, reader, venue)
    await eng.recover()
    assert (await one(Position)).status == PositionStatus.OPEN  # re-armed, not stuck in CLOSING
    async with session_scope() as s:
        await set_control(s, TradingStatus.RUNNING, by="operator")
    await set_ticker(bus, SYM, last * 0.9)
    assert await eng.monitor_positions() == 1
    assert venue.sent == [f"aegis-x-{pos.id}-2"] and (await one(Position)).status == PositionStatus.CLOSED


async def test_exit_failure_suspends_and_emergency_stop_liquidates(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    venue = FakeVenue(last)
    eng = TradingEngine(settings, bus, reader, venue)
    await eng.execute_approved(await approve(last))
    venue.mode = "insufficient"
    await set_ticker(bus, SYM, last * 0.9)
    assert await eng.monitor_positions() == 0
    async with session_scope() as s:
        assert (await get_control(s)).status == TradingStatus.SUSPENDED
    assert (await one(Position)).status == PositionStatus.OPEN
    venue.mode = "ok"
    await set_ticker(bus, SYM, last)  # back inside the bracket
    async with session_scope() as s:
        await set_control(s, TradingStatus.EMERGENCY_STOP, by="operator", liquidate=True)
    await eng.tick()
    pos = await one(Position)
    assert pos.status == PositionStatus.CLOSED and pos.close_reason == "EMERGENCY"
    assert await eng.execute_approved(await approve(last)) is False  # no new entries while stopped


async def test_agent_sell_closes_position_and_reconciliation_mismatch_suspends(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    venue = FakeVenue(last)
    eng = TradingEngine(settings, bus, reader, venue)
    await eng.execute_approved(await approve(last))
    venue.balances = {"BTC": D(1), "USDT": D(5000)}
    assert await eng.reconcile() == []
    venue.balances = {"BTC": D("0.4"), "USDT": D(5000)}
    problems = await eng.reconcile()
    assert problems and "BTC" in problems[0]
    async with session_scope() as s:
        assert (await get_control(s)).status == TradingStatus.SUSPENDED
    assert any(e.kind == "reconcile_mismatch" for e in await all_(RiskEvent))
    sell = await approve(last, side=Side.SELL, stop_pct=1.03, tp_pct=0.95)
    assert await eng.execute_approved(sell) is True  # exits are allowed while suspended
    pos = await one(Position)
    assert pos.status == PositionStatus.CLOSED and pos.close_reason == "AGENT_EXIT"
    orphan = await approve(last, side=Side.SELL, stop_pct=1.03, tp_pct=0.95)
    assert await eng.execute_approved(orphan) is False
    assert (await all_(ApprovedTrade))[-1].status == ApprovedStatus.REJECTED_AT_EXECUTION  # cannot short


async def test_pending_approvals_reserve_exposure(settings, bus, db, reader):
    last = await seed_market(bus)
    await approve(last, qty="2")
    async with session_scope() as s:
        pf, _ = await load_portfolio(s, settings, bus)
    assert pf.open_positions == 1 and pf.open_symbols == [SYM] and pf.exposure == pytest.approx(D(str(last)) * 2)


# ---- Binance adapter (CCXT boundary faked) and mode gating -------------------------------------------
class FakeCcxt:
    def __init__(self):
        self.calls, self.raise_ = [], None
        self.order = {"id": "777", "status": "closed", "side": "buy", "filled": 0.5, "average": 60000.0, "cost": 30000.0,
                      "fees": [{"currency": "BTC", "cost": 0.0005}]}

    async def create_order(self, symbol, type_, side, amount, price, params):
        self.calls.append((symbol, type_, side, amount, params))
        if self.raise_:
            raise self.raise_
        return self.order

    async def fetch_order(self, id_, symbol, params):
        self.calls.append(("fetch", symbol, params))
        if self.raise_:
            raise self.raise_
        return self.order

    async def fetch_balance(self):
        return {"total": {"BTC": 1.5, "USDT": 1000.0, "ETH": 0}}

    async def close(self):
        pass


async def test_binance_adapter_mapping_and_error_classification():
    s = make_settings(trading_mode="testnet", binance_testnet_api_key="k", binance_testnet_api_secret="s")
    fx = FakeCcxt()
    ad = BinanceSpotAdapter(s, exchange=fx)
    req = OrderRequest(client_order_id="aegis-e-1", symbol=SYM, side=Side.BUY, quantity=D("0.5"), reference_price=D(60000))
    r = await ad.create_market_order(req)
    assert fx.calls[0] == (SYM, "market", "buy", 0.5, {"newClientOrderId": "aegis-e-1"})
    assert (r.status, r.filled_quantity, r.avg_price) == (OrderStatus.FILLED, D("0.5"), D("60000.0"))
    assert r.net_quantity == D("0.4995") and r.fee_quote == D("30.0")  # base-asset fee reduces what we actually hold
    assert ad.name == "binance_testnet"
    found = await ad.fetch_order(SYM, "aegis-e-1")
    assert fx.calls[-1] == ("fetch", SYM, {"origClientOrderId": "aegis-e-1"}) and found.exchange_order_id == "777"
    assert await ad.fetch_balances() == {"BTC": D("1.5"), "USDT": D("1000.0")}
    for exc, expected in ((ccxt.RequestTimeout("t"), ExchangeTimeout), (ccxt.NetworkError("n"), ExchangeTimeout),
                          (ccxt.InsufficientFunds("i"), ExchangeRejected), (ccxt.InvalidOrder("MIN_NOTIONAL"), ExchangeRejected),
                          (ccxt.ExchangeError("???"), ExchangeTimeout)):
        fx.raise_ = exc
        with pytest.raises(expected):
            await ad.create_market_order(req)
    fx.raise_ = ccxt.OrderNotFound("nope")
    assert await ad.fetch_order(SYM, "x") is None


def test_real_ccxt_client_is_spot_testnet_only():
    s = make_settings(trading_mode="testnet", binance_testnet_api_key="k", binance_testnet_api_secret="s")
    ad = BinanceSpotAdapter(s)
    assert "testnet.binance.vision" in ad.ex.urls["api"]["public"] and "testnet.binance.vision" in ad.ex.urls["api"]["private"]
    assert ad.ex.options["defaultType"] == "spot" and ad.ex.options["fetchMarkets"] == ["spot"]


def test_trading_mode_gating(reader):
    params = RiskParams.from_settings(make_settings())
    assert isinstance(build_adapter(make_settings(), reader, params), PaperAdapter)
    assert make_settings().trading_mode == "paper" and make_settings().live_trading_enabled is False
    with pytest.raises(ModeNotAllowed, match="BINANCE_TESTNET_API_KEY"):
        build_adapter(make_settings(trading_mode="testnet"), reader, params)
    live = dict(trading_mode="live", binance_live_api_key="live-k", binance_live_api_secret="live-s")
    with pytest.raises(ModeNotAllowed, match="LIVE_TRADING_ENABLED"):
        build_adapter(make_settings(**live), reader, params)
    with pytest.raises(ModeNotAllowed, match="LIVE_TRADING_CONFIRMATION"):
        build_adapter(make_settings(**live, live_trading_enabled=True), reader, params)
    with pytest.raises(ModeNotAllowed, match="separate"):
        build_adapter(make_settings(trading_mode="live", live_trading_enabled=True, live_trading_confirmation="I-ACCEPT-REAL-MONEY-RISK"), reader, params)
    with pytest.raises(ModeNotAllowed, match="must differ"):
        build_adapter(make_settings(**live, live_trading_enabled=True, live_trading_confirmation="I-ACCEPT-REAL-MONEY-RISK",
                                    binance_testnet_api_key="live-k", binance_testnet_api_secret="x"), reader, params)
