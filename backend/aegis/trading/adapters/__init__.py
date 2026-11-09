from __future__ import annotations

from aegis.config import RiskParams, Settings, TradingMode
from aegis.marketdata.service import MarketDataReader
from aegis.trading.adapters.base import ExchangeAdapter


class ModeNotAllowed(RuntimeError):
    pass


def build_adapter(settings: Settings, reader: MarketDataReader, params: RiskParams) -> ExchangeAdapter:
    """Single place where a trading mode becomes a venue. Refuses anything that is not explicitly configured."""
    problems = settings.mode_problems()
    if problems:
        raise ModeNotAllowed("; ".join(problems))
    if settings.trading_mode == TradingMode.PAPER:
        from aegis.trading.adapters.paper import PaperAdapter
        return PaperAdapter(reader, params)
    from aegis.trading.adapters.binance import BinanceSpotAdapter
    return BinanceSpotAdapter(settings)
