"""LangGraph workflow coordinating the six agents.

    scan -> scout -> next_candidate -+-> news ------+-> analyst -> skeptic -> risk -> finalize -+
                         ^           +-> technical -+      |          |                 ^       |
                         |                                 +----------+-----------------+       |
                         +----------------------------------------------------------------------+

* news and technical run in parallel (fan-out / fan-in).
* conditional edges stop early (no candidates, NO_TRADE, skeptic rejection) so no LLM call is wasted.
* every failure path is fail-closed: a missing/failed review can only reject, never approve.
"""
from __future__ import annotations

import json
import operator
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from aegis.agents.base import Agent, AgentContext, AgentError
from aegis.agents.definitions import AGENTS
from aegis.agents.schemas import (
    AnalystInput,
    NewsInput,
    RiskManagerInput,
    ScoutInput,
    SkepticInput,
    TechnicalInput,
)
from aegis.config import Settings
from aegis.core.bus import Bus, dumps
from aegis.core.logging import get_logger
from aegis.core.state import get_control
from aegis.core.timeutil import utcnow
from aegis.db.models import (
    AgentMessage,
    ApprovedTrade,
    Position,
    PositionStatus,
    ProposalStatus,
    RejectedTrade,
    Side,
    TradeProposal,
)
from aegis.db.session import session_scope
from aegis.marketdata.indicators import compute_snapshot
from aegis.marketdata.news import news_context
from aegis.marketdata.service import MarketDataReader
from aegis.risk.engine import ProposalInput, RiskDecision, evaluate_entry, floor_to_step
from aegis.trading.context import build_market_context, load_portfolio

log = get_logger(__name__)


class CycleState(TypedDict, total=False):
    run_id: int
    candidates: list[dict[str, Any]]
    exit_reviews: list[dict[str, Any]]
    scout: dict[str, Any] | None
    queue: list[dict[str, Any]]
    current: dict[str, Any] | None
    market: dict[str, Any] | None
    news: dict[str, Any] | None
    technical: dict[str, Any] | None
    tech_reference: dict[str, Any] | None
    analyst: dict[str, Any] | None
    proposal_id: int | None
    skeptic: dict[str, Any] | None
    risk: dict[str, Any] | None
    outcome: dict[str, Any] | None
    results: Annotated[list[dict[str, Any]], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]


@dataclass
class Deps:
    settings: Settings
    bus: Bus
    reader: MarketDataReader
    ctx: AgentContext
    agents: dict[str, Agent] | None = None

    def agent(self, name: str) -> Agent:
        return (self.agents or AGENTS)[name]


def _j(obj: Any) -> Any:
    return json.loads(dumps(obj))


def _step(node: str, started: float, **detail: Any) -> list[dict[str, Any]]:
    return [{"node": node, "at": utcnow().isoformat(), "ms": int((time.monotonic() - started) * 1000), **detail}]


async def _message(run_id: int | None, sender: str, recipient: str, kind: str, symbol: str | None, summary: str, payload: Any, bus: Bus) -> None:
    async with session_scope() as s:
        m = AgentMessage(run_id=run_id, sender=sender, recipient=recipient, kind=kind, symbol=symbol, summary=summary[:500], payload=_j(payload))
        s.add(m)
        await s.flush()
        mid = m.id
    await bus.publish("agent_message", {"id": mid, "run_id": run_id, "from": sender, "to": recipient, "kind": kind, "symbol": symbol, "summary": summary[:500]})


