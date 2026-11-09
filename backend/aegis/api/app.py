"""HTTP + WebSocket API for the dashboard. Read models and operator controls only: this process never sends orders
and never calls an LLM."""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Gauge, generate_latest
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text

from aegis.agents.definitions import AGENTS
from aegis.api.deps import BusDep, SessionDep, SettingsDep, UserDep, audit, client_ip
from aegis.config import AGENT_NAMES, RiskParams, Settings, get_settings
from aegis.core.bus import get_bus
from aegis.core.logging import get_logger
from aegis.core.security import (
    create_access_token,
    decode_token,
    hash_password,
    ip_allowed,
    mask_secret,
    verify_password,
)
from aegis.core.state import (
    AgentConfig,
    NotificationConfig,
    TradingStatus,
    get_agent_configs,
    get_control,
    get_notification_config,
    get_risk_params,
    set_agent_config,
    set_notification_config,
    set_risk_params,
)
from aegis.core.timeutil import utcnow
from aegis.db.models import (
    AgentExecution,
    AgentMessage,
    ApprovedStatus,
    ApprovedTrade,
    AuditLog,
    Order,
    PortfolioSnapshot,
    Position,
    PositionStatus,
    RiskEvent,
    SystemEvent,
    TradeProposal,
    User,
    WorkflowRun,
)
from aegis.db.session import session_scope
from aegis.llm.providers import LLMRouter
from aegis.orchestration.runner import STATUS_KEY
from aegis.trading.context import load_portfolio
from aegis.trading.engine import operator_set_status
from aegis.trading.portfolio import max_drawdown_pct, unrealized_pnl

log = get_logger(__name__)
SERVICES = ("worker", "orchestrator", "trading-engine")
RUN_CYCLE_KEY = "aegis:cmd:run_cycle"


def row(obj: Any, *fields: str) -> dict[str, Any]:
    out = {}
    for f in fields:
        v = getattr(obj, f)
        out[f] = str(v) if isinstance(v, Decimal) else v.isoformat() if hasattr(v, "isoformat") else v
    return out


POSITION_FIELDS = ("id", "symbol", "mode", "status", "quantity", "entry_price", "stop_loss", "take_profit", "entry_fee", "exit_price", "exit_fee",
                   "realized_pnl", "close_reason", "opened_at", "closed_at", "approved_trade_id")
ORDER_FIELDS = ("id", "client_order_id", "exchange_order_id", "exchange", "mode", "symbol", "side", "order_type", "purpose", "quantity", "reference_price",
                "filled_quantity", "avg_fill_price", "fee", "status", "error", "close_reason", "position_id", "approved_trade_id", "created_at")
PROPOSAL_FIELDS = ("id", "run_id", "symbol", "side", "entry_price", "stop_loss", "take_profit", "risk_reward", "holding_period", "confidence", "status", "created_at")
EXEC_FIELDS = ("id", "run_id", "agent", "symbol", "status", "provider", "model", "llm_calls", "prompt_tokens", "completion_tokens", "duration_ms",
               "confidence", "error", "created_at", "finished_at")


# =====================================================================================================
# schemas
# =====================================================================================================
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class EmergencyIn(BaseModel):
    liquidate: bool = False
    reason: str = Field(default="", max_length=300)


class ResumeIn(BaseModel):
    confirm: bool = False


class AgentConfigIn(BaseModel):
    provider: str = Field(pattern="^(openai|xai|custom)$")
    model: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:/\-]+$")
    enabled: bool = True
    temperature: float = Field(default=0.2, ge=0.0, le=1.0)


# =====================================================================================================
# routes
# =====================================================================================================
public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api")


@public.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@public.get("/ready")
async def ready(bus: BusDep) -> JSONResponse:
    db_ok = True
    try:
        async with session_scope() as s:
            await s.execute(text("select 1"))
    except Exception:
        db_ok = False
    redis_ok = await bus.ping()
    return JSONResponse({"database": db_ok, "redis": redis_ok}, status_code=200 if db_ok and redis_ok else 503)


