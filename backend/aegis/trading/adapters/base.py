"""Exchange adapter contract. New venues (other CEXs, a Solana DEX router) implement this interface only."""
from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from pydantic import BaseModel

from aegis.db.models import OrderStatus, Side


class ExchangeError(Exception):
    """Base class."""


class ExchangeTimeout(ExchangeError):
    """Outcome UNKNOWN: the order may or may not have reached the venue. Must be resolved by lookup, never resent."""


class ExchangeRejected(ExchangeError):
    """The venue definitively refused the order (insufficient balance, filter violation, ...)."""


class OrderRequest(BaseModel):
    client_order_id: str
    symbol: str
    side: Side
    quantity: Decimal
    reference_price: Decimal


class OrderResult(BaseModel):
    exchange_order_id: str
    status: OrderStatus
    filled_quantity: Decimal          # gross base quantity filled
    net_quantity: Decimal             # base quantity actually held after base-denominated fees
    avg_price: Decimal | None
    fee_quote: Decimal                # total fee expressed in quote currency


class ExchangeAdapter(ABC):
    name: str

    @abstractmethod
    async def create_market_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def fetch_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        """Look up an order by our idempotency key. None means the venue has never seen it."""

    @abstractmethod
    async def fetch_balances(self) -> dict[str, Decimal] | None:
        """Total balances per asset, or None when the venue has no account to reconcile (paper)."""

    async def close(self) -> None:
        return None
