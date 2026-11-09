"""Runs agent cycles on a schedule and owns their lifecycle (pre-flight, tracing, recovery)."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy import update

from aegis.agents.base import AgentContext
from aegis.config import AGENT_NAMES, Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.core.state import TradingStatus, get_agent_configs, get_control, record_system_event
from aegis.core.timeutil import utcnow
from aegis.db.models import AgentExecution, ExecStatus, RunStatus, Severity, WorkflowRun
from aegis.db.session import session_scope
from aegis.llm.providers import LLMRouter
from aegis.marketdata.service import MarketDataReader
from aegis.orchestration.graph import Deps, build_graph

log = get_logger(__name__)
STATUS_KEY = "orchestrator_status"


class CycleRunner:
    def __init__(self, settings: Settings, bus: Bus, reader: MarketDataReader, router: LLMRouter, agents: dict | None = None) -> None:
        self.settings, self.bus, self.reader, self.router, self.agents = settings, bus, reader, router, agents
        self._lock = asyncio.Lock()

    async def recover(self) -> None:
        """Mark work interrupted by a crash. Interrupted cycles have no financial side effects: approval is written
        in a single transaction at the very end of a candidate's flow."""
        async with session_scope() as s:
            r1 = await s.execute(update(WorkflowRun).where(WorkflowRun.status == RunStatus.RUNNING).values(status=RunStatus.ABORTED, finished_at=utcnow()))
            r2 = await s.execute(update(AgentExecution).where(AgentExecution.status == ExecStatus.RUNNING)
                                 .values(status=ExecStatus.ERROR, error="interrupted by restart", finished_at=utcnow()))
            if r1.rowcount or r2.rowcount:
                await record_system_event(s, self.bus, "orchestrator", "recovery", f"marked {r1.rowcount} runs and {r2.rowcount} agent executions as interrupted",
                                          severity=Severity.WARNING)

    async def preflight(self) -> tuple[bool, str]:
        """Free checks that decide whether paid LLM work is worth starting."""
        async with session_scope() as s:
            control = await get_control(s)
            configs = await get_agent_configs(s, self.settings)
        if control.status != TradingStatus.RUNNING:
            return False, f"trading status is {control.status}"
        md = await self.reader.status()
        if not md.get("healthy"):
            return False, f"market data unhealthy: {md.get('reason')}"
        missing = sorted({c.provider for n, c in configs.items() if c.enabled and c.provider not in self.router.available()})
        disabled = [n for n in AGENT_NAMES if not configs[n].enabled]
        if disabled:
            return False, f"agents disabled: {', '.join(disabled)} (all six are required)"
        if missing:
            return False, f"LLM provider(s) not configured: {', '.join(missing)}"
        if await self.router.calls_today() >= self.settings.llm_daily_call_budget > 0:
            return False, "daily LLM call budget reached"
        return True, "ok"

    async def run_cycle(self, trigger: str = "schedule") -> dict[str, Any]:
        if self._lock.locked():
            return {"ran": False, "reason": "a cycle is already running"}
        async with self._lock:
            ok, reason = await self.preflight()
            if not ok:
                await self._set_status("idle", reason)
                return {"ran": False, "reason": reason}
            async with session_scope() as s:
                configs = await get_agent_configs(s, self.settings)
                run = WorkflowRun(run_uid=uuid.uuid4().hex, status=RunStatus.RUNNING, trigger=trigger)
                s.add(run)
                await s.flush()
                run_id = run.id
            await self._set_status("running", f"cycle {run_id}")
            await self.bus.publish("cycle", {"run_id": run_id, "status": "started", "trigger": trigger})
            ctx = AgentContext(settings=self.settings, router=self.router, bus=self.bus, configs=configs, run_id=run_id)
            graph = build_graph(Deps(self.settings, self.bus, self.reader, ctx, self.agents))
            status, final, error = RunStatus.COMPLETED, {}, None
            try:
                final = await graph.ainvoke({"run_id": run_id, "results": [], "trace": []}, {"recursion_limit": 100})
            except Exception as e:
                status, error = RunStatus.FAILED, f"{type(e).__name__}: {e}"
                log.exception("cycle_failed", run_id=run_id)
            results = final.get("results", [])
            summary = {
                "candidates": len(final.get("candidates") or []), "scout": final.get("scout"),
                "results": results, "approved": sum(1 for r in results if r["status"] == "approved"),
                "rejected": sum(1 for r in results if r["status"] == "rejected"),
                "no_trade": sum(1 for r in results if r["status"] == "no_trade"), "error": error,
            }
            async with session_scope() as s:
                row = await s.get(WorkflowRun, run_id)
                assert row is not None
                row.status, row.summary, row.trace, row.finished_at = status, summary, final.get("trace", []), utcnow()
                if error:
                    await record_system_event(s, self.bus, "orchestrator", "cycle_failed", error, severity=Severity.CRITICAL)
            await self.bus.publish("cycle", {"run_id": run_id, "status": status, **{k: summary[k] for k in ("candidates", "approved", "rejected", "no_trade")}})
            await self._set_status("idle", f"last cycle {run_id}: {status}")
            return {"ran": True, "run_id": run_id, "status": status, **summary}

    async def _set_status(self, state: str, detail: str) -> None:
        await self.bus.cache_set(STATUS_KEY, {"state": state, "detail": detail, "at": utcnow().isoformat()}, ttl=86400)
