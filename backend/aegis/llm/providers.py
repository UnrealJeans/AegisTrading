"""Provider-independent LLM access.

OpenAI and xAI (Grok) both speak the OpenAI Chat Completions protocol, as do most other vendors and self-hosted
gateways, so one HTTP implementation covers all of them. Agents depend only on `LLMProvider`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

import httpx
from pydantic import BaseModel

from aegis.config import Settings
from aegis.core.bus import Bus


class LLMError(Exception):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMNotConfigured(LLMError):
    pass


class LLMBudgetExceeded(LLMError):
    pass


class LLMResponse(BaseModel):
    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMProvider(Protocol):
    name: str

    async def complete(self, *, system: str, user: str, model: str, temperature: float, timeout: float, json_mode: bool = True) -> LLMResponse: ...


class OpenAICompatibleProvider:
    def __init__(self, name: str, base_url: str, api_key: str, http: httpx.AsyncClient | None = None) -> None:
        self.name, self.base_url, self._api_key = name, base_url.rstrip("/"), api_key
        self.http = http or httpx.AsyncClient()

    async def complete(self, *, system: str, user: str, model: str, temperature: float, timeout: float, json_mode: bool = True) -> LLMResponse:
        body: dict = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = await self.http.post(
                f"{self.base_url}/chat/completions", json=body, timeout=timeout,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            )
        except httpx.TimeoutException as e:
            raise LLMError(f"{self.name}: request timed out", retryable=True) from e
        except httpx.HTTPError as e:
            raise LLMError(f"{self.name}: transport error {type(e).__name__}", retryable=True) from e
        if r.status_code >= 400:
            # body may echo the prompt but never the key; keep it short
            raise LLMError(f"{self.name}: HTTP {r.status_code}: {r.text[:300]}", retryable=r.status_code in (408, 409, 429) or r.status_code >= 500)
        try:
            data = r.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.name}: malformed response", retryable=True) from e
        if not content:
            raise LLMError(f"{self.name}: empty completion", retryable=True)
        usage = data.get("usage") or {}
        return LLMResponse(content=content, model=data.get("model", model),
                           prompt_tokens=int(usage.get("prompt_tokens") or 0), completion_tokens=int(usage.get("completion_tokens") or 0))


class LLMRouter:
    """Resolves provider names to configured providers and enforces the daily call budget."""

    def __init__(self, settings: Settings, bus: Bus | None = None, providers: dict[str, LLMProvider] | None = None) -> None:
        self.settings, self.bus = settings, bus
        self._http = httpx.AsyncClient() if providers is None else None
        self.providers: dict[str, LLMProvider] = providers if providers is not None else self._from_settings(settings)

    def _from_settings(self, s: Settings) -> dict[str, LLMProvider]:
        out: dict[str, LLMProvider] = {}
        for name, url, key in (
            ("openai", s.openai_base_url, s.openai_api_key.get_secret_value()),
            ("xai", s.xai_base_url, s.xai_api_key.get_secret_value()),
            ("custom", s.custom_llm_base_url, s.custom_llm_api_key.get_secret_value()),
        ):
            if key and url:
                out[name] = OpenAICompatibleProvider(name, url, key, self._http)
        return out

    def available(self) -> list[str]:
        return sorted(self.providers)

    def get(self, provider: str) -> LLMProvider:
        if provider not in self.providers:
            raise LLMNotConfigured(f"LLM provider '{provider}' has no API key configured")
        return self.providers[provider]

    async def charge(self) -> None:
        """Count one call against the UTC-day budget; raises once the budget is spent."""
        if not self.bus or self.settings.llm_daily_call_budget <= 0:
            return
        key = f"aegis:llm:calls:{datetime.now(UTC):%Y%m%d}"
        used = await self.bus.incr_window(key, 2 * 86400)
        if used > self.settings.llm_daily_call_budget:
            raise LLMBudgetExceeded(f"daily LLM call budget of {self.settings.llm_daily_call_budget} reached")

    async def calls_today(self) -> int:
        if not self.bus:
            return 0
        return int(await self.bus.r.get(f"aegis:llm:calls:{datetime.now(UTC):%Y%m%d}") or 0)

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
