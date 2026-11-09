from __future__ import annotations

from decimal import Decimal as D

import pytest

from aegis.config import RiskParams
from aegis.risk.engine import (
    MarketContext,
    PortfolioState,
    ProposalInput,
    evaluate_entry,
    floor_to_step,
    limit_breaches,
    risk_reward,
)
from tests.conftest import make_settings

PARAMS = RiskParams.from_settings(make_settings())


def portfolio(**kw) -> PortfolioState:
    base = dict(starting_capital=D(1000), cash=D(1000), equity=D(1000), day_start_equity=D(1000), peak_equity=D(1000))
    base.update(kw)
    return PortfolioState(**base)


def market(**kw) -> MarketContext:
    base = dict(last_price=D(100), price_is_fresh=True, spread_bps=D(2), quote_volume_24h=D("1e9"), qty_step=D("0.001"), min_qty=D("0.001"), min_notional=D(5))
    base.update(kw)
    return MarketContext(**base)


def proposal(**kw) -> ProposalInput:
    base = dict(symbol="BTC/USDT", side="BUY", entry_price=D(100), stop_loss=D(98), take_profit=D(106))
    base.update(kw)
    return ProposalInput(**base)


def failed(d) -> set[str]:
    return {c.name for c in d.checks if not c.passed}


def test_defaults_match_specification():
    assert (PARAMS.starting_capital, PARAMS.risk_per_trade_pct, PARAMS.max_daily_loss_pct) == (D(1000), D(1), D(3))
    assert (PARAMS.max_exposure_pct, PARAMS.max_open_positions, PARAMS.min_risk_reward) == (D(30), 5, D(2))


def test_position_sizing_risks_one_percent_including_costs():
    # 5% stop so the 1% risk rule (not the 30% exposure cap) is the binding constraint
    d = evaluate_entry(proposal(stop_loss=D(95), take_profit=D(115)), market(), portfolio(), PARAMS, trading_allowed=True)
    assert d.approved, d.reasons
    assert d.risk_amount == D(10)
    # modelled loss at the stop (price move + both fees + slippage) never exceeds the 1% budget
    assert d.loss_at_stop <= D(10) and d.loss_at_stop > D("9.9")
    eff_entry = D(100) * D("1.0005")
    per_unit = (eff_entry - D(95) * D("0.9995")) + D("0.001") * (eff_entry + D(95) * D("0.9995"))
    assert d.quantity == floor_to_step(D(10) / per_unit, D("0.001"))
    assert d.notional < D(300)


def test_size_is_capped_by_exposure_limit_and_cash():
    # tight stop -> risk sizing wants ~ $1,990 notional; the 30% exposure cap must win
    d = evaluate_entry(proposal(stop_loss=D("99.5"), take_profit=D(102)), market(), portfolio(), PARAMS, trading_allowed=True)
    assert d.approved and d.notional <= D(300) and d.notional > D(295)
    d2 = evaluate_entry(proposal(stop_loss=D("99.5"), take_profit=D(102)), market(), portfolio(exposure=D(250)), PARAMS, trading_allowed=True)
    assert d2.approved and d2.notional <= D(50)
    d3 = evaluate_entry(proposal(), market(), portfolio(exposure=D(300)), PARAMS, trading_allowed=True)
    assert not d3.approved and "position_size" in failed(d3)


def test_requested_quantity_can_only_shrink():
    full = evaluate_entry(proposal(), market(), portfolio(), PARAMS, trading_allowed=True)
    smaller = evaluate_entry(proposal(), market(), portfolio(), PARAMS, trading_allowed=True, requested_quantity=full.quantity / 2)
    bigger = evaluate_entry(proposal(), market(), portfolio(), PARAMS, trading_allowed=True, requested_quantity=full.quantity * 10)
    assert smaller.quantity <= full.quantity / 2 and bigger.quantity == full.quantity