def build_graph(deps: Deps) -> Any:
    st, bus, reader, ctx = deps.settings, deps.bus, deps.reader, deps.ctx

    # ---- 1. deterministic market scan ----------------------------------------------------------
    async def scan(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        found = await reader.scan()
        async with session_scope() as s:
            held = {p.symbol: p for p in (await s.execute(select(Position).where(
                Position.mode == st.trading_mode.value, Position.status == PositionStatus.OPEN))).scalars()}
            portfolio, params = await load_portfolio(s, st, bus)
        at_limit = portfolio.open_positions >= params.max_open_positions
        candidates, exits = [], []
        for c in found:
            if c.symbol in held:
                if c.breakout == "down":  # held asset breaking down -> ask the desk whether to exit early
                    exits.append({**c.model_dump(), "kind": "exit_review"})
                continue
            if at_limit or await bus.cache_get(f"cooldown:{c.symbol}"):
                continue  # no room for a new position, or analysed recently: do not spend LLM calls
            candidates.append(c.model_dump())
        return {"candidates": candidates, "exit_reviews": exits, "queue": [], "results": [],
                "trace": _step("scan", t0, found=len(found), candidates=len(candidates), exit_reviews=len(exits))}

    def after_scan(state: CycleState) -> str:
        return "scout" if state.get("candidates") or state.get("exit_reviews") else END

    # ---- 2. Scout ------------------------------------------------------------------------------
    async def scout(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        queue: list[dict[str, Any]] = list(state.get("exit_reviews") or [])
        by_symbol = {c["symbol"]: c for c in state["candidates"]}
        out: dict[str, Any] | None = None
        error = None
        try:
            res = await deps.agent("scout").run(ScoutInput(candidates=state["candidates"], max_picks=st.max_candidates_per_cycle), ctx)
            out = res.output.model_dump(mode="json")
            for p in res.output.picks:
                queue.append({**by_symbol[p.symbol], "kind": "entry", "pick": p.model_dump(mode="json")})
                await _message(ctx.run_id, "scout", "analyst", "opportunity", p.symbol, p.thesis, p, bus)
        except AgentError as e:
            error = str(e)
        return {"scout": out, "queue": queue, "trace": _step("scout", t0, picks=len(queue), error=error)}

    # ---- 3. per-candidate loop -----------------------------------------------------------------
    async def next_candidate(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        queue = list(state.get("queue") or [])
        reset = {"news": None, "technical": None, "tech_reference": None, "analyst": None, "proposal_id": None,
                 "skeptic": None, "risk": None, "outcome": None, "market": None}
        if not queue:
            return {**reset, "current": None, "queue": []}
        current = queue.pop(0)
        symbol = current["symbol"]
        market = await build_market_context(reader, symbol)
        outcome = None
        if not market.price_is_fresh:
            outcome = {"status": "skipped", "stage": "market_data", "reasons": ["no fresh price for symbol"]}
        return {**reset, "current": current, "queue": queue, "market": market.model_dump(mode="json"), "outcome": outcome,
                "trace": _step("next_candidate", t0, symbol=symbol)}

    def after_next(state: CycleState) -> Any:
        if not state.get("current"):
            return END
        return "finalize" if state.get("outcome") else ["news", "technical"]

    # ---- 4a. News & Sentiment (parallel) -------------------------------------------------------
    async def news(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        symbol = state["current"]["symbol"]
        try:
            data = await news_context(st, bus, symbol)
            res = await deps.agent("news").run(NewsInput(symbol=symbol, **data), ctx)
            out = res.output.model_dump(mode="json")
            await _message(ctx.run_id, "news", "analyst", "sentiment_report", symbol, res.output.summary, out, bus)
            return {"news": out, "trace": _step("news", t0, symbol=symbol, sentiment=out["sentiment"], skipped=res.skipped)}
        except AgentError as e:
            return {"news": {"error": str(e)}, "trace": _step("news", t0, symbol=symbol, error=str(e))}

    # ---- 4b. Technical Analyst (parallel) ------------------------------------------------------
    async def technical(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        symbol = state["current"]["symbol"]
        snaps = {tf: compute_snapshot(await reader.get_candles(symbol, tf, 300)) for tf in st.timeframes}
        primary = next((snaps[tf] for tf in st.timeframes if snaps[tf].get("sufficient_data")), {})
        reference = {"atr14": (primary.get("volatility") or {}).get("atr14"), "atr_pct": (primary.get("volatility") or {}).get("atr_pct"),
                     "levels": {tf: s.get("levels") for tf, s in snaps.items() if s.get("sufficient_data")}}
        try:
            res = await deps.agent("technical").run(TechnicalInput(symbol=symbol, last_price=float(state["market"]["last_price"]), timeframes=snaps), ctx)
            out = {**res.output.model_dump(mode="json"), "skipped": res.skipped}
            await _message(ctx.run_id, "technical", "analyst", "technical_report", symbol, res.output.summary, {"report": out, "indicators": snaps}, bus)
            return {"technical": out, "tech_reference": reference, "trace": _step("technical", t0, symbol=symbol, bias=out["bias"], skipped=res.skipped)}
        except AgentError as e:
            return {"technical": {"error": str(e)}, "tech_reference": reference, "trace": _step("technical", t0, symbol=symbol, error=str(e))}

    # ---- 5. Analyst ----------------------------------------------------------------------------
    async def analyst(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        cur, tech, market = state["current"], state.get("technical") or {}, state["market"]
        symbol = cur["symbol"]
        if tech.get("error") or tech.get("skipped"):
            reason = "technical analysis unavailable; a proposal without it is not allowed"
            return {"outcome": {"status": "no_trade", "stage": "analyst", "reasons": [reason]}, "trace": _step("analyst", t0, symbol=symbol, skipped=reason)}
        async with session_scope() as s:
            params = (await load_portfolio(s, st, bus))[1]
            pos = (await s.execute(select(Position).where(Position.symbol == symbol, Position.mode == st.trading_mode.value,
                                                           Position.status == PositionStatus.OPEN))).scalar_one_or_none()
        open_position = None if pos is None else {"quantity": str(pos.quantity), "entry_price": str(pos.entry_price),
                                                  "stop_loss": str(pos.stop_loss), "take_profit": str(pos.take_profit)}
        news_report = state.get("news")
        inp = AnalystInput(
            symbol=symbol, last_price=float(market["last_price"]),
            scout={"scanner": {k: v for k, v in cur.items() if k != "pick"}, "pick": cur.get("pick"), "kind": cur.get("kind")},
            news=None if not news_report or news_report.get("error") else news_report, technical=tech,
            reference={**(state.get("tech_reference") or {}), "spread_bps": market.get("spread_bps"), "quote_volume_24h": market.get("quote_volume_24h")},
            constraints={"min_risk_reward": float(params.min_risk_reward), "long_only_spot": True, "order_type": "market",
                         "fee_rate_per_side": float(params.fee_rate), "allowed_actions": ["SELL", "NO_TRADE"] if open_position else ["BUY", "NO_TRADE"]},
            open_position=open_position,
        )
        try:
            res = await deps.agent("analyst").run(inp, ctx)
        except AgentError as e:
            return {"outcome": {"status": "error", "stage": "analyst", "reasons": [str(e)]}, "trace": _step("analyst", t0, symbol=symbol, error=str(e))}
        out = res.output.model_dump(mode="json")
        if res.output.action == "NO_TRADE" or res.output.proposal is None:
            return {"analyst": out, "outcome": {"status": "no_trade", "stage": "analyst", "reasons": [res.output.rationale]},
                    "trace": _step("analyst", t0, symbol=symbol, action="NO_TRADE")}
        p = res.output.proposal
        async with session_scope() as s:
            row = TradeProposal(run_id=ctx.run_id, symbol=symbol, side=Side(p.side), entry_price=Decimal(str(p.entry_price)),
                                stop_loss=Decimal(str(p.stop_loss)), take_profit=Decimal(str(p.take_profit)), risk_reward=Decimal(str(p.risk_reward)),
                                holding_period=p.expected_holding_period, confidence=p.confidence, status=ProposalStatus.PROPOSED, evidence={})
            s.add(row)
            await s.flush()
            pid = row.id
        await _message(ctx.run_id, "analyst", "skeptic", "trade_proposal", symbol,
                       f"{p.side} {symbol} entry {p.entry_price:.8g} stop {p.stop_loss:.8g} target {p.take_profit:.8g} (R/R {p.risk_reward:.2f})", out, bus)
        return {"analyst": out, "proposal_id": pid, "trace": _step("analyst", t0, symbol=symbol, action=p.side, proposal_id=pid)}

    def after_analyst(state: CycleState) -> str:
        return "finalize" if state.get("outcome") else "skeptic"

    # ---- 6. Skeptic ----------------------------------------------------------------------------
    async def skeptic(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        cur = state["current"]
        symbol = cur["symbol"]
        inp = SkepticInput(proposal=state["analyst"]["proposal"], scout={k: v for k, v in cur.items()}, news=state.get("news"),
                           technical=state["technical"], market={**state["market"], **(state.get("tech_reference") or {})})
        try:
            res = await deps.agent("skeptic").run(inp, ctx)
        except AgentError as e:  # fail closed: no review means no trade
            return {"skeptic": {"error": str(e)}, "outcome": {"status": "rejected", "stage": "skeptic", "reasons": [f"skeptic review unavailable: {e}"]},
                    "trace": _step("skeptic", t0, symbol=symbol, error=str(e))}
        out = res.output.model_dump(mode="json")
        await _message(ctx.run_id, "skeptic", "risk_manager", "challenge", symbol, f"{res.output.verdict}: {res.output.summary}", out, bus)
        upd: dict[str, Any] = {"skeptic": out, "trace": _step("skeptic", t0, symbol=symbol, verdict=res.output.verdict)}
        if res.output.verdict == "REJECT":
            reasons = [f"[{o.severity}] {o.category}: {o.detail}" for o in res.output.objections] or [res.output.summary]
            upd["outcome"] = {"status": "rejected", "stage": "skeptic", "reasons": reasons}
        return upd

    def after_skeptic(state: CycleState) -> str:
        return "finalize" if state.get("outcome") else "risk"

    # ---- 7. Risk: deterministic engine first, then the Risk Manager agent (veto only) ----------
    async def risk(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        symbol = state["current"]["symbol"]
        p = state["analyst"]["proposal"]
        prop = ProposalInput(symbol=symbol, side=p["side"], entry_price=Decimal(str(p["entry_price"])),
                             stop_loss=Decimal(str(p["stop_loss"])), take_profit=Decimal(str(p["take_profit"])))
        market = await build_market_context(reader, symbol)
        async with session_scope() as s:
            control = await get_control(s)
            portfolio, params = await load_portfolio(s, st, bus)
            pos = (await s.execute(select(Position).where(Position.symbol == symbol, Position.mode == st.trading_mode.value,
                                                           Position.status == PositionStatus.OPEN))).scalar_one_or_none()
        if prop.side == "SELL":  # exit of an existing long: always risk-reducing, no sizing decision
            if pos is None:
                engine = RiskDecision(approved=False, reasons=["spot_long_only: SELL requires an open position (no shorting)"])
            else:
                engine = RiskDecision(approved=True, quantity=pos.quantity, notional=pos.quantity * (market.last_price or pos.entry_price))
        else:
            engine = evaluate_entry(prop, market, portfolio, params, trading_allowed=control.allows_new_entries)
        engine_json = engine.model_dump(mode="json")
        risk_state: dict[str, Any] = {"engine": engine_json, "manager": None, "final_quantity": "0"}

        agent = deps.agent("risk_manager")
        inp = RiskManagerInput(proposal=p, deterministic_assessment=engine_json, portfolio=portfolio.model_dump(mode="json"),
                               risk_parameters=params.model_dump(mode="json"), skeptic=state.get("skeptic") or {})
        try:
            # RiskManagerAgent.skip() avoids the LLM call when the engine already rejected, or for exits.
            res = await agent.run(inp, ctx)
        except AgentError as e:  # fail closed
            risk_state["manager"] = {"error": str(e)}
            return {"risk": risk_state, "outcome": {"status": "rejected", "stage": "risk_manager", "reasons": [f"risk manager unavailable: {e}"]},
                    "trace": _step("risk", t0, symbol=symbol, error=str(e))}
        rm = res.output
        risk_state["manager"] = {**rm.model_dump(mode="json"), "skipped": res.skipped}
        await _message(ctx.run_id, "risk_manager", "trading_engine", "risk_decision", symbol, f"{rm.decision}: {rm.reasoning}", risk_state, bus)

        if not engine.approved:
            return {"risk": risk_state, "outcome": {"status": "rejected", "stage": "risk_engine", "reasons": engine.reasons},
                    "trace": _step("risk", t0, symbol=symbol, decision="ENGINE_REJECT")}
        if rm.decision == "REJECT":
            return {"risk": risk_state, "outcome": {"status": "rejected", "stage": "risk_manager", "reasons": rm.concerns or [rm.reasoning]},
                    "trace": _step("risk", t0, symbol=symbol, decision="REJECT")}
        final = engine
        if prop.side == "BUY" and rm.decision == "REDUCE":
            wanted = floor_to_step(engine.quantity * Decimal(str(rm.size_multiplier)), market.qty_step)
            final = evaluate_entry(prop, market, portfolio, params, trading_allowed=control.allows_new_entries, requested_quantity=wanted)
            risk_state["engine_after_reduce"] = final.model_dump(mode="json")
            if not final.approved:
                return {"risk": risk_state, "outcome": {"status": "rejected", "stage": "risk_engine", "reasons": final.reasons},
                        "trace": _step("risk", t0, symbol=symbol, decision="REDUCE_INVALID")}
        risk_state["final_quantity"] = str(final.quantity)
        return {"risk": risk_state,
                "outcome": {"status": "approved", "stage": "risk_manager", "reasons": [], "quantity": str(final.quantity),
                            "risk_amount": str(final.loss_at_stop), "notional": str(final.notional)},
                "trace": _step("risk", t0, symbol=symbol, decision=rm.decision, quantity=str(final.quantity))}

    # ---- 8. persist the decision and hand off to the trading engine ----------------------------
    async def finalize(state: CycleState) -> dict[str, Any]:
        t0 = time.monotonic()
        cur, outcome, pid = state["current"], state["outcome"] or {"status": "error", "stage": "unknown", "reasons": []}, state.get("proposal_id")
        symbol = cur["symbol"]
        evidence = _j({"scout": {"scanner": {k: v for k, v in cur.items() if k != "pick"}, "pick": cur.get("pick")},
                       "news": state.get("news"), "technical": state.get("technical"), "analyst": state.get("analyst"),
                       "skeptic": state.get("skeptic"), "risk": state.get("risk"), "outcome": outcome})
        approved_id = None
        if pid is not None:
            async with session_scope() as s:  # one transaction: status + approved/rejected row are atomic
                prop = await s.get(TradeProposal, pid)
                assert prop is not None
                prop.evidence = evidence
                if outcome["status"] == "approved":
                    prop.status = ProposalStatus.APPROVED
                    a = ApprovedTrade(proposal_id=pid, symbol=symbol, side=prop.side, quantity=Decimal(outcome["quantity"]),
                                      entry_price=prop.entry_price, stop_loss=prop.stop_loss, take_profit=prop.take_profit,
                                      risk_amount=Decimal(outcome["risk_amount"]),
                                      expires_at=utcnow() + timedelta(seconds=st.approved_trade_ttl_seconds))
                    s.add(a)
                    await s.flush()
                    approved_id = a.id
                else:
                    prop.status = ProposalStatus.REJECTED
                    s.add(RejectedTrade(proposal_id=pid, symbol=symbol, stage=outcome["stage"], reasons=outcome["reasons"]))
        await bus.cache_set(f"cooldown:{symbol}", {"at": utcnow().isoformat(), "status": outcome["status"]}, ttl=st.symbol_cooldown_minutes * 60)
        result = {"symbol": symbol, "proposal_id": pid, "approved_trade_id": approved_id, **outcome}
        await bus.publish("decision", result)
        log.info("candidate_decision", **{k: v for k, v in result.items() if k != "reasons"}, reasons=outcome.get("reasons", [])[:3])
        return {"results": [result], "trace": _step("finalize", t0, symbol=symbol, status=outcome["status"], stage=outcome["stage"])}

    g = StateGraph(CycleState)
    for name, fn in (("scan", scan), ("scout", scout), ("next_candidate", next_candidate), ("news", news), ("technical", technical),
                     ("analyst", analyst), ("skeptic", skeptic), ("risk", risk), ("finalize", finalize)):
        g.add_node(name, fn)
    g.add_edge(START, "scan")
    g.add_conditional_edges("scan", after_scan, ["scout", END])
    g.add_edge("scout", "next_candidate")
    g.add_conditional_edges("next_candidate", after_next, ["news", "technical", "finalize", END])
    g.add_edge(["news", "technical"], "analyst")
    g.add_conditional_edges("analyst", after_analyst, ["skeptic", "finalize"])
    g.add_conditional_edges("skeptic", after_skeptic, ["risk", "finalize"])
    g.add_edge("risk", "finalize")
    g.add_edge("finalize", "next_candidate")
    return g.compile()
