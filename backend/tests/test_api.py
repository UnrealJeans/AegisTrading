from __future__ import annotations

import asyncio
import json
import socket
from decimal import Decimal as D

import httpx
import pytest
import uvicorn
import websockets
from sqlalchemy import select

from aegis.api.app import bootstrap_admin, create_app
from aegis.core.state import get_risk_params, suspend_trading
from aegis.db.models import AuditLog, User
from aegis.db.session import session_scope
from tests.conftest import ScriptedLLM, make_settings, seed_market, start_trading
from tests.test_orchestration import make_runner, scripts

PW = "correct-horse-battery"


@pytest.fixture
async def client(settings, bus, db):
    await bootstrap_admin(settings)
    app = create_app(settings, bootstrap=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def auth(client) -> dict[str, str]:
    r = await client.post("/api/auth/login", json={"username": "admin", "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_authentication_is_required_everywhere(client):
    assert (await client.get("/api/health")).json() == {"status": "ok"}
    assert (await client.get("/api/ready")).json() == {"database": True, "redis": True}
    for path in ("/api/overview", "/api/agents", "/api/positions", "/api/orders", "/api/proposals", "/api/risk", "/api/settings", "/api/activity",
                 "/api/audit", "/api/runs", "/api/portfolio/history", "/api/approved"):
        assert (await client.get(path)).status_code == 401, path
    for path in ("/api/control/start", "/api/control/pause", "/api/control/emergency-stop", "/api/control/run-cycle"):
        assert (await client.post(path, json={})).status_code == 401, path
    assert (await client.put("/api/settings/risk", json={})).status_code == 401
    assert (await client.get("/api/overview", headers={"Authorization": "Bearer forged.token.value"})).status_code == 401


async def test_login_hashing_audit_and_rate_limit(client, settings):
    assert (await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})).status_code == 401
    assert (await client.post("/api/auth/login", json={"username": "ghost", "password": "x"})).status_code == 401
    h = await auth(client)
    assert (await client.get("/api/auth/me", headers=h)).json() == {"username": "admin"}
    async with session_scope() as s:
        user = (await s.execute(select(User))).scalar_one()
        actions = [a.action for a in (await s.execute(select(AuditLog).order_by(AuditLog.id))).scalars()]
    assert user.password_hash.startswith("$argon2") and PW not in user.password_hash
    assert actions == ["login_failed", "login_failed", "login_success"]
    codes = [(await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})).status_code for _ in range(settings.login_rate_limit_per_minute)]
    assert codes[-1] == 429 and (await client.post("/api/auth/login", json={"username": "admin", "password": PW})).status_code == 429


async def test_overview_agents_and_settings_never_leak_secrets(bus, db):
    settings = make_settings(openai_api_key="sk-live-SECRETSECRET1234", binance_testnet_api_key="testnet-KEY-abcdWXYZ", binance_testnet_api_secret="testnet-SECRET",
                             telegram_bot_token="123:TELEGRAMSECRET", llm_default_provider="openai", agent_scout_model="xai:grok-test")
    await bootstrap_admin(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, bootstrap=False)), base_url="http://testserver") as c:
        h = await auth(c)
        st = (await c.get("/api/settings", headers=h))
        ov = (await c.get("/api/overview", headers=h)).json()
        ag = (await c.get("/api/agents", headers=h)).json()
        blob = st.text + json.dumps(ov) + json.dumps(ag)
    for secret in ("SECRETSECRET", "testnet-KEY", "testnet-SECRET", "TELEGRAMSECRET", "x" * 40, PW):
        assert secret not in blob
    s = st.json()
    assert s["trading_mode"] == "paper" and s["live_trading_enabled"] is False
    assert s["providers"][0] == {"name": "openai", "base_url": "https://api.openai.com/v1", "configured": True, "key_hint": "…1234", "default_model": "gpt-4o-mini"}
    assert s["providers"][1]["configured"] is False and s["exchange"]["testnet_credentials"]["configured"] is True
    assert ov["system"]["is_paper"] and ov["system"]["control"]["status"] == "STOPPED"
    assert ov["portfolio"]["equity"] == "1000" and ov["stats"]["win_rate"] is None
    assert ov["system"]["services"] == {"worker": {"online": False}, "orchestrator": {"online": False}, "trading-engine": {"online": False}}
    assert [a["name"] for a in ag] == ["scout", "news", "technical", "analyst", "skeptic", "risk_manager"]
    scout, analyst = ag[0], ag[3]
    # honest status: scout's provider has no key; analyst has a key but the orchestrator is not running
    assert (scout["provider"], scout["online"], scout["offline_reason"]) == ("xai", False, "no API key for provider 'xai'")
    assert (analyst["provider_configured"], analyst["online"], analyst["offline_reason"]) == (True, False, "orchestrator service offline")


