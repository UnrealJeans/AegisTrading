"""Paper trading: REAL market prices, simulated fills with configurable slippage and fees. No order leaves the box."""
from __future__ import annotations

from decimal import Decimal

from aegis.config import RiskParams
from aegis.db.models import OrderStatus, Side
from aegis.marketdata.service import MarketDataReader, StaleDataError
from aegis.trading.adapters.base import ExchangeAdapter, ExchangeRejected, OrderRequest, OrderResult


class PaperAdapter(ExchangeAdapter):
    name = "paper"

    def __init__(self, reader: MarketDataReader, params: RiskParams) -> None:
        self.reader, self.params = reader, params

    async def create_market_order(self, req: OrderRequest) -> OrderResult:
        try:
            last = await self.reader.get_price(req.symbol)
        except StaleDataError as e:
            raise ExchangeRejected(f"paper fill refused: {e}") from e  # never fill against an old price
        slip = self.params.slippage_bps / Decimal(10000)
        price = last * (1 + slip) if req.side == Side.BUY else last * (1 - slip)
        fee = req.quantity * price * self.params.fee_rate
        return OrderResult(exchange_order_id=f"paper-{req.client_order_id}", status=OrderStatus.FILLED, filled_quantity=req.quantity,
                           net_quantity=req.quantity, avg_price=price, fee_quote=fee)

    async def fetch_order(self, symbol: str, client_order_id: str) -> OrderResult | None:
        # A paper fill only exists once its database transaction commits, so an unresolved order was never filled.
        return None

    async def fetch_balances(self) -> dict[str, Decimal] | None:
        return None
