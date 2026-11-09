"""Agent runtime: one real LLM call (plus bounded retries) per execution, validated structured output,
timeouts, persisted execution history and live status."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ValidationError

from aegis.config import Settings
from aegis.core.bus import Bus, dumps
from aegis.core.logging import get_logger
from aegis.core.state import AgentConfig
from aegis.core.timeutil import utcnow
from aegis.db.models import AgentExecution, ExecStatus
from aegis.db.session import session_scope
from aegis.llm.providers import LLMBudgetExceeded, LLMError, LLMNotConfigured, LLMRouter

log = get_logger(__name__)
In = TypeVar("In", bound=BaseModel)
Out = TypeVar("Out", bound=BaseModel)

COMMON_RULES = """
Operating rules (non-negotiable):
- Use ONLY the data in the user message. Never invent prices, news, events, indicators or sources.
- If the data is insufficient, say so through the schema (low confidence / neutral / no trade). Do not guess.
- Respond with a single JSON object that matches the provided JSON Schema exactly. No prose, no markdown.
- You cannot place orders or change risk limits. Deterministic code outside your control enforces them.
"""


class AgentError(Exception):
    def __init__(self, message: str, status: ExecStatus = ExecStatus.ERROR) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class AgentContext:
    settings: Settings
    router: LLMRouter
    bus: Bus | None
    configs: dict[str, AgentConfig]
    run_id: int | None = None
    persist: bool = True


@dataclass
class AgentResult(Generic[Out]):
    output: Out
    execution_id: int | None
    llm_calls: int
    duration_ms: int
    skipped: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model response")
    return json.loads(text[start : end + 1])


class Agent(Generic[In, Out]):
    name: ClassVar[str]
    title: ClassVar[str]
    role: ClassVar[str]
    system_prompt: ClassVar[str]
    tools: ClassVar[list[str]] = []        # deterministic data tools whose results form this agent's input
    input_model: ClassVar[type[BaseModel]]
    output_model: ClassVar[type[BaseModel]]

    # ---- hooks ------------------------------------------------------------------------------------
    def skip(self, inp: In) -> tuple[Out, str] | None:
        """Return (output, reason) to avoid a pointless LLM call (e.g. there is no data to reason about)."""
        return None

    def post_validate(self, inp: In, out: Out) -> Out:
        """Semantic validation against the input. Raise ValueError to request a corrected answer."""
        return out

    def confidence_of(self, out: Out) -> float | None:
        return getattr(out, "confidence", None)

    def symbol_of(self, inp: In) -> str | None:
        return getattr(inp, "symbol", None)

    def build_user_prompt(self, inp: In) -> str:
        return (
            "INPUT DATA (JSON):\n" + dumps(inp.model_dump(mode="json"))
            + "\n\nOUTPUT JSON SCHEMA:\n" + json.dumps(self.output_model.model_json_schema(), separators=(",", ":"))
            + "\n\nReturn only the JSON object."
        )

    # ---- execution --------------------------------------------------------------------------------
    async def run(self, inp: In, ctx: AgentContext) -> AgentResult[Out]:
        cfg = ctx.configs[self.name]
        started = time.monotonic()
        symbol = self.symbol_of(inp)
        exec_id = await self._start(ctx, inp, cfg, symbol)
        await self._status(ctx, "working", f"{self.title}: analysing {symbol or 'market'}")
        llm_calls = prompt_toks = completion_toks = 0
        try:
            skipped = self.skip(inp)
            if skipped is not None:
                out, reason = skipped
                ms = int((time.monotonic() - started) * 1000)
                await self._finish(ctx, exec_id, ExecStatus.SKIPPED, out, None, 0, 0, 0, ms, symbol, note=reason)
                return AgentResult(out, exec_id, 0, ms, skipped=True, meta={"skip_reason": reason})
            if not cfg.enabled:
                raise AgentError(f"agent {self.name} is disabled")
            provider = ctx.router.get(cfg.provider)
            system = self.system_prompt.strip() + "\n" + COMMON_RULES
            user = self.build_user_prompt(inp)
            feedback, last_error = "", "no attempt made"
            deadline = started + ctx.settings.agent_timeout_seconds
            for attempt in range(ctx.settings.llm_max_retries + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 1:
                    raise AgentError(f"agent timed out after {ctx.settings.agent_timeout_seconds}s ({last_error})", ExecStatus.TIMEOUT)
                await ctx.router.charge()
                llm_calls += 1
                try:
                    resp = await asyncio.wait_for(
                        provider.complete(system=system, user=user + feedback, model=cfg.model, temperature=cfg.temperature,
                                          timeout=min(ctx.settings.llm_timeout_seconds, remaining)),
                        timeout=remaining,
                    )
                except TimeoutError:
                    last_error = "LLM call timed out"
                    continue
                except LLMError as e:
                    last_error = str(e)
                    if not e.retryable:
                        raise AgentError(last_error) from e
                    await asyncio.sleep(min(2.0 * (attempt + 1), max(0.0, deadline - time.monotonic())))
                    continue
                prompt_toks += resp.prompt_tokens
                completion_toks += resp.completion_tokens
                try:
                    out = self.output_model.model_validate(extract_json(resp.content))
                    out = self.post_validate(inp, out)  # type: ignore[arg-type]
                except (ValidationError, ValueError) as e:
                    last_error = f"invalid output: {str(e)[:500]}"
                    feedback = f"\n\nYour previous answer was rejected by validation:\n{str(e)[:800]}\nReturn a corrected JSON object."
                    continue
                ms = int((time.monotonic() - started) * 1000)
                await self._finish(ctx, exec_id, ExecStatus.SUCCESS, out, None, llm_calls, prompt_toks, completion_toks, ms, symbol)
                return AgentResult(out, exec_id, llm_calls, ms)  # type: ignore[arg-type]
            raise AgentError(f"gave up after {llm_calls} LLM calls: {last_error}")
        except (AgentError, LLMNotConfigured, LLMBudgetExceeded) as e:
            status = e.status if isinstance(e, AgentError) else ExecStatus.ERROR
            ms = int((time.monotonic() - started) * 1000)
            await self._finish(ctx, exec_id, status, None, str(e), llm_calls, prompt_toks, completion_toks, ms, symbol)
            if isinstance(e, AgentError):
                raise
            raise AgentError(str(e)) from e
        except asyncio.CancelledError:
            await self._finish(ctx, exec_id, ExecStatus.ERROR, None, "cancelled", llm_calls, prompt_toks, completion_toks, 0, symbol)
            raise
        except Exception as e:  # defensive: an agent bug must never take the workflow down
            ms = int((time.monotonic() - started) * 1000)
            await self._finish(ctx, exec_id, ExecStatus.ERROR, None, f"{type(e).__name__}: {e}", llm_calls, prompt_toks, completion_toks, ms, symbol)
            raise AgentError(f"{type(e).__name__}: {e}") from e

    # ---- persistence / status ---------------------------------------------------------------------
    async def _start(self, ctx: AgentContext, inp: In, cfg: AgentConfig, symbol: str | None) -> int | None:
        if not ctx.persist:
            return None
        async with session_scope() as s:
            row = AgentExecution(run_id=ctx.run_id, agent=self.name, symbol=symbol, status=ExecStatus.RUNNING,
                                 provider=cfg.provider, model=cfg.model, input=json.loads(dumps(inp.model_dump(mode="json"))))
            s.add(row)
            await s.flush()
            return row.id

    async def _finish(self, ctx: AgentContext, exec_id: int | None, status: ExecStatus, out: BaseModel | None, error: str | None,
                      llm_calls: int, pt: int, ct: int, ms: int, symbol: str | None, note: str | None = None) -> None:
        payload = out.model_dump(mode="json") if out is not None else None
        conf = self.confidence_of(out) if out is not None else None  # type: ignore[arg-type]
        if ctx.persist and exec_id is not None:
            async with session_scope() as s:
                row = await s.get(AgentExecution, exec_id)
                if row:
                    row.status, row.output, row.error = status, payload, error or note
                    row.llm_calls, row.prompt_tokens, row.completion_tokens = llm_calls, pt, ct
                    row.duration_ms, row.confidence, row.finished_at = ms, conf, utcnow()
        log.info("agent_finished", agent=self.name, status=status, symbol=symbol, llm_calls=llm_calls, ms=ms, error=error)
        await self._status(ctx, "idle", None, last={"status": status, "symbol": symbol, "confidence": conf, "at": utcnow().isoformat(), "error": error})
        if ctx.bus:
            await ctx.bus.publish("agent_execution", {
                "id": exec_id, "agent": self.name, "status": status, "symbol": symbol, "confidence": conf, "llm_calls": llm_calls,
                "duration_ms": ms, "error": error or note, "run_id": ctx.run_id,
            })

    async def _status(self, ctx: AgentContext, state: str, task: str | None, last: dict[str, Any] | None = None) -> None:
        if not ctx.bus:
            return
        prev = await ctx.bus.get_agent_status(self.name) or {}
        await ctx.bus.set_agent_status(self.name, {"state": state, "task": task, "last": last or prev.get("last")})
        if state == "working":
            await ctx.bus.publish("agent_status", {"agent": self.name, "state": state, "task": task}, feed=False)