async def test_control_state_machine_and_audit(client, bus):
    h = await auth(client)
    post = lambda p, **j: client.post(f"/api/control/{p}", headers=h, json=j)  # noqa: E731
    assert (await post("pause")).status_code == 409                      # cannot pause when stopped
    assert (await post("start")).json()["status"] == "RUNNING"
    assert (await post("start")).status_code == 409
    assert (await post("pause")).json()["status"] == "PAUSED"
    assert (await post("resume")).json()["status"] == "RUNNING"
    r = await post("emergency-stop", liquidate=True, reason="test")
    assert r.json()["status"] == "EMERGENCY_STOP" and r.json()["liquidate"] is True
    assert (await post("resume")).status_code == 409                     # needs explicit confirmation
    assert (await post("resume", confirm=True)).json()["status"] == "RUNNING"
    async with session_scope() as s:
        assert await suspend_trading(s, "daily loss 3.5% reached the 3% limit")
    assert "daily loss" in (await post("resume")).json()["detail"]
    assert (await post("resume", confirm=True)).json()["liquidate"] is False
    audit = (await client.get("/api/audit", headers=h)).json()
    assert [a["action"] for a in audit if a["action"].startswith("control_")][:2] == ["control_resume", "control_resume"]
    assert any(e["data"].get("status") == "EMERGENCY_STOP" for e in await bus.recent_feed() if e["type"] == "control")
    assert (await post("run-cycle")).json() == {"queued": True}
    assert await bus.r.get("aegis:cmd:run_cycle") == "admin"


async def test_start_refused_when_mode_is_not_runnable(bus, db):
    settings = make_settings(trading_mode="live")
    await bootstrap_admin(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, bootstrap=False)), base_url="http://testserver") as c:
        r = await c.post("/api/control/start", headers=await auth(c))
    assert r.status_code == 409 and "LIVE_TRADING_ENABLED" in r.json()["detail"]


async def test_risk_settings_validation_and_bounds(client, settings):
    h = await auth(client)
    cur = (await client.get("/api/settings", headers=h)).json()["risk"]
    ok = await client.put("/api/settings/risk", headers=h, json={**cur, "risk_per_trade_pct": "0.5", "max_open_positions": 3})
    assert ok.status_code == 200
    async with session_scope() as s:
        p = await get_risk_params(s, settings)
    assert p.risk_per_trade_pct == D("0.5") and p.max_open_positions == 3 and p.max_daily_loss_pct == D(3)
    for bad in ({"risk_per_trade_pct": "50"}, {"min_risk_reward": "0.2"}, {"max_exposure_pct": "500"}, {"max_open_positions": 0}, {"starting_capital": "-1"},
                {"risk_per_trade_pct": "abc"}):
        assert (await client.put("/api/settings/risk", headers=h, json={**cur, **bad})).status_code == 422, bad
    assert (await client.put("/api/settings/agents/analyst", headers=h, json={"provider": "xai", "model": "grok-test"})).status_code == 200
    assert (await client.get("/api/settings", headers=h)).json()["agents"]["analyst"]["model"] == "grok-test"
    assert (await client.put("/api/settings/agents/analyst", headers=h, json={"provider": "evil", "model": "m"})).status_code == 422
    assert (await client.put("/api/settings/agents/analyst", headers=h, json={"provider": "xai", "model": "m; rm -rf"})).status_code == 422
    assert (await client.put("/api/settings/agents/nobody", headers=h, json={"provider": "xai", "model": "m"})).status_code == 404
    assert (await client.put("/api/settings/notifications", headers=h, json={"enabled": False})).json()["enabled"] is False


