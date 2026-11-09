from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from aegis.agents.base import AgentContext, AgentError, extract_json
from aegis.agents.definitions import AGENTS
from aegis.agents.schemas import (
    AnalystInput,
    NewsInput,
    RiskManagerInput,
    ScoutInput,
    SkepticInput,
    TechnicalInput,
)
from aegis.core.state import get_agent_configs
from aegis.db.models import AgentExecution, ExecStatus
from aegis.db.session import session_scope
from aegis.llm.providers import LLMError, LLMNotConfigured, LLMRouter, OpenAICompatibleProvider
from tests.conftest import ScriptedLLM, make_settings, router_with

CAND = [{"symbol": "BTC/USDT", "score": 5}, {"symbol": "ETH/USDT", "score": 4}]


async def ctx_for(settings, bus, llm) -> AgentContext:
    async with session_scope() as s:
        configs = await get_agent_configs(s, settings)
    return AgentContext(settings=settings, router=router_with(settings, bus, llm), bus=bus, configs=configs)


def test_six_distinct_agents_with_own_prompts_and_schemas():
    assert list(AGENTS) == ["scout", "news", "technical", "analyst", "skeptic", "risk_manager"]
    assert len({a.system_prompt for a in AGENTS.values()}) == 6
    assert len({a.output_model for a in AGENTS.values()}) == 6 and len({a.input_model for a in AGENTS.values()}) == 6
    assert all(a.tools and a.role for a in AGENTS.values())


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": 2}} done') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")


async def test_scout_real_call_is_validated_and_recorded(settings, bus, db):
    llm = ScriptedLLM({"scout": {"market_summary": "ok", "picks": [
        {"symbol": "DOGE/USDT", "rank": 1, "opportunity_type": "momentum", "thesis": "invented", "liquidity": "good", "confidence": 0.9},
        {"symbol": "ETH/USDT", "rank": 3, "opportunity_type": "breakout", "thesis": "volume breakout", "liquidity": "good", "confidence": 0.7},
        {"symbol": "BTC/USDT", "rank": 2, "opportunity_type": "breakout", "thesis": "held already", "liquidity": "good", "confidence": 0.6},
    ], "rejected": []}})
    ctx = await ctx_for(settings, bus, llm)
    res = await AGENTS["scout"].run(ScoutInput(candidates=CAND, open_position_symbols=["BTC/USDT"], max_picks=2), ctx)
    # hallucinated symbol and held symbol are removed; ranks renumbered
    assert [(p.symbol, p.rank) for p in res.output.picks] == [("ETH/USDT", 1)]
    assert llm.count("scout") == 1 and "INPUT DATA" in llm.calls[0]["user"] and "BTC/USDT" in llm.calls[0]["user"]
    async with session_scope() as s:
        row = (await s.execute(select(AgentExecution))).scalar_one()
    assert (row.agent, row.status, row.llm_calls, row.provider) == ("scout", ExecStatus.SUCCESS, 1, "fake")
    assert row.prompt_tokens == 10 and row.output["picks"][0]["symbol"] == "ETH/USDT" and row.confidence == 0.7
    status = await bus.get_agent_status("scout")
    assert status["state"] == "idle" and status["last"]["status"] == "SUCCESS"


async def test_invalid_output_triggers_one_repair_retry_then_fails(settings, bus, db):
    llm = ScriptedLLM({"scout": ['{"market_summary": 5, "picks": "nope"}', {"market_summary": "fixed", "picks": [], "rejected": []}]})
    ctx = await ctx_for(settings, bus, llm)
    res = await AGENTS["scout"].run(ScoutInput(candidates=CAND), ctx)
    assert res.llm_calls == 2 and res.output.market_summary == "fixed"
    assert "rejected by validation" in llm.calls[1]["user"]

    always_bad = ScriptedLLM({"scout": "not json at all"})
    ctx = await ctx_for(settings, bus, always_bad)
    with pytest.raises(AgentError, match="gave up after 2 LLM calls"):
        await AGENTS["scout"].run(ScoutInput(candidates=CAND), ctx)
    async with session_scope() as s:
        rows = (await s.execute(select(AgentExecution).order_by(AgentExecution.id))).scalars().all()
    assert rows[-1].status == ExecStatus.ERROR and "invalid output" in rows[-1].error


