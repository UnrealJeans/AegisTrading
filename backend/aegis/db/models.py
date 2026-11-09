"""Persistence model. Every decision and transaction is traceable:

workflow_runs -> agent_executions -> agent_messages
             -> trade_proposals -> approved_trades | rejected_trades -> orders -> positions
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aegis.core.timeutil import utcnow
from aegis.db.base import PK, Base, IdMixin, JSONType, Money, TimestampMixin, UTCDateTime


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"  # process died mid-run; marked during startup recovery


class ExecStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"  # no LLM call was made, reason recorded


class ProposalStatus(StrEnum):
    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class ApprovedStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    REJECTED_AT_EXECUTION = "REJECTED_AT_EXECUTION"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class OrderStatus(StrEnum):
    SUBMITTED = "SUBMITTED"  # intent persisted BEFORE sending; outcome may be unknown after a crash/timeout
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


UNRESOLVED_ORDER_STATES = (OrderStatus.SUBMITTED,)


class PositionStatus(StrEnum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class User(Base, IdMixin, TimestampMixin):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AuditLog(Base, IdMixin, TimestampMixin):
    __tablename__ = "audit_logs"
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SystemState(Base):
    """Small key/value store for durable control state (trading status, runtime risk params, agent config)."""
    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Asset(Base, IdMixin, TimestampMixin):
    __tablename__ = "assets"
    symbol: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. BTC/USDT
    base: Mapped[str] = mapped_column(String(16))
    quote: Mapped[str] = mapped_column(String(16))
    exchange: Mapped[str] = mapped_column(String(32), default="binance")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    min_qty: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    qty_step: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    price_tick: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    min_notional: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Candle(Base, IdMixin):
    __tablename__ = "candles"
    __table_args__ = (UniqueConstraint("symbol", "timeframe", "open_time", name="uq_candle"),)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    timeframe: Mapped[str] = mapped_column(String(8))
    open_time: Mapped[datetime] = mapped_column(UTCDateTime)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[Decimal] = mapped_column(Money)


class NewsItem(Base, IdMixin, TimestampMixin):
    __tablename__ = "news_items"
    uid: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 of url
    source: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    symbols: Mapped[list[str]] = mapped_column(JSONType, default=list)


class WorkflowRun(Base, IdMixin, TimestampMixin):
    __tablename__ = "workflow_runs"
    run_uid: Mapped[str] = mapped_column(String(40), unique=True)
    status: Mapped[RunStatus] = mapped_column(String(16), default=RunStatus.RUNNING)
    trigger: Mapped[str] = mapped_column(String(32), default="schedule")
    summary: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    trace: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, default=list)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    executions: Mapped[list[AgentExecution]] = relationship(back_populates="run")


class AgentExecution(Base, IdMixin, TimestampMixin):
    __tablename__ = "agent_executions"
    __table_args__ = (Index("ix_agent_exec_agent_created", "agent", "created_at"),)
    run_id: Mapped[int | None] = mapped_column(PK, ForeignKey("workflow_runs.id"), nullable=True, index=True)
    agent: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[ExecStatus] = mapped_column(String(16), default=ExecStatus.RUNNING)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    run: Mapped[WorkflowRun | None] = relationship(back_populates="executions")


class AgentMessage(Base, IdMixin, TimestampMixin):
    """Structured inter-agent communication (typed payloads, not free chat)."""
    __tablename__ = "agent_messages"
    run_id: Mapped[int | None] = mapped_column(PK, ForeignKey("workflow_runs.id"), nullable=True, index=True)
    sender: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(48))
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class TradeProposal(Base, IdMixin, TimestampMixin):
    __tablename__ = "trade_proposals"
    run_id: Mapped[int | None] = mapped_column(PK, ForeignKey("workflow_runs.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[Side] = mapped_column(String(4))
    entry_price: Mapped[Decimal] = mapped_column(Money)
    stop_loss: Mapped[Decimal] = mapped_column(Money)
    take_profit: Mapped[Decimal] = mapped_column(Money)
    risk_reward: Mapped[Decimal] = mapped_column(Money)
    holding_period: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ProposalStatus] = mapped_column(String(16), default=ProposalStatus.PROPOSED, index=True)
    # full decision record: scout / news / technical / analyst / skeptic / risk outputs
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    approved: Mapped[ApprovedTrade | None] = relationship(back_populates="proposal", uselist=False)
    rejected: Mapped[RejectedTrade | None] = relationship(back_populates="proposal", uselist=False)


class ApprovedTrade(Base, IdMixin, TimestampMixin):
    """Durable hand-off queue from orchestration to the trading engine."""
    __tablename__ = "approved_trades"
    proposal_id: Mapped[int] = mapped_column(PK, ForeignKey("trade_proposals.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[Side] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(Money)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    stop_loss: Mapped[Decimal] = mapped_column(Money)
    take_profit: Mapped[Decimal] = mapped_column(Money)
    risk_amount: Mapped[Decimal] = mapped_column(Money)
    status: Mapped[ApprovedStatus] = mapped_column(String(24), default=ApprovedStatus.PENDING, index=True)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    executed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    proposal: Mapped[TradeProposal] = relationship(back_populates="approved")


class RejectedTrade(Base, IdMixin, TimestampMixin):
    __tablename__ = "rejected_trades"
    proposal_id: Mapped[int] = mapped_column(PK, ForeignKey("trade_proposals.id"), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32))  # skeptic | risk_engine | risk_manager | execution
    reasons: Mapped[list[str]] = mapped_column(JSONType, default=list)
    proposal: Mapped[TradeProposal] = relationship(back_populates="rejected")


class Position(Base, IdMixin, TimestampMixin):
    __tablename__ = "positions"
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[PositionStatus] = mapped_column(String(16), default=PositionStatus.OPEN, index=True)
    approved_trade_id: Mapped[int | None] = mapped_column(PK, ForeignKey("approved_trades.id"), nullable=True, unique=True)
    quantity: Mapped[Decimal] = mapped_column(Money)
    entry_price: Mapped[Decimal] = mapped_column(Money)
    stop_loss: Mapped[Decimal] = mapped_column(Money)
    take_profit: Mapped[Decimal] = mapped_column(Money)
    entry_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    exit_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    exit_fee: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    orders: Mapped[list[Order]] = relationship(back_populates="position")


class Order(Base, IdMixin, TimestampMixin):
    __tablename__ = "orders"
    # deterministic idempotency key; UNIQUE makes duplicate submission impossible at the DB level
    client_order_id: Mapped[str] = mapped_column(String(48), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(16))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[Side] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(16), default="MARKET")
    purpose: Mapped[str] = mapped_column(String(16))  # ENTRY | EXIT
    quantity: Mapped[Decimal] = mapped_column(Money)
    reference_price: Mapped[Decimal] = mapped_column(Money)
    filled_quantity: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Money, nullable=True)
    fee: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))  # in quote currency
    status: Mapped[OrderStatus] = mapped_column(String(20), default=OrderStatus.SUBMITTED, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approved_trade_id: Mapped[int | None] = mapped_column(PK, ForeignKey("approved_trades.id"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(PK, ForeignKey("positions.id"), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    position: Mapped[Position | None] = relationship(back_populates="orders")


class PortfolioSnapshot(Base, IdMixin, TimestampMixin):
    __tablename__ = "portfolio_snapshots"
    mode: Mapped[str] = mapped_column(String(16))
    equity: Mapped[Decimal] = mapped_column(Money)
    cash: Mapped[Decimal] = mapped_column(Money)
    positions_value: Mapped[Decimal] = mapped_column(Money)
    realized_pnl: Mapped[Decimal] = mapped_column(Money)
    unrealized_pnl: Mapped[Decimal] = mapped_column(Money)
    open_positions: Mapped[int] = mapped_column(Integer, default=0)
    drawdown_pct: Mapped[Decimal] = mapped_column(Money, default=Decimal(0))


class RiskEvent(Base, IdMixin, TimestampMixin):
    __tablename__ = "risk_events"
    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[Severity] = mapped_column(String(16), default=Severity.WARNING)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SystemEvent(Base, IdMixin, TimestampMixin):
    __tablename__ = "system_events"
    service: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[Severity] = mapped_column(String(16), default=Severity.INFO)
    message: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