@pytest.mark.parametrize("kw,check", [
    (dict(take_profit=D(103)), "risk_reward"),                 # 1.5:1
    (dict(stop_loss=D(101)), "price_geometry"),
    (dict(side="SELL"), "spot_long_only"),
    (dict(stop_loss=D("99.95"), take_profit=D(101)), "stop_distance"),
    (dict(stop_loss=D(70), take_profit=D(200)), "stop_distance"),
    (dict(entry_price=D(102), stop_loss=D(100), take_profit=D(110)), "entry_near_market"),
])
def test_proposal_validation_rejections(kw, check):
    d = evaluate_entry(proposal(**kw), market(), portfolio(), PARAMS, trading_allowed=True)
    assert not d.approved and check in failed(d) and d.quantity == 0


@pytest.mark.parametrize("mkw,check", [
    (dict(price_is_fresh=False), "market_data_fresh"),
    (dict(last_price=None), "market_data_fresh"),
    (dict(spread_bps=D(80)), "spread"),
    (dict(asset_active=False), "asset_tradable"),
    (dict(min_notional=D(100000)), "min_notional"),
])
def test_market_condition_rejections(mkw, check):
    d = evaluate_entry(proposal(), market(**mkw), portfolio(), PARAMS, trading_allowed=True)
    assert not d.approved and check in failed(d)


def test_liquidity_cap_limits_size_to_share_of_volume():
    d = evaluate_entry(proposal(stop_loss=D("99.5"), take_profit=D(102)), market(quote_volume_24h=D(100000)), portfolio(), PARAMS, trading_allowed=True)
    assert d.approved and d.notional <= D(100)


def test_portfolio_limits():
    assert "trading_enabled" in failed(evaluate_entry(proposal(), market(), portfolio(), PARAMS, trading_allowed=False))
    at_max = portfolio(open_positions=5, open_symbols=["A", "B", "C", "D", "E"])
    assert "max_positions" in failed(evaluate_entry(proposal(), market(), at_max, PARAMS, trading_allowed=True))
    dup = portfolio(open_positions=1, open_symbols=["BTC/USDT"])
    assert "no_duplicate_symbol" in failed(evaluate_entry(proposal(), market(), dup, PARAMS, trading_allowed=True))
    # at execution time the trade's own reservation is not a duplicate
    own = portfolio(open_positions=1, open_symbols=["BTC/USDT"], exposure=D(200))
    assert evaluate_entry(proposal(), market(), own, PARAMS, trading_allowed=True, requested_quantity=D(2), exclude_self=True).approved


def test_daily_loss_limit_reached_blocks_trading():
    lost = portfolio(cash=D(970), equity=D(970), daily_pnl=D(-30))
    assert lost.daily_loss_pct == D(3)
    assert limit_breaches(lost, PARAMS)
    d = evaluate_entry(proposal(), market(), lost, PARAMS, trading_allowed=True)
    assert not d.approved and "portfolio_limit" in failed(d)
    # 2.5% down: one more full-risk loss would cross 3% -> headroom check blocks it
    near = portfolio(cash=D(975), equity=D(975), daily_pnl=D(-25))
    d2 = evaluate_entry(proposal(), market(), near, PARAMS, trading_allowed=True)
    assert not limit_breaches(near, PARAMS) and "daily_loss_headroom" in failed(d2)


def test_drawdown_limit():
    dd = portfolio(cash=D(890), equity=D(890), peak_equity=D(1000), drawdown_pct=D(11), day_start_equity=D(890))
    assert any("drawdown" in b for b in limit_breaches(dd, PARAMS))
    assert not evaluate_entry(proposal(), market(), dd, PARAMS, trading_allowed=True).approved


def test_helpers_and_param_bounds():
    assert floor_to_step(D("1.23456"), D("0.01")) == D("1.23")
    assert floor_to_step(D("-1"), D("0.01")) == 0
    assert risk_reward(D(100), D(98), D(106)) == D(3)
    with pytest.raises(ValueError):
        RiskParams(**{**PARAMS.model_dump(), "risk_per_trade_pct": D(25)})  # UI/agents cannot loosen beyond hard bounds
    with pytest.raises(ValueError):
        RiskParams(**{**PARAMS.model_dump(), "min_risk_reward": D("0.5")})
