from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import select

from aegis.core.state import TradingStatus, set_control
from aegis.core.timeutil import utcnow
from aegis.db.models import (
    AgentExecution,
    AgentMessage,
    ApprovedStatus,
    ApprovedTrade,
    ExecStatus,
    NewsItem,
    ProposalStatus,
    RejectedTrade,
    RunStatus,
    TradeProposal,
    WorkflowRun,
)
from aegis.db.session import session_scope
from aegis.orchestration.runner import CycleRunner
from tests.conftest import ScriptedLLM, input_of, router_with, seed_market, set_ticker, start_trading


def scripts(last: float, **over):
    def scout(user):
        cands = input_of(user)["candidates"]
        return {"market_summary": "breakout", "rejected": [], "picks": [
            {"symbol": c["symbol"], "rank": i + 1, "opportunity_type": "breakout", "thesis": "volume breakout", "liquidity": "good", "confidence": 0.8}
            for i, c in enumerate(cands)]}

    def analyst(user):
        price = input_of(user)["last_price"]
        return {"action": "BUY", "rationale": "aligned", "proposal": {
            "asset": input_of(user)["symbol"], "side": "BUY", "entry_price": price, "stop_loss": round(price * 0.97, 4), "take_profit": round(price * 1.09, 4),
            "risk_reward": 3, "expected_holding_period": "1-3 days", "supporting_evidence": ["EMA stack up", "volume 4x"], "confidence": 0.72}}

    base = {
        "scout": scout,
        "news": {"sentiment": "neutral", "score": 0.1, "confidence": 0.4, "data_quality": "low", "key_events": [], "conflicts": [], "summary": "quiet"},
        "technical": {"bias": "bullish", "strength": 0.7, "confidence": 0.7, "timeframe_alignment": "aligned", "signals": [
            {"indicator": "EMA", "timeframe": "1h", "reading": "20>50>200", "interpretation": "bullish"}], "key_support": None, "key_resistance": None,
            "volatility": "normal", "summary": "uptrend"},
        "analyst": analyst,
        "skeptic": {"verdict": "APPROVE", "objections": [{"category": "weak_technicals", "severity": "low", "detail": "RSI elevated"}],
                    "risk_score": 0.3, "confidence": 0.6, "summary": "survives"},
        "risk_manager": {"decision": "APPROVE", "size_multiplier": 1.0, "concerns": [], "reasoning": "within limits", "confidence": 0.8},
    }
    base.update(over)
    return base


async def add_news():
    async with session_scope() as s:
        s.add(NewsItem(uid="u1", source="TestWire", title="Bitcoin ETF inflows rise", url="http://x/1", summary="", published_at=utcnow() - timedelta(hours=1), symbols=["BTC"]))


async def make_runner(settings, bus, reader, llm) -> CycleRunner:
    return CycleRunner(settings, bus, reader, router_with(settings, bus, llm))


async def test_full_cycle_approves_and_persists_complete_decision_trail(settings, bus, db, reader):
    last = await seed_market(bus)
    await add_news()
    await start_trading()
    llm = ScriptedLLM(scripts(last))
    out = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert out["ran"] and out["status"] == RunStatus.COMPLETED and out["approved"] == 1
    assert [llm.count(a) for a in ("scout", "news", "technical", "analyst", "skeptic", "risk_manager")] == [1] * 6

    async with session_scope() as s:
        run = (await s.execute(select(WorkflowRun))).scalar_one()
        prop = (await s.execute(select(TradeProposal))).scalar_one()
        approved = (await s.execute(select(ApprovedTrade))).scalar_one()
        execs = (await s.execute(select(AgentExecution))).scalars().all()
        msgs = (await s.execute(select(AgentMessage).order_by(AgentMessage.id))).scalars().all()
    assert [t["node"] for t in run.trace if t["node"] not in ("news", "technical")] == ["scan", "scout", "next_candidate", "analyst", "skeptic", "risk", "finalize"]
    assert prop.status == ProposalStatus.APPROVED and prop.run_id == run.id
    assert set(prop.evidence) == {"scout", "news", "technical", "analyst", "skeptic", "risk", "outcome"}
    assert prop.evidence["risk"]["engine"]["approved"] is True
    # size comes from the deterministic engine: <= 1% risk, <= 30% exposure
    assert approved.status == ApprovedStatus.PENDING and approved.proposal_id == prop.id
    assert approved.risk_amount <= Decimal(10) and approved.quantity * approved.entry_price <= Decimal(301)
    assert Decimal(prop.evidence["risk"]["engine"]["quantity"]) == approved.quantity
    assert {e.agent for e in execs} == {"scout", "news", "technical", "analyst", "skeptic", "risk_manager"} and all(e.status == ExecStatus.SUCCESS and e.run_id == run.id for e in execs)
    assert {(m.sender, m.recipient, m.kind) for m in msgs} == {
        ("scout", "analyst", "opportunity"), ("news", "analyst", "sentiment_report"), ("technical", "analyst", "technical_report"),
        ("analyst", "skeptic", "trade_proposal"), ("skeptic", "risk_manager", "challenge"), ("risk_manager", "trading_engine", "risk_decision")}
    # the technical agent received deterministic indicator values, and the news agent the real stored item
    tech_in = input_of(next(c for c in llm.calls if c["agent"] == "technical")["user"])
    assert tech_in["timeframes"]["1h"]["sufficient_data"] and tech_in["timeframes"]["1h"]["rsi14"] is not None
    news_in = input_of(next(c for c in llm.calls if c["agent"] == "news")["user"])
    assert news_in["asset_news"][0]["title"] == "Bitcoin ETF inflows rise"
    # cooldown: an immediate second cycle makes no LLM calls for the same symbol
    n = len(llm.calls)
    again = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert again["ran"] and again["candidates"] == 0 and len(llm.calls) == n