@public.post("/auth/login")
async def login(body: LoginIn, request: Request, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    ip = client_ip(request, settings) or "unknown"
    if await bus.incr_window(f"aegis:rl:login:{ip}:{int(time.time() // 60)}", 120) > settings.login_rate_limit_per_minute:
        raise HTTPException(429, "too many login attempts")
    user = (await session.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    ok = bool(user and user.is_active and verify_password(body.password, user.password_hash))
    if not user:
        verify_password(body.password, _DUMMY_HASH)  # equalise timing for unknown users
    await audit(session, body.username[:64], "login_success" if ok else "login_failed", request, settings)
    if not ok:
        await session.commit()
        raise HTTPException(401, "invalid credentials")
    assert user is not None
    user.last_login_at = utcnow()
    return {"access_token": create_access_token(settings, user.username), "token_type": "bearer", "expires_in": settings.access_token_minutes * 60}


_DUMMY_HASH = hash_password("timing-equaliser")


@api.get("/auth/me")
async def me(user: UserDep) -> dict[str, str]:
    return {"username": user}


async def _system_block(session: Any, settings: Settings, bus: Any) -> dict[str, Any]:
    control = await get_control(session)
    hb = await bus.heartbeats()
    now = time.time()
    return {
        "mode": settings.trading_mode.value, "is_paper": settings.is_paper, "control": control.model_dump(mode="json"),
        "services": {name: {"online": name in hb and now - hb[name]["ts"] < 90, **({k: v for k, v in hb[name].items() if k != "ts"} if name in hb else {})} for name in SERVICES},
        "market_data": await bus.cache_get("md_status") or {"healthy": False, "reason": "no report from market data service"},
        "orchestrator": await bus.cache_get(STATUS_KEY),
    }


@api.get("/overview")
async def overview(user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    mode = settings.trading_mode.value
    portfolio, params = await load_portfolio(session, settings, bus)
    closed = (await session.execute(select(Position.realized_pnl).where(Position.mode == mode, Position.status == PositionStatus.CLOSED))).scalars().all()
    wins = sum(1 for p in closed if p is not None and p > 0)
    hist_dd = await max_drawdown_pct(session, mode)
    return {
        "system": await _system_block(session, settings, bus),
        "portfolio": {**portfolio.model_dump(mode="json"), "daily_loss_pct": str(portfolio.daily_loss_pct), "exposure_pct": str(portfolio.exposure_pct),
                      "total_pnl": str(portfolio.equity - params.starting_capital)},
        "stats": {"closed_trades": len(closed), "wins": wins, "losses": len(closed) - wins,
                  "win_rate": round(wins / len(closed) * 100, 2) if closed else None,
                  "open_positions": len(portfolio.open_symbols), "max_drawdown_pct": str(max(hist_dd, portfolio.drawdown_pct))},
    }


@api.get("/portfolio/history")
async def portfolio_history(user: UserDep, session: SessionDep, settings: SettingsDep, hours: int = 168) -> list[dict[str, Any]]:
    hours = max(1, min(hours, 24 * 90))
    rows = (await session.execute(
        select(PortfolioSnapshot).where(PortfolioSnapshot.mode == settings.trading_mode.value, PortfolioSnapshot.created_at >= utcnow() - timedelta(hours=hours))
        .order_by(PortfolioSnapshot.created_at.asc()))).scalars().all()
    step = max(1, len(rows) // 500)  # downsample for the chart
    return [{"t": r.created_at.isoformat(), "equity": float(r.equity), "cash": float(r.cash), "drawdown_pct": float(r.drawdown_pct)} for r in rows[::step]]


@api.get("/agents")
async def agents(user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> list[dict[str, Any]]:
    configs = await get_agent_configs(session, settings)
    hb = await bus.heartbeats()
    orch_online = "orchestrator" in hb and time.time() - hb["orchestrator"]["ts"] < 90
    out = []
    for name in AGENT_NAMES:
        a, cfg = AGENTS[name], configs[name]
        last = (await session.execute(select(AgentExecution).where(AgentExecution.agent == name).order_by(AgentExecution.id.desc()).limit(1))).scalar_one_or_none()
        status = await bus.get_agent_status(name) or {}
        configured = settings.provider_configured(cfg.provider)
        out.append({
            "name": name, "title": a.title, "role": a.role, "tools": a.tools, "provider": cfg.provider, "model": cfg.model, "enabled": cfg.enabled,
            "provider_configured": configured, "online": bool(orch_online and configured and cfg.enabled),
            "offline_reason": None if orch_online and configured and cfg.enabled else
            ("agent disabled" if not cfg.enabled else f"no API key for provider '{cfg.provider}'" if not configured else "orchestrator service offline"),
            "state": status.get("state", "idle"), "current_task": status.get("task") if status.get("state") == "working" else None,
            "last_execution": {**row(last, *EXEC_FIELDS), "output": last.output} if last else None,
        })
    return out


@api.get("/agents/{name}/executions")
async def agent_executions(name: str, user: UserDep, session: SessionDep, limit: int = 30) -> list[dict[str, Any]]:
    if name not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    rows = (await session.execute(select(AgentExecution).where(AgentExecution.agent == name).order_by(AgentExecution.id.desc()).limit(min(limit, 100)))).scalars().all()
    return [{**row(r, *EXEC_FIELDS), "output": r.output} for r in rows]


@api.get("/agents/executions/{exec_id}")
async def agent_execution(exec_id: int, user: UserDep, session: SessionDep) -> dict[str, Any]:
    r = await session.get(AgentExecution, exec_id)
    if not r:
        raise HTTPException(404, "not found")
    return {**row(r, *EXEC_FIELDS), "input": r.input, "output": r.output}


@api.get("/activity")
async def activity(user: UserDep, bus: BusDep, limit: int = 100) -> list[dict[str, Any]]:
    return await bus.recent_feed(min(limit, 300))


@api.get("/positions")
async def positions(user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
    q = select(Position).where(Position.mode == settings.trading_mode.value)
    if status == "open":
        q = q.where(Position.status.in_([PositionStatus.OPEN, PositionStatus.CLOSING])).order_by(Position.id.desc())
    else:
        q = q.where(Position.status == PositionStatus.CLOSED).order_by(Position.closed_at.desc())
    rows = (await session.execute(q.limit(min(limit, 500)))).scalars().all()
    params = await get_risk_params(session, settings)
    tickers = await bus.get_tickers() if status == "open" else {}
    out = []
    for p in rows:
        d = row(p, *POSITION_FIELDS)
        if status == "open":
            t = tickers.get(p.symbol) or {}
            fresh = bool(t.get("last")) and time.time() - float(t.get("ts") or 0) <= settings.stale_ticker_seconds * 2
            price = Decimal(str(t["last"])) if t.get("last") else None
            d.update(current_price=str(price) if price is not None else None, price_stale=not fresh,
                     unrealized_pnl=str(unrealized_pnl(p, price, params.fee_rate)) if price is not None else None)
        out.append(d)
    return out


@api.get("/orders")
async def orders(user: UserDep, session: SessionDep, limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(select(Order).order_by(Order.id.desc()).limit(min(limit, 500)))).scalars().all()
    return [row(o, *ORDER_FIELDS) for o in rows]


@api.get("/approved")
async def approved(user: UserDep, session: SessionDep, pending_only: bool = True, limit: int = 50) -> list[dict[str, Any]]:
    q = select(ApprovedTrade).order_by(ApprovedTrade.id.desc()).limit(min(limit, 200))
    if pending_only:
        q = q.where(ApprovedTrade.status.in_([ApprovedStatus.PENDING, ApprovedStatus.EXECUTING]))
    return [row(a, "id", "proposal_id", "symbol", "side", "quantity", "entry_price", "stop_loss", "take_profit", "risk_amount", "status",
                "status_reason", "expires_at", "executed_at", "created_at") for a in (await session.execute(q)).scalars()]


@api.get("/proposals")
async def proposals(user: UserDep, session: SessionDep, limit: int = 50) -> list[dict[str, Any]]:
    rows = (await session.execute(select(TradeProposal).order_by(TradeProposal.id.desc()).limit(min(limit, 200)))).scalars().all()
    return [{**row(p, *PROPOSAL_FIELDS), "outcome": (p.evidence or {}).get("outcome"), "executed": "execution" in (p.evidence or {})} for p in rows]


@api.get("/proposals/{proposal_id}")
async def proposal_detail(proposal_id: int, user: UserDep, session: SessionDep) -> dict[str, Any]:
    p = await session.get(TradeProposal, proposal_id)
    if not p:
        raise HTTPException(404, "not found")
    a = (await session.execute(select(ApprovedTrade).where(ApprovedTrade.proposal_id == p.id))).scalar_one_or_none()
    ords = (await session.execute(select(Order).where(Order.approved_trade_id == a.id))).scalars().all() if a else []
    pos = (await session.execute(select(Position).where(Position.approved_trade_id == a.id))).scalar_one_or_none() if a else None
    exits = (await session.execute(select(Order).where(Order.position_id == pos.id, Order.purpose == "EXIT"))).scalars().all() if pos else []
    msgs = (await session.execute(select(AgentMessage).where(AgentMessage.run_id == p.run_id, AgentMessage.symbol == p.symbol).order_by(AgentMessage.id))).scalars().all() if p.run_id else []
    execs = (await session.execute(select(AgentExecution).where(AgentExecution.run_id == p.run_id).order_by(AgentExecution.id))).scalars().all() if p.run_id else []
    return {
        **row(p, *PROPOSAL_FIELDS), "evidence": p.evidence,
        "approved_trade": row(a, "id", "quantity", "risk_amount", "status", "status_reason", "expires_at", "executed_at") if a else None,
        "orders": [row(o, *ORDER_FIELDS) for o in [*ords, *exits]], "position": row(pos, *POSITION_FIELDS) if pos else None,
        "messages": [row(m, "id", "sender", "recipient", "kind", "summary", "created_at") for m in msgs],
        "executions": [row(e, *EXEC_FIELDS) for e in execs if e.symbol in (None, p.symbol)],
    }


@api.get("/runs")
async def runs(user: UserDep, session: SessionDep, limit: int = 30) -> list[dict[str, Any]]:
    rows = (await session.execute(select(WorkflowRun).order_by(WorkflowRun.id.desc()).limit(min(limit, 100)))).scalars().all()
    return [{**row(r, "id", "run_uid", "status", "trigger", "created_at", "finished_at"), "summary": r.summary, "trace": r.trace} for r in rows]


@api.get("/risk")
async def risk(user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    portfolio, params = await load_portfolio(session, settings, bus)
    events = (await session.execute(select(RiskEvent).order_by(RiskEvent.id.desc()).limit(100))).scalars().all()
    sys_events = (await session.execute(select(SystemEvent).order_by(SystemEvent.id.desc()).limit(50))).scalars().all()
    equity = portfolio.equity
    return {
        "control": (await get_control(session)).model_dump(mode="json"), "params": params.model_dump(mode="json"),
        "usage": {
            "equity": str(equity), "exposure": str(portfolio.exposure), "exposure_pct": str(portfolio.exposure_pct),
            "daily_pnl": str(portfolio.daily_pnl), "daily_loss_pct": str(portfolio.daily_loss_pct), "drawdown_pct": str(portfolio.drawdown_pct),
            "open_positions": portfolio.open_positions, "risk_per_trade_amount": str(equity * params.risk_per_trade_pct / 100),
            "max_exposure_amount": str(equity * params.max_exposure_pct / 100),
            "daily_loss_limit_amount": str(portfolio.day_start_equity * params.max_daily_loss_pct / 100), "stale_marks": portfolio.stale_marks,
        },
        "risk_events": [row(e, "id", "kind", "severity", "symbol", "message", "created_at") for e in events],
        "system_events": [row(e, "id", "service", "kind", "severity", "message", "created_at") for e in sys_events],
    }


# ---- settings -------------------------------------------------------------------------------------------
@api.get("/settings")
async def get_settings_view(user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    """Secrets are never returned: only whether they are configured, plus the last 4 characters for identification."""
    def provider(name: str, url: str, key: str, model: str) -> dict[str, Any]:
        return {"name": name, "base_url": url, "configured": settings.provider_configured(name), "key_hint": mask_secret(key), "default_model": model}

    router = LLMRouter(settings, bus, providers={})
    return {
        "trading_mode": settings.trading_mode.value, "live_trading_enabled": settings.live_trading_enabled, "mode_problems": settings.mode_problems(),
        "providers": [
            provider("openai", settings.openai_base_url, settings.openai_api_key.get_secret_value(), settings.openai_default_model),
            provider("xai", settings.xai_base_url, settings.xai_api_key.get_secret_value(), settings.xai_default_model),
            provider("custom", settings.custom_llm_base_url, settings.custom_llm_api_key.get_secret_value(), settings.custom_llm_default_model),
        ],
        "llm": {"daily_call_budget": settings.llm_daily_call_budget, "calls_today": await router.calls_today(), "cycle_interval_seconds": settings.cycle_interval_seconds},
        "agents": {n: c.model_dump() for n, c in (await get_agent_configs(session, settings)).items()},
        "exchange": {
            "market_data_source": settings.data_source, "quote_asset": settings.quote_asset,
            "testnet_credentials": {"configured": bool(settings.binance_testnet_api_key.get_secret_value()), "key_hint": mask_secret(settings.binance_testnet_api_key.get_secret_value())},
            "live_credentials": {"configured": bool(settings.binance_live_api_key.get_secret_value())},
            "universe_max_symbols": settings.universe_max_symbols, "timeframes": settings.timeframes,
        },
        "risk": (await get_risk_params(session, settings)).model_dump(mode="json"),
        "notifications": {**(await get_notification_config(session)).model_dump(),
                          "telegram_configured": bool(settings.telegram_bot_token.get_secret_value() and settings.telegram_chat_id),
                          "webhook_configured": bool(settings.notify_webhook_url.get_secret_value())},
        "security": {"ip_allowlist_active": bool(settings.allowed_ips.strip()), "cors_origins": settings.cors_origin_list},
    }


@api.put("/settings/risk")
async def put_risk(body: RiskParams, request: Request, user: UserDep, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    if body.min_stop_distance_pct >= body.max_stop_distance_pct:
        raise HTTPException(422, "min_stop_distance_pct must be below max_stop_distance_pct")
    before = await get_risk_params(session, settings)
    await set_risk_params(session, body)
    await audit(session, user, "risk_params_changed", request, settings, before=before.model_dump(mode="json"), after=body.model_dump(mode="json"))
    return body.model_dump(mode="json")


@api.put("/settings/agents/{name}")
async def put_agent(name: str, body: AgentConfigIn, request: Request, user: UserDep, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    if name not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    cfg = AgentConfig(**body.model_dump())
    await set_agent_config(session, name, cfg)
    await audit(session, user, "agent_config_changed", request, settings, agent=name, **cfg.model_dump())
    return cfg.model_dump()


@api.put("/settings/notifications")
async def put_notifications(body: NotificationConfig, request: Request, user: UserDep, session: SessionDep, settings: SettingsDep) -> dict[str, Any]:
    await set_notification_config(session, body)
    await audit(session, user, "notifications_changed", request, settings, **body.model_dump())
    return body.model_dump()


@api.get("/audit")
async def audit_log(user: UserDep, session: SessionDep, limit: int = 100) -> list[dict[str, Any]]:
    rows = (await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500)))).scalars().all()
    return [{**row(a, "id", "actor", "action", "ip", "created_at"), "detail": a.detail} for a in rows]


# ---- operator controls -----------------------------------------------------------------------------------
async def _transition(session: Any, bus: Any, settings: Settings, request: Request, user: str, action: str, target: TradingStatus,
                      allowed_from: set[TradingStatus], **kw: Any) -> dict[str, Any]:
    current = await get_control(session)
    if current.status not in allowed_from:
        raise HTTPException(409, f"cannot {action} while trading status is {current.status}")
    problems = settings.mode_problems()
    if target == TradingStatus.RUNNING and problems:
        raise HTTPException(409, "trading mode is not runnable: " + "; ".join(problems))
    c = await operator_set_status(session, bus, target, by=user, **kw)
    await audit(session, user, f"control_{action}", request, settings, previous=current.status, **kw)
    return c.model_dump(mode="json")


@api.post("/control/start")
async def control_start(request: Request, user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    return await _transition(session, bus, settings, request, user, "start", TradingStatus.RUNNING, {TradingStatus.STOPPED})


@api.post("/control/pause")
async def control_pause(request: Request, user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    return await _transition(session, bus, settings, request, user, "pause", TradingStatus.PAUSED, {TradingStatus.RUNNING})


@api.post("/control/resume")
async def control_resume(body: ResumeIn, request: Request, user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    current = await get_control(session)
    if current.status in (TradingStatus.SUSPENDED, TradingStatus.EMERGENCY_STOP) and not body.confirm:
        raise HTTPException(409, f"resuming from {current.status} requires explicit confirmation (reason: {current.reason or 'n/a'})")
    return await _transition(session, bus, settings, request, user, "resume", TradingStatus.RUNNING,
                             {TradingStatus.PAUSED, TradingStatus.SUSPENDED, TradingStatus.EMERGENCY_STOP})


@api.post("/control/emergency-stop")
async def control_emergency(body: EmergencyIn, request: Request, user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    c = await operator_set_status(session, bus, TradingStatus.EMERGENCY_STOP, by=user, reason=body.reason or "operator emergency stop", liquidate=body.liquidate)
    await audit(session, user, "control_emergency_stop", request, settings, liquidate=body.liquidate, reason=body.reason)
    return c.model_dump(mode="json")


@api.post("/control/run-cycle")
async def control_run_cycle(request: Request, user: UserDep, session: SessionDep, settings: SettingsDep, bus: BusDep) -> dict[str, Any]:
    """Ask the orchestrator to run an analysis cycle now (it still performs every pre-flight check)."""
    if await bus.incr_window(f"aegis:rl:cycle:{int(time.time() // 60)}", 120) > 2:
        raise HTTPException(429, "manual cycles are limited to 2 per minute")
    await bus.r.set(RUN_CYCLE_KEY, user, ex=120)
    await audit(session, user, "manual_cycle_requested", request, settings)
    return {"queued": True}


# =====================================================================================================
# websocket
# =====================================================================================================
async def ws_endpoint(ws: WebSocket) -> None:
    settings, bus = ws.app.state.settings, get_bus()
    ip = (ws.headers.get("x-real-ip") if settings.trust_proxy_headers else None) or (ws.client.host if ws.client else None)
    origin = ws.headers.get("origin")
    if not ip_allowed(ip, settings.allowed_ips) or (origin and origin not in settings.cors_origin_list):
        await ws.close(code=4403)
        return
    await ws.accept()
    try:  # the token travels in the first frame, never in the URL (URLs end up in access logs)
        first = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))
        if first.get("type") != "auth" or not decode_token(settings, str(first.get("token", ""))):
            raise ValueError("unauthenticated")
    except Exception:
        await ws.close(code=4401)
        return
    await ws.send_text(json.dumps({"type": "hello", "data": {"mode": settings.trading_mode.value}}))

    async def pump() -> None:
        async for msg in bus.subscribe():
            await ws.send_text(msg)

    task = asyncio.create_task(pump())
    try:
        while True:
            await ws.receive_text()  # keep-alive pings from the client; content ignored
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task


# =====================================================================================================
# app factory
# =====================================================================================================
async def bootstrap_admin(settings: Settings) -> None:
    """Create/refresh the single operator account from the environment."""
    async with session_scope() as s:
        user = (await s.execute(select(User).where(User.username == settings.admin_username))).scalar_one_or_none()
        pw = settings.admin_password.get_secret_value()
        if user is None:
            s.add(User(username=settings.admin_username, password_hash=hash_password(pw)))
        elif not verify_password(pw, user.password_hash):
            user.password_hash = hash_password(pw)


def create_app(settings: Settings | None = None, *, bootstrap: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if bootstrap:
            problems = settings.security_problems()
            if problems:
                raise RuntimeError("refusing to start API: " + "; ".join(problems))
            await bootstrap_admin(settings)
        yield

    app = FastAPI(title="Aegis Trading API", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs" if settings.environment != "production" else None, redoc_url=None,
                  openapi_url="/api/openapi.json" if settings.environment != "production" else None)
    app.state.settings = settings
    app.dependency_overrides[get_settings] = lambda: settings
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        ip = client_ip(request, settings)
        path = request.url.path
        if path not in ("/api/health", "/api/ready"):
            if not ip_allowed(ip, settings.allowed_ips):
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            try:
                n = await get_bus().incr_window(f"aegis:rl:{ip}:{int(time.time() // 60)}", 120)
            except Exception:
                n = 0  # Redis outage must not lock the operator out of the emergency stop
            if n > settings.rate_limit_per_minute:
                return JSONResponse({"detail": "rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
        response: Response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                                 "Cache-Control": "no-store"})
        return response

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        """Prometheus metrics. Not routed by the public reverse proxy; scrape it on the internal network."""
        reg = CollectorRegistry()
        async with session_scope() as s:
            portfolio, _ = await load_portfolio(s, settings, get_bus())
            control = await get_control(s)
            counts = dict((await s.execute(select(AgentExecution.status, func.count()).group_by(AgentExecution.status))).all())
        Gauge("aegis_equity", "Portfolio equity (quote currency)", registry=reg).set(float(portfolio.equity))
        Gauge("aegis_drawdown_pct", "Current drawdown percent", registry=reg).set(float(portfolio.drawdown_pct))
        Gauge("aegis_daily_pnl", "PnL since UTC day start", registry=reg).set(float(portfolio.daily_pnl))
        Gauge("aegis_open_positions", "Open positions", registry=reg).set(len(portfolio.open_symbols))
        Gauge("aegis_trading_running", "1 if trading status is RUNNING", registry=reg).set(1 if control.status == TradingStatus.RUNNING else 0)
        g = Gauge("aegis_agent_executions", "Agent executions by status", ["status"], registry=reg)
        for st, n in counts.items():
            g.labels(status=str(st)).set(n)
        hb = await get_bus().heartbeats()
        up = Gauge("aegis_service_up", "Service heartbeat fresh", ["service"], registry=reg)
        for name in SERVICES:
            up.labels(service=name).set(1 if name in hb and time.time() - hb[name]["ts"] < 90 else 0)
        return Response(generate_latest(reg), media_type=CONTENT_TYPE_LATEST)

    app.include_router(public)
    app.include_router(api)
    app.add_api_websocket_route("/api/ws", ws_endpoint)
    return app
