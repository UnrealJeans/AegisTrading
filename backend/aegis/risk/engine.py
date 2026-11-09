"""Deterministic risk engine. Pure functions, no I/O, no LLM.

This is the authority on financial safety. It runs twice for every trade: once during orchestration (before the
Risk Manager agent is consulted) and again inside the trading engine immediately before an order is sent.
An LLM can only make the outcome stricter (veto / smaller size); it can never relax a check here.
"""
from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from pydantic import BaseModel, Field

from aegis.config import RiskParams

D0 = Decimal(0)
MAX_SPREAD_BPS = Decimal("30")
MAX_NOTIONAL_SHARE_OF_24H_VOLUME = Decimal("0.001")  # order must be <= 0.1% of 24h quote volume


class ProposalInput(BaseModel):
    symbol: str
    side: str
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal


class MarketContext(BaseModel):
    last_price: Decimal | None = None
    price_is_fresh: bool = False
    spread_bps: Decimal | None = None
    quote_volume_24h: Decimal | None = None
    qty_step: Decimal | None = None
    min_qty: Decimal | None = None
    min_notional: Decimal | None = None
    asset_active: bool = True


class PortfolioState(BaseModel):
    starting_capital: Decimal
    cash: Decimal
    positions_value: Decimal = D0
    equity: Decimal
    realized_pnl: Decimal = D0
    unrealized_pnl: Decimal = D0
    day_start_equity: Decimal
    daily_pnl: Decimal = D0
    peak_equity: Decimal
    drawdown_pct: Decimal = D0
    exposure: Decimal = D0            # market value of open positions + notional reserved by pending approvals
    open_positions: int = 0           # open positions + pending approvals
    open_symbols: list[str] = Field(default_factory=list)
    stale_marks: list[str] = Field(default_factory=list)

    @property
    def daily_loss_pct(self) -> Decimal:
        if self.day_start_equity <= 0:
            return D0
        return max(D0, -self.daily_pnl / self.day_start_equity * 100)

    @property
    def exposure_pct(self) -> Decimal:
        return self.exposure / self.equity * 100 if self.equity > 0 else D0


class Check(BaseModel):
    name: str
    passed: bool
    detail: str


class RiskDecision(BaseModel):
    approved: bool
    reasons: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    quantity: Decimal = D0
    notional: Decimal = D0
    risk_amount: Decimal = D0          # budget: equity * risk_per_trade
    loss_at_stop: Decimal = D0         # modelled loss incl. fees + slippage for `quantity`
    estimated_fees: Decimal = D0
    effective_entry: Decimal = D0
    risk_reward: Decimal = D0
    risk_reward_net: Decimal = D0


def floor_to_step(qty: Decimal, step: Decimal | None) -> Decimal:
    if qty <= 0:
        return D0
    if not step or step <= 0:
        return qty.quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def risk_reward(entry: Decimal, stop: Decimal, target: Decimal) -> Decimal:
    risk = abs(entry - stop)
    return abs(target - entry) / risk if risk > 0 else D0


def limit_breaches(portfolio: PortfolioState, params: RiskParams) -> list[str]:
    """Portfolio-level circuit breakers. Any breach suspends new trading."""
    out = []
    if portfolio.daily_loss_pct >= params.max_daily_loss_pct:
        out.append(f"daily loss {portfolio.daily_loss_pct:.2f}% reached the {params.max_daily_loss_pct}% limit")
    if portfolio.drawdown_pct >= params.max_drawdown_pct:
        out.append(f"drawdown {portfolio.drawdown_pct:.2f}% reached the {params.max_drawdown_pct}% limit")
    return out