async def test_no_candidates_and_no_news_skip_the_llm(settings, bus, db):
    llm = ScriptedLLM({})
    ctx = await ctx_for(settings, bus, llm)
    res = await AGENTS["scout"].run(ScoutInput(candidates=[]), ctx)
    news = await AGENTS["news"].run(NewsInput(symbol="BTC/USDT"), ctx)
    tech = await AGENTS["technical"].run(TechnicalInput(symbol="BTC/USDT", last_price=1.0, timeframes={"1h": {"sufficient_data": False}}), ctx)
    assert res.skipped and news.skipped and tech.skipped and llm.calls == []
    assert news.output.data_quality == "none" and news.output.sentiment == "neutral" and news.output.confidence == 0
    async with session_scope() as s:
        assert {r.status for r in (await s.execute(select(AgentExecution))).scalars()} == {ExecStatus.SKIPPED}


async def test_news_agent_cannot_cite_sources_it_was_not_given(settings, bus, db):
    llm = ScriptedLLM({"news": {"sentiment": "bullish", "score": 0.8, "confidence": 0.9, "data_quality": "high", "conflicts": [], "summary": "s",
                                "key_events": [{"headline": "ETF approved", "source_ids": [11], "impact": "bullish", "importance": "high"},
                                               {"headline": "Made-up partnership", "source_ids": [999], "impact": "bullish", "importance": "high"}]}})
    ctx = await ctx_for(settings, bus, llm)
    res = await AGENTS["news"].run(NewsInput(symbol="BTC/USDT", asset_news=[{"id": 11, "title": "ETF approved", "source": "x"}]), ctx)
    assert [e.headline for e in res.output.key_events] == ["ETF approved"]
    # only a sentiment index, no articles: quality is forced down regardless of what the model claims
    res2 = await AGENTS["news"].run(NewsInput(symbol="BTC/USDT", fear_greed={"value": 70}), ctx)
    assert res2.output.data_quality == "low" and res2.output.key_events == []


async def test_analyst_geometry_and_recomputed_risk_reward(settings, bus, db):
    good = {"action": "BUY", "rationale": "r", "proposal": {"asset": "WRONG", "side": "BUY", "entry_price": 100, "stop_loss": 98, "take_profit": 106,
            "risk_reward": 99, "expected_holding_period": "1-2 days", "supporting_evidence": ["rsi"], "confidence": 0.7}}
    bad = {**good, "proposal": {**good["proposal"], "stop_loss": 101}}
    llm = ScriptedLLM({"analyst": [bad, good]})
    ctx = await ctx_for(settings, bus, llm)
    inp = AnalystInput(symbol="BTC/USDT", last_price=100, scout={}, news=None, technical={}, reference={}, constraints={})
    res = await AGENTS["analyst"].run(inp, ctx)
    assert res.llm_calls == 2 and res.output.proposal.risk_reward == 3.0 and res.output.proposal.asset == "BTC/USDT"
    sell = {**good, "action": "SELL", "proposal": {**good["proposal"], "side": "SELL", "stop_loss": 102, "take_profit": 96}}
    with pytest.raises(AgentError, match="SELL is only allowed"):
        await AGENTS["analyst"].run(inp, await ctx_for(settings, bus, ScriptedLLM({"analyst": sell})))
    no_trade = await AGENTS["analyst"].run(inp, await ctx_for(settings, bus, ScriptedLLM({"analyst": {"action": "NO_TRADE", "rationale": "weak", "proposal": good["proposal"]}})))
    assert no_trade.output.proposal is None


async def test_skeptic_critical_objection_forces_reject_and_risk_manager_cannot_upsize(settings, bus, db):
    llm = ScriptedLLM({
        "skeptic": {"verdict": "APPROVE", "risk_score": 0.9, "confidence": 0.8, "summary": "s",
                    "objections": [{"category": "manipulation_risk", "severity": "critical", "detail": "pump"}]},
        "risk_manager": {"decision": "REDUCE", "size_multiplier": 1.0, "concerns": [], "reasoning": "r", "confidence": 0.5},
    })
    ctx = await ctx_for(settings, bus, llm)
    sk = await AGENTS["skeptic"].run(SkepticInput(proposal={"asset": "BTC/USDT"}, scout={}, news=None, technical={}, market={}), ctx)
    assert sk.output.verdict == "REJECT"
    rm_in = RiskManagerInput(proposal={"asset": "BTC/USDT", "side": "BUY"}, deterministic_assessment={"approved": True}, portfolio={}, risk_parameters={}, skeptic={})
    rm = await AGENTS["risk_manager"].run(rm_in, ctx)
    assert rm.output.decision == "APPROVE" and rm.output.size_multiplier == 1.0
    with pytest.raises(AgentError):  # schema forbids multiplier > 1 outright
        await AGENTS["risk_manager"].run(rm_in, await ctx_for(settings, bus, ScriptedLLM({"risk_manager": {"decision": "APPROVE", "size_multiplier": 3, "concerns": [], "reasoning": "r", "confidence": 1}})))
    # engine veto -> no LLM call at all
    veto = await AGENTS["risk_manager"].run(rm_in.model_copy(update={"deterministic_assessment": {"approved": False, "reasons": ["risk_reward: too low"]}}), ctx)
    assert veto.skipped and veto.output.decision == "REJECT" and llm.count("risk_manager") == 1