async def test_trading_views_show_full_decision_trail(client, settings, bus, reader):
    from aegis.config import RiskParams
    from aegis.trading.adapters.paper import PaperAdapter
    from aegis.trading.engine import TradingEngine
    from tests.conftest import set_ticker

    last = await seed_market(bus)
    await start_trading()
    await (await make_runner(settings, bus, reader, ScriptedLLM(scripts(last)))).run_cycle()
    h = await auth(client)
    pending = (await client.get("/api/approved", headers=h)).json()
    assert len(pending) == 1 and pending[0]["status"] == "PENDING"
    eng = TradingEngine(settings, bus, reader, PaperAdapter(reader, RiskParams.from_settings(settings)))
    await eng.tick()
    await set_ticker(bus, "BTC/USDT", last * 1.01)
    open_ = (await client.get("/api/positions", headers=h)).json()
    assert len(open_) == 1 and float(open_[0]["unrealized_pnl"]) > 0 and open_[0]["price_stale"] is False and open_[0]["current_price"]
    props = (await client.get("/api/proposals", headers=h)).json()
    detail = (await client.get(f"/api/proposals/{props[0]['id']}", headers=h)).json()
    assert props[0]["executed"] and detail["status"] == "APPROVED"
    assert set(detail["evidence"]) >= {"scout", "news", "technical", "analyst", "skeptic", "risk", "outcome", "execution"}
    assert [m["sender"] for m in detail["messages"]][-1] == "risk_manager" and detail["orders"][0]["status"] == "FILLED"
    assert detail["position"]["status"] == "OPEN" and len(detail["executions"]) == 6
    await set_ticker(bus, "BTC/USDT", last * 1.2)
    await eng.tick()
    closed = (await client.get("/api/positions?status=closed", headers=h)).json()
    ov = (await client.get("/api/overview", headers=h)).json()
    assert closed[0]["close_reason"] == "TAKE_PROFIT" and float(closed[0]["realized_pnl"]) > 0
    assert ov["stats"] == {**ov["stats"], "closed_trades": 1, "wins": 1, "win_rate": 100.0} and float(ov["portfolio"]["total_pnl"]) > 0
    assert len((await client.get("/api/orders", headers=h)).json()) == 2
    assert len((await client.get("/api/portfolio/history", headers=h)).json()) >= 1
    agents = (await client.get("/api/agents", headers=h)).json()
    assert agents[3]["last_execution"]["status"] == "SUCCESS" and agents[3]["last_execution"]["output"]["action"] == "BUY"
    hist = (await client.get("/api/agents/skeptic/executions", headers=h)).json()
    assert hist[0]["output"]["verdict"] == "APPROVE"
    risk = (await client.get("/api/risk", headers=h)).json()
    assert risk["usage"]["open_positions"] == 0 and risk["params"]["max_open_positions"] == 5
    feed_types = {e["type"] for e in (await client.get("/api/activity", headers=h)).json()}
    assert {"agent_message", "agent_execution", "decision", "execution", "cycle"} <= feed_types
    metrics = (await client.get("/metrics")).text
    assert "aegis_equity" in metrics and 'aegis_agent_executions{status="SUCCESS"} 5.0' in metrics  # news was SKIPPED (no stored news)


async def test_ip_allowlist_cors_and_global_rate_limit(bus, db):
    settings = make_settings(allowed_ips="10.0.0.0/8", rate_limit_per_minute=5, trust_proxy_headers=True)
    await bootstrap_admin(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(settings, bootstrap=False)), base_url="http://testserver") as c:
        assert (await c.get("/api/overview", headers={"X-Real-IP": "8.8.8.8"})).status_code == 403
        assert (await c.get("/api/health", headers={"X-Real-IP": "8.8.8.8"})).status_code == 200
        ok = {"X-Real-IP": "10.1.2.3"}
        codes = [(await c.get("/api/overview", headers=ok)).status_code for _ in range(7)]
        assert codes[:5] == [401] * 5 and codes[-1] == 429
        pre = await c.options("/api/overview", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "GET", "X-Real-IP": "10.9.9.9"})
        assert "access-control-allow-origin" not in pre.headers
        good = await c.options("/api/overview", headers={"Origin": "http://testserver", "Access-Control-Request-Method": "GET", "X-Real-IP": "10.9.9.8"})
        assert good.headers["access-control-allow-origin"] == "http://testserver"


def test_api_refuses_to_boot_with_weak_secrets():
    weak = make_settings(secret_key="short", admin_password="pw")
    assert len(weak.security_problems()) == 2 and make_settings().security_problems() == []


async def test_websocket_requires_auth_and_streams_live_events(settings, bus, db):
    await bootstrap_admin(settings)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(create_app(settings, bootstrap=False), host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    try:
        url = f"ws://127.0.0.1:{port}/api/ws"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "auth", "token": "bad"}))
            with pytest.raises(websockets.ConnectionClosed) as e:
                await ws.recv()
            assert e.value.rcvd.code == 4401
        with pytest.raises(websockets.InvalidStatus):
            async with websockets.connect(url, origin="http://evil.example"):
                pass
        from aegis.core.security import create_access_token
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"type": "auth", "token": create_access_token(settings, "admin")}))
            assert json.loads(await ws.recv())["type"] == "hello"
            await asyncio.sleep(0.1)
            await bus.publish("decision", {"symbol": "BTC/USDT", "status": "approved"})
            msg = json.loads(await asyncio.wait_for(ws.recv(), 3))
            assert msg["type"] == "decision" and msg["data"]["symbol"] == "BTC/USDT"
    finally:
        server.should_exit = True
        await task