def evaluate_entry(
    proposal: ProposalInput, market: MarketContext, portfolio: PortfolioState, params: RiskParams, *,
    trading_allowed: bool, requested_quantity: Decimal | None = None, exclude_self: bool = False,
) -> RiskDecision:
    """Validate and size a new long entry.

    `requested_quantity` may only shrink the computed size. `exclude_self` is used at execution time, when the
    trade being executed is already counted in `portfolio` as a pending reservation.
    """
    checks: list[Check] = []

    def check(name: str, ok: bool, detail: str) -> bool:
        checks.append(Check(name=name, passed=bool(ok), detail=detail))
        return bool(ok)

    def finish(d: RiskDecision | None = None) -> RiskDecision:
        d = d or RiskDecision(approved=False)
        d.checks = checks
        d.reasons = [f"{c.name}: {c.detail}" for c in checks if not c.passed]
        d.approved = not d.reasons
        if not d.approved:
            d.quantity = d.notional = D0
        return d

    entry, stop, target = proposal.entry_price, proposal.stop_loss, proposal.take_profit
    check("trading_enabled", trading_allowed, "trading is running" if trading_allowed else "new entries are not allowed in the current trading status")
    check("spot_long_only", proposal.side == "BUY", "BUY entry" if proposal.side == "BUY" else "only BUY entries are supported (spot, no shorting, no leverage)")
    check("asset_tradable", market.asset_active, "asset is active" if market.asset_active else "asset is not active on the exchange")
    geometry = entry > 0 and stop > 0 and target > 0 and stop < entry < target
    check("price_geometry", geometry, f"stop {stop} < entry {entry} < target {target}" if geometry else f"requires 0 < stop < entry < target, got stop={stop} entry={entry} target={target}")
    fresh = market.price_is_fresh and market.last_price is not None and market.last_price > 0
    check("market_data_fresh", fresh, "live price available" if fresh else "no fresh market price")
    if not (geometry and fresh):
        return finish()
    last = market.last_price
    assert last is not None

    deviation = abs(last - entry) / entry * 100
    check("entry_near_market", deviation <= params.max_entry_deviation_pct, f"market {last} deviates {deviation:.3f}% from proposed entry (limit {params.max_entry_deviation_pct}%)")

    slip = params.slippage_bps / Decimal(10000)
    eff_entry = last * (1 + slip)                      # market buy fills at/above last
    eff_stop_fill = stop * (1 - slip)                  # stop-out sells below the stop
    check("stop_below_market", stop < last, f"stop {stop} vs market {last}")
    check("target_above_market", target > eff_entry, f"target {target} vs effective entry {eff_entry:.8f}")
    if stop >= last or target <= eff_entry:
        return finish()

    stop_dist = (eff_entry - stop) / eff_entry * 100
    check("stop_distance", params.min_stop_distance_pct <= stop_dist <= params.max_stop_distance_pct,
          f"stop is {stop_dist:.2f}% from entry (allowed {params.min_stop_distance_pct}%..{params.max_stop_distance_pct}%)")

    rr = risk_reward(eff_entry, stop, target)
    per_unit_cost = params.fee_rate * (eff_entry + eff_stop_fill)
    per_unit_risk = (eff_entry - eff_stop_fill) + per_unit_cost
    per_unit_reward = (target - eff_entry) - params.fee_rate * (eff_entry + target)
    rr_net = per_unit_reward / per_unit_risk if per_unit_risk > 0 else D0
    check("risk_reward", rr >= params.min_risk_reward, f"risk/reward {rr:.2f} at market (minimum {params.min_risk_reward})")

    for breach in limit_breaches(portfolio, params):
        check("portfolio_limit", False, breach)

    open_count = portfolio.open_positions - (1 if exclude_self else 0)
    check("max_positions", open_count < params.max_open_positions, f"{open_count} open/pending of {params.max_open_positions} allowed")
    others = list(portfolio.open_symbols)
    if exclude_self and proposal.symbol in others:
        others.remove(proposal.symbol)
    check("no_duplicate_symbol", proposal.symbol not in others, "no existing exposure in this symbol" if proposal.symbol not in others else "a position or pending trade already exists for this symbol")

    if market.spread_bps is not None:
        check("spread", market.spread_bps <= MAX_SPREAD_BPS, f"spread {market.spread_bps:.1f} bps (limit {MAX_SPREAD_BPS})")

    # ---- sizing -------------------------------------------------------------------------------
    risk_budget = portfolio.equity * params.risk_per_trade_pct / 100
    qty = risk_budget / per_unit_risk if per_unit_risk > 0 else D0
    exposure_room = portfolio.equity * params.max_exposure_pct / 100 - portfolio.exposure
    cash_room = portfolio.cash / (1 + params.fee_rate)
    if exclude_self and requested_quantity is not None:  # our own reservation is inside `exposure`; give it back
        exposure_room += requested_quantity * proposal.entry_price
    max_notional = min(exposure_room, cash_room)
    if market.quote_volume_24h is not None:
        max_notional = min(max_notional, market.quote_volume_24h * MAX_NOTIONAL_SHARE_OF_24H_VOLUME)
    qty = min(qty, max(D0, max_notional) / eff_entry)
    if requested_quantity is not None:
        qty = min(qty, requested_quantity)
    qty = floor_to_step(qty, market.qty_step)
    notional = qty * eff_entry

    check("position_size", qty > 0, f"sized {qty} units (~{notional:.2f} quote)" if qty > 0 else f"no size available (exposure room {max(D0, exposure_room):.2f}, cash {portfolio.cash:.2f})")
    if qty > 0:
        if market.min_qty:
            check("min_quantity", qty >= market.min_qty, f"{qty} vs exchange minimum {market.min_qty}")
        if market.min_notional:
            check("min_notional", notional >= market.min_notional, f"{notional:.2f} vs exchange minimum {market.min_notional}")
    loss_at_stop = qty * per_unit_risk
    daily_room = portfolio.day_start_equity * params.max_daily_loss_pct / 100 + portfolio.daily_pnl
    if qty > 0:
        check("daily_loss_headroom", loss_at_stop <= daily_room, f"loss at stop {loss_at_stop:.2f} vs remaining daily loss allowance {max(D0, daily_room):.2f}")

    return finish(RiskDecision(
        approved=True, quantity=qty, notional=notional, risk_amount=risk_budget, loss_at_stop=loss_at_stop,
        estimated_fees=qty * params.fee_rate * (eff_entry + target), effective_entry=eff_entry,
        risk_reward=rr, risk_reward_net=rr_net,
    ))