async def test_timeout_and_provider_errors(bus, db):
    import asyncio
    settings = make_settings(agent_timeout_seconds=1.2, llm_max_retries=0)

    class Slow(ScriptedLLM):
        async def complete(self, **kw):
            await asyncio.sleep(5)

    with pytest.raises(AgentError) as e:
        await AGENTS["scout"].run(ScoutInput(candidates=CAND), await ctx_for(settings, bus, Slow({})))
    assert "timed out" in str(e.value)
    with pytest.raises(AgentError, match="HTTP 401"):
        await AGENTS["scout"].run(ScoutInput(candidates=CAND), await ctx_for(settings, bus, ScriptedLLM({"scout": LLMError("fake: HTTP 401", retryable=False)})))
    flaky = ScriptedLLM({"scout": [LLMError("fake: HTTP 503", retryable=True), {"market_summary": "ok", "picks": [], "rejected": []}]})
    res = await AGENTS["scout"].run(ScoutInput(candidates=CAND), await ctx_for(make_settings(), bus, flaky))
    assert res.llm_calls == 2


async def test_unconfigured_provider_fails_loudly_never_mocks(bus, db):
    settings = make_settings(llm_default_provider="openai")
    router = LLMRouter(settings, bus)
    assert router.available() == []
    with pytest.raises(LLMNotConfigured):
        router.get("openai")
    async with session_scope() as s:
        configs = await get_agent_configs(s, settings)
    with pytest.raises(AgentError, match="no API key configured"):
        await AGENTS["scout"].run(ScoutInput(candidates=CAND), AgentContext(settings=settings, router=router, bus=bus, configs=configs))
    await router.close()


async def test_daily_budget_stops_llm_calls(bus, db):
    settings = make_settings(llm_daily_call_budget=1)
    llm = ScriptedLLM({"scout": {"market_summary": "ok", "picks": [], "rejected": []}})
    ctx = await ctx_for(settings, bus, llm)
    await AGENTS["scout"].run(ScoutInput(candidates=CAND), ctx)
    with pytest.raises(AgentError, match="budget"):
        await AGENTS["scout"].run(ScoutInput(candidates=CAND), ctx)
    assert llm.count("scout") == 1


async def test_openai_compatible_wire_format_and_per_agent_models(bus, db):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        seen["url"], seen["auth"], seen["body"] = str(request.url), request.headers["authorization"], json.loads(request.content)
        return httpx.Response(200, json={"model": "grok-x", "choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    p = OpenAICompatibleProvider("xai", "https://api.x.ai/v1/", "sk-secret", http)
    r = await p.complete(system="sys", user="usr", model="grok-x", temperature=0.1, timeout=5)
    assert seen["url"] == "https://api.x.ai/v1/chat/completions" and seen["auth"] == "Bearer sk-secret"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"} and seen["body"]["response_format"] == {"type": "json_object"}
    assert (r.content, r.prompt_tokens, r.completion_tokens) == ('{"ok": true}', 7, 3)
    for code, retryable in ((429, True), (500, True), (401, False)):
        bad = OpenAICompatibleProvider("x", "https://h/v1", "k", httpx.AsyncClient(transport=httpx.MockTransport(lambda req, c=code: httpx.Response(c, text="err"))))
        with pytest.raises(LLMError) as e:
            await bad.complete(system="s", user="u", model="m", temperature=0, timeout=5)
        assert e.value.retryable is retryable and "sk-" not in str(e.value)

    s = make_settings(llm_default_provider="openai", openai_api_key="sk-a", xai_api_key="xai-b", agent_scout_model="xai:grok-fast", openai_default_model="gpt-test")
    router = LLMRouter(s)
    assert router.available() == ["openai", "xai"]
    async with session_scope() as sess:
        cfg = await get_agent_configs(sess, s)
    assert (cfg["scout"].provider, cfg["scout"].model) == ("xai", "grok-fast")
    assert (cfg["analyst"].provider, cfg["analyst"].model) == ("openai", "gpt-test")
    await router.close()
