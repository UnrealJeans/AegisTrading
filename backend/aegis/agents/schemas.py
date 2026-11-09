"""Typed contracts between agents. Every agent output is validated against one of these models."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Sentiment = Literal["bullish", "bearish", "neutral"]
Level = Literal["low", "medium", "high"]
Confidence = Field(ge=0.0, le=1.0, description="0..1")


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ---- Scout ---------------------------------------------------------------------------------------
class ScoutInput(AgentModel):
    candidates: list[dict[str, Any]]
    open_position_symbols: list[str] = []
    max_picks: int = 2


class ScoutPick(AgentModel):
    symbol: str
    rank: int = Field(ge=1)
    opportunity_type: Literal["breakout", "volume_spike", "momentum", "reversal", "other"]
    thesis: str = Field(max_length=600)
    liquidity: Literal["good", "adequate", "poor"]
    confidence: float = Confidence


class ScoutRejection(AgentModel):
    symbol: str
    reason: str = Field(max_length=300)


class ScoutOutput(AgentModel):
    market_summary: str = Field(max_length=800)
    picks: list[ScoutPick] = []
    rejected: list[ScoutRejection] = []


# ---- News & Sentiment ----------------------------------------------------------------------------
class NewsInput(AgentModel):
    symbol: str
    asset_news: list[dict[str, Any]] = []
    market_news: list[dict[str, Any]] = []
    fear_greed: dict[str, Any] | None = None
    window_hours: int = 24


class NewsEvent(AgentModel):
    headline: str = Field(max_length=300)
    source_ids: list[int] = Field(min_length=1, description="ids of the provided news items that support this event")
    impact: Sentiment
    importance: Level


class NewsOutput(AgentModel):
    sentiment: Sentiment
    score: float = Field(ge=-1.0, le=1.0, description="-1 very bearish .. +1 very bullish")
    confidence: float = Confidence
    data_quality: Literal["none", "low", "medium", "high"]
    key_events: list[NewsEvent] = []
    conflicts: list[str] = []
    summary: str = Field(max_length=800)


# ---- Technical Analyst ---------------------------------------------------------------------------
class TechnicalInput(AgentModel):
    symbol: str
    last_price: float
    timeframes: dict[str, dict[str, Any]]  # timeframe -> deterministic indicator snapshot


class TechnicalSignal(AgentModel):
    indicator: str = Field(max_length=40)
    timeframe: str = Field(max_length=8)
    reading: str = Field(max_length=200)
    interpretation: Sentiment


class TechnicalOutput(AgentModel):
    bias: Sentiment
    strength: float = Confidence
    confidence: float = Confidence
    timeframe_alignment: Literal["aligned", "mixed", "conflicting"]
    signals: list[TechnicalSignal] = []
    key_support: float | None = None
    key_resistance: float | None = None
    volatility: Literal["low", "normal", "high", "extreme"]
    summary: str = Field(max_length=800)


# ---- Analyst -------------------------------------------------------------------------------------
class AnalystInput(AgentModel):
    symbol: str
    last_price: float
    scout: dict[str, Any]
    news: dict[str, Any] | None
    technical: dict[str, Any]
    reference: dict[str, Any]         # ATR, levels, spread, volume
    constraints: dict[str, Any]
    open_position: dict[str, Any] | None = None


class TradeProposalOut(AgentModel):
    asset: str
    side: Literal["BUY", "SELL"]
    entry_price: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    risk_reward: float = Field(default=0, ge=0, description="recomputed deterministically after validation")
    expected_holding_period: str = Field(max_length=64)
    supporting_evidence: list[str] = Field(min_length=1, max_length=10)
    confidence: float = Confidence

    @field_validator("supporting_evidence")
    @classmethod
    def _trim(cls, v: list[str]) -> list[str]:
        return [e[:400] for e in v]


class AnalystOutput(AgentModel):
    action: Literal["BUY", "SELL", "NO_TRADE"]
    proposal: TradeProposalOut | None = None
    rationale: str = Field(max_length=1000)


# ---- Skeptic -------------------------------------------------------------------------------------
class SkepticInput(AgentModel):
    proposal: dict[str, Any]
    scout: dict[str, Any]
    news: dict[str, Any] | None
    technical: dict[str, Any]
    market: dict[str, Any]


class Objection(AgentModel):
    category: Literal["weak_technicals", "conflicting_sentiment", "poor_liquidity", "manipulation_risk",
                      "excessive_volatility", "unfavorable_risk_reward", "unreliable_information", "other"]
    severity: Literal["low", "medium", "high", "critical"]
    detail: str = Field(max_length=400)


class SkepticOutput(AgentModel):
    verdict: Literal["APPROVE", "REJECT"]
    objections: list[Objection] = []
    risk_score: float = Field(ge=0.0, le=1.0, description="0 = clean, 1 = certain loser")
    confidence: float = Confidence
    summary: str = Field(max_length=800)


# ---- Risk Manager --------------------------------------------------------------------------------
class RiskManagerInput(AgentModel):
    proposal: dict[str, Any]
    deterministic_assessment: dict[str, Any]
    portfolio: dict[str, Any]
    risk_parameters: dict[str, Any]
    skeptic: dict[str, Any]


class RiskManagerOutput(AgentModel):
    decision: Literal["APPROVE", "REDUCE", "REJECT"]
    size_multiplier: float = Field(ge=0.0, le=1.0, description="fraction of the engine-computed size to take; never above 1")
    concerns: list[str] = []
    reasoning: str = Field(max_length=1000)
    confidence: float = Confidence
