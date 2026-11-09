"""Durable control state (PostgreSQL). All services read it before acting."""
from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aegis.config import AGENT_NAMES, RiskParams, Settings, get_settings
from aegis.core.bus import Bus
from aegis.core.timeutil import utcnow
from aegis.db.models import RiskEvent, Severity, SystemEvent, SystemState


class TradingStatus(StrEnum):
    STOPPED = "STOPPED"                # never started / stopped by operator
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"                  # operator pause: no new entries, exits still managed
    SUSPENDED = "SUSPENDED"            # automatic: risk limit or critical error; operator must resume
    EMERGENCY_STOP = "EMERGENCY_STOP"  # operator kill switch


class Control(BaseModel):
    status: TradingStatus = TradingStatus.STOPPED
    reason: str = ""
    updated_by: str = "system"
    updated_at: str = ""
    liquidate: bool = False  # emergency stop asked the engine to close every open position

    @property
    def allows_new_entries(self) -> bool:
        return self.status == TradingStatus.RUNNING


class AgentConfig(BaseModel):
    provider: str
    model: str
    enabled: bool = True
    temperature: float = 0.2


class NotificationConfig(BaseModel):
    enabled: bool = True
    on_trade: bool = True
    on_risk_event: bool = True
    on_system_error: bool = True


async def _get(session: AsyncSession, key: str) -> dict[str, Any] | None:
    row = await session.get(SystemState, key)
    return dict(row.value) if row else None


async def _set(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await session.get(SystemState, key)
    if row:
        row.value = value
        row.updated_at = utcnow()
    else:
        session.add(SystemState(key=key, value=value))
    await session.flush()


async def get_control(session: AsyncSession) -> Control:
    return Control(**(await _get(session, "control") or {}))


async def set_control(
    session: AsyncSession, status: TradingStatus, *, reason: str = "", by: str = "system", liquidate: bool = False
) -> Control:
    c = Control(status=status, reason=reason, updated_by=by, updated_at=utcnow().isoformat(), liquidate=liquidate)
    await _set(session, "control", c.model_dump(mode="json"))
    return c


async def suspend_trading(session: AsyncSession, reason: str, *, by: str = "system") -> bool:
    """Automatic suspension. Only downgrades RUNNING; never overrides an operator stop. Returns True if changed."""
    current = await get_control(session)
    if current.status != TradingStatus.RUNNING:
        return False
    await set_control(session, TradingStatus.SUSPENDED, reason=reason, by=by)
    return True


async def get_risk_params(session: AsyncSession, settings: Settings | None = None) -> RiskParams:
    base = RiskParams.from_settings(settings or get_settings()).model_dump(mode="json")
    base.update(await _get(session, "risk_params") or {})
    return RiskParams(**base)


async def set_risk_params(session: AsyncSession, params: RiskParams) -> None:
    await _set(session, "risk_params", params.model_dump(mode="json"))


def default_agent_config(agent: str, settings: Settings) -> AgentConfig:
    spec = settings.agent_model_spec(agent)
    provider, _, model = spec.partition(":") if spec else (settings.llm_default_provider, "", "")
    if not model:
        model = {
            "openai": settings.openai_default_model,
            "xai": settings.xai_default_model,
            "custom": settings.custom_llm_default_model,
        }.get(provider, "")
    return AgentConfig(provider=provider, model=model)


async def get_agent_configs(session: AsyncSession, settings: Settings | None = None) -> dict[str, AgentConfig]:
    settings = settings or get_settings()
    stored = await _get(session, "agent_config") or {}
    out: dict[str, AgentConfig] = {}
    for name in AGENT_NAMES:
        cfg = default_agent_config(name, settings)
        if name in stored:
            cfg = AgentConfig(**{**cfg.model_dump(), **stored[name]})
        out[name] = cfg
    return out


async def set_agent_config(session: AsyncSession, agent: str, cfg: AgentConfig) -> None:
    stored = await _get(session, "agent_config") or {}
    stored[agent] = cfg.model_dump()
    await _set(session, "agent_config", stored)


async def get_notification_config(session: AsyncSession) -> NotificationConfig:
    return NotificationConfig(**(await _get(session, "notifications") or {}))


async def set_notification_config(session: AsyncSession, cfg: NotificationConfig) -> None:
    await _set(session, "notifications", cfg.model_dump())


async def get_value(session: AsyncSession, key: str) -> dict[str, Any] | None:
    return await _get(session, key)


async def set_value(session: AsyncSession, key: str, value: dict[str, Any]) -> None:
    await _set(session, key, value)


async def record_risk_event(
    session: AsyncSession, bus: Bus | None, kind: str, message: str, *,
    severity: Severity = Severity.WARNING, symbol: str | None = None, detail: dict[str, Any] | None = None,
) -> RiskEvent:
    ev = RiskEvent(kind=kind, severity=severity, symbol=symbol, message=message, detail=detail or {})
    session.add(ev)
    await session.flush()
    if bus:
        await bus.publish("risk_event", {"id": ev.id, "kind": kind, "severity": severity, "symbol": symbol, "message": message})
    return ev


async def record_system_event(
    session: AsyncSession, bus: Bus | None, service: str, kind: str, message: str, *,
    severity: Severity = Severity.INFO, detail: dict[str, Any] | None = None,
) -> SystemEvent:
    ev = SystemEvent(service=service, kind=kind, severity=severity, message=message, detail=detail or {})
    session.add(ev)
    await session.flush()
    if bus:
        await bus.publish("system_event", {"id": ev.id, "service": service, "kind": kind, "severity": severity, "message": message})
    return ev


async def all_state(session: AsyncSession) -> dict[str, Any]:
    return {r.key: r.value for r in (await session.execute(select(SystemState))).scalars()}