async def test_news_and_technical_run_in_parallel(settings, bus, db, reader):
    last = await seed_market(bus)
    await add_news()
    await start_trading()
    windows: dict[str, tuple[float, float]] = {}

    class Timed(ScriptedLLM):
        async def complete(self, **kw):
            agent = self.agent_of(kw["system"])
            t0 = time.monotonic()
            if agent in ("news", "technical"):
                await asyncio.sleep(0.3)
            r = await super().complete(**kw)
            windows[agent] = (t0, time.monotonic())
            return r

    await (await make_runner(settings, bus, reader, Timed(scripts(last)))).run_cycle()
    (n0, n1), (t0, t1) = windows["news"], windows["technical"]
    assert n0 < t1 and t0 < n1, "news and technical executions must overlap"
    assert windows["analyst"][0] >= max(n1, t1), "analyst must wait for both (fan-in)"


async def test_conditional_transitions_stop_early_to_save_llm_calls(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    # analyst says NO_TRADE -> skeptic and risk manager are never called, nothing is proposed
    llm = ScriptedLLM(scripts(last, analyst={"action": "NO_TRADE", "rationale": "mixed evidence", "proposal": None}))
    out = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert out["no_trade"] == 1 and llm.count("skeptic") == 0 and llm.count("risk_manager") == 0
    assert llm.count("news") == 0, "no stored news -> the news agent must not call the LLM"
    async with session_scope() as s:
        assert (await s.execute(select(TradeProposal))).first() is None


async def test_skeptic_rejection_blocks_trade(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    llm = ScriptedLLM(scripts(last, skeptic={"verdict": "REJECT", "risk_score": 0.9, "confidence": 0.8, "summary": "pump",
                                             "objections": [{"category": "manipulation_risk", "severity": "high", "detail": "isolated spike"}]}))
    out = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert out["rejected"] == 1 and llm.count("risk_manager") == 0
    async with session_scope() as s:
        rej = (await s.execute(select(RejectedTrade))).scalar_one()
        assert rej.stage == "skeptic" and "manipulation_risk" in rej.reasons[0]
        assert (await s.execute(select(ApprovedTrade))).first() is None
        assert (await s.execute(select(TradeProposal))).scalar_one().status == ProposalStatus.REJECTED


async def test_deterministic_engine_veto_cannot_be_overridden_by_llm(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()

    def greedy_analyst(user):  # 1:1 risk/reward violates the 2:1 minimum
        price = input_of(user)["last_price"]
        return {"action": "BUY", "rationale": "yolo", "proposal": {"asset": "BTC/USDT", "side": "BUY", "entry_price": price, "stop_loss": price * 0.97,
                "take_profit": price * 1.03, "risk_reward": 5, "expected_holding_period": "1d", "supporting_evidence": ["trust me"], "confidence": 0.99}}

    llm = ScriptedLLM(scripts(last, analyst=greedy_analyst))
    out = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert out["rejected"] == 1 and llm.count("risk_manager") == 0  # no LLM call spent on an engine veto
    async with session_scope() as s:
        rej = (await s.execute(select(RejectedTrade))).scalar_one()
        rm_exec = (await s.execute(select(AgentExecution).where(AgentExecution.agent == "risk_manager"))).scalar_one()
        assert rej.stage == "risk_engine" and any("risk_reward" in r for r in rej.reasons)
        assert rm_exec.status == ExecStatus.SKIPPED and rm_exec.llm_calls == 0
        assert (await s.execute(select(ApprovedTrade))).first() is None


async def test_risk_manager_veto_and_reduce(settings, bus, db, reader):
    last = await seed_market(bus)
    await seed_market(bus, "ETH/USDT")
    await start_trading()
    llm = ScriptedLLM(scripts(last, risk_manager=[
        {"decision": "REDUCE", "size_multiplier": 0.5, "concerns": ["correlated"], "reasoning": "halve", "confidence": 0.7},
        {"decision": "REJECT", "size_multiplier": 0, "concerns": ["too much beta"], "reasoning": "no", "confidence": 0.9}]))
    out = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert out["approved"] == 1 and out["rejected"] == 1
    async with session_scope() as s:
        approved = (await s.execute(select(ApprovedTrade))).scalar_one()
        prop = await s.get(TradeProposal, approved.proposal_id)
        rej = (await s.execute(select(RejectedTrade))).scalar_one()
    full = Decimal(prop.evidence["risk"]["engine"]["quantity"])
    assert Decimal(0) < approved.quantity <= full / 2
    assert rej.stage == "risk_manager" and rej.reasons == ["too much beta"]


async def test_agent_failures_fail_closed(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    llm = ScriptedLLM(scripts(last, skeptic="this is not json"))
    out = await (await make_runner(settings, bus, reader, llm)).run_cycle()
    assert out["status"] == RunStatus.COMPLETED and out["rejected"] == 1 and out["approved"] == 0
    async with session_scope() as s:
        assert (await s.execute(select(RejectedTrade))).scalar_one().stage == "skeptic"
    # technical agent down -> the analyst is not even asked
    await bus.r.flushall()
    await seed_cache_only(bus, last)
    llm2 = ScriptedLLM(scripts(last, technical="garbage"))
    out2 = await (await make_runner(settings, bus, reader, llm2)).run_cycle()
    assert out2["no_trade"] == 1 and llm2.count("analyst") == 0


async def seed_cache_only(bus, last):
    from aegis.marketdata.service import MD_STATUS_KEY, UNIVERSE_KEY
    await set_ticker(bus, "BTC/USDT", last)
    await bus.cache_set(UNIVERSE_KEY, ["BTC/USDT"], ttl=60)
    await bus.cache_set(MD_STATUS_KEY, {"healthy": True}, ttl=60)


async def test_preflight_blocks_cycles_without_spending(settings, bus, db, reader):
    last = await seed_market(bus)
    llm = ScriptedLLM(scripts(last))
    runner = await make_runner(settings, bus, reader, llm)
    assert (await runner.run_cycle())["reason"] == "trading status is STOPPED"
    async with session_scope() as s:
        await set_control(s, TradingStatus.PAUSED, by="t")
    assert "PAUSED" in (await runner.run_cycle())["reason"]
    await start_trading()
    from aegis.marketdata.service import MD_STATUS_KEY
    await bus.cache_set(MD_STATUS_KEY, {"healthy": False, "reason": "only 0/1 tickers are fresh"}, ttl=60)
    assert "market data unhealthy" in (await runner.run_cycle())["reason"]  # market data outage
    await bus.cache_set(MD_STATUS_KEY, {"healthy": True}, ttl=60)
    from aegis.llm.providers import LLMRouter
    unconfigured = CycleRunner(settings, bus, reader, LLMRouter(settings, bus, providers={}))
    assert "not configured" in (await unconfigured.run_cycle())["reason"]
    assert llm.calls == []
    async with session_scope() as s:
        assert (await s.execute(select(WorkflowRun))).first() is None


async def test_stale_price_skips_candidate_and_recovery_marks_interrupted_runs(settings, bus, db, reader):
    last = await seed_market(bus)
    await start_trading()
    llm = ScriptedLLM(scripts(last))
    runner = await make_runner(settings, bus, reader, llm)

    orig = runner.agents
    from aegis.agents.definitions import AGENTS, ScoutAgent

    class StalingScout(ScoutAgent):
        async def run(self, inp, ctx):
            res = await super().run(inp, ctx)
            await set_ticker(bus, "BTC/USDT", last, age=600)  # price goes stale between scout and analysis
            return res

    runner.agents = {**AGENTS, "scout": StalingScout()}
    out = await runner.run_cycle()
    runner.agents = orig
    assert out["results"][0]["status"] == "skipped" and llm.count("analyst") == 0

    async with session_scope() as s:
        s.add(WorkflowRun(run_uid="dead", status=RunStatus.RUNNING))
        s.add(AgentExecution(agent="analyst", status=ExecStatus.RUNNING, input={}))
    await runner.recover()
    async with session_scope() as s:
        assert (await s.execute(select(WorkflowRun).where(WorkflowRun.run_uid == "dead"))).scalar_one().status == RunStatus.ABORTED
        assert (await s.execute(select(AgentExecution).where(AgentExecution.agent == "analyst"))).scalar_one().status == ExecStatus.ERROR
