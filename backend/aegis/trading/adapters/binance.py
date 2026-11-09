"""Binance Spot adapter (CCXT). Used for Spot Testnet; the same code path serves live once that milestone is enabled."""
from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt

from aegis.config import Settings, TradingMode
from aegis.db.models import OrderStatus
from aegis.marketdata.exchange import build_ccxt_binance
from aegis.trading.adapters.base import (
    ExchangeAdapter,
    ExchangeError,
    ExchangeRejected,
    ExchangeTimeout,
    OrderRequest,
    OrderResult,
)

_STATUS = {"closed": OrderStatus.FILLED, "open": OrderStatus.PARTIALLY_FILLED, "canceled": OrderStatus.CANCELED,
           "expired": OrderStatus.CANCELED, "rejected": OrderStatus.REJECTED}


class BinanceSpotAdapter(ExchangeAdapter):
    def __init__(self, settings: Settings, exchange: Any | None = None) -> None:
        self.settings = settings
        testnet = settings.trading_mode != TradingMode.LIVE
        self.name = "binance_testnet" if testnet else "binance"
        if exchange is not None:
            self.ex = exchange
        elif testnet:
            self.ex = build_ccxt_binance(settings, source="binance_testnet", api_key=settings.binance_testnet_api_key.get_secret_value(),
                                         secret=settings.binance_testnet_api_secret.get_secret_value())
        else:
            self.ex = build_ccxt_binance(settings, source="binance", api_key=settings.binance_live_api_key.get_secret_value(),
                                         secret=settings.binance_live_api_secret.get_secret_value())

    async def close(self) -> None:
        await self.ex.close()

    def _parse(self, o: dict[str, Any], symbol: str) -> OrderResult:
        base, quote = symbol.split("/")
        filled = Decimal(str(o.get("filled") or 0))
        avg = o.get("average") or (o.get("cost") / o["filled"] if o.get("cost") and o.get("filled") else None)
        avg_d = Decimal(str(avg)) if avg else None
        fee_quote, fee_base = Decimal(0), Decimal(0)
        fees = o.get("fees") or ([o["fee"]] if o.get("fee") else [])
        known = True
        for f in fees:
            cost = Decimal(str(f.get("cost") or 0))
            if f.get("currency") == quote:
                fee_quote += cost
            elif f.get("currency") == base and avg_d:
                fee_base += cost
                fee_quote += cost * avg_d
            elif cost > 0:
                known = False  # e.g. paid in BNB: valued with the configured fee rate below
        if (not fees or not known) and avg_d:
            fee_quote = max(fee_quote, filled * avg_d * self.settings.fee_rate)
        status = _STATUS.get(o.get("status") or "", OrderStatus.SUBMITTED)
        if status == OrderStatus.CANCELED and filled > 0:
            status = OrderStatus.FILLED  # market order expired after a partial fill: what filled is what we hold
        return OrderResult(exchange_order_id=str(o.get("id")), status=status, filled_quantity=filled,
                           net_quantity=filled - fee_base if str(o.get("side")).lower() == "buy" else filled, avg_price=avg_d, fee_quote=fee_quote)

    async def create_market_order(self, req: OrderRequest) -> OrderResult:
        try:
            amount = float(self.ex.amount_to_precision(req.symbol, float(req.quantity))) if getattr(self.ex, "markets", None) else float(req.quantity)
            o = await asyncio.wait_for(
                self.ex.create_order(req.symbol, "market", req.side.value.lower(), amount, None, {"newClientOrderId": req.client_order_id}),
                timeout=self.settings.exchange_timeout_seconds + 5,
            )
        except (TimeoutError, ccxt.RequestTimeout, ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            raise ExchangeTimeout(f"{type(e).__name__}: {e}") from e
        except (ccxt.InsufficientFunds, ccxt.InvalidOrder, ccxt.BadRequest, ccxt.BadSymbol, ccxt.PermissionDenied, ccxt.AuthenticationError) as e:
            raise ExchangeRejected(f"{type(e).__name__}: {e}") from e
        except ccxt.BaseError as e:
            raise ExchangeTimeout(f"unclassified exchange error, outcome unknown: {type(e).__name__}: {e}") from e
        return self._parse(o, req.symbol)

    async def fetch_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        try:
            o = await asyncio.wait_for(self.ex.fetch_order("", symbol, {"origClientOrderId": client_order_id}),
                                       timeout=self.settings.exchange_timeout_seconds + 5)
        except ccxt.OrderNotFound:
            return None
        except (TimeoutError, ccxt.BaseError) as e:
            raise ExchangeTimeout(f"{type(e).__name__}: {e}") from e
        return self._parse(o, symbol)

    async def fetch_balances(self) -> dict[str, Decimal] | None:
        try:
            b = await asyncio.wait_for(self.ex.fetch_balance(), timeout=self.settings.exchange_timeout_seconds + 5)
        except (TimeoutError, ccxt.BaseError) as e:
            raise ExchangeError(f"{type(e).__name__}: {e}") from e
        return {a: Decimal(str(v)) for a, v in (b.get("total") or {}).items() if v}

    async def load_markets(self) -> None:
        await self.ex.load_markets()
