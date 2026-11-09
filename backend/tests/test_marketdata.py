from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from aegis.db.models import Asset, Candle, NewsItem
from aegis.db.session import session_scope
from aegis.marketdata.exchange import MarketDataError
from aegis.marketdata.news import NewsService, news_context, parse_feed, tag_symbols
from aegis.marketdata.service import MarketDataService, StaleDataError
from tests.conftest import make_settings, set_ticker

NOW_MS = int(time.time() * 1000)
H = 3600_000


class FakeMDClient:
    """Stands in for the CCXT boundary only."""
    source = "binance"

    def __init__(self):
        self.id_to_symbol = {}
        self.fail = False
        self.ohlcv_calls = 0

    async def load_markets(self):
        self.id_to_symbol = {"BTCUSDT": "BTC/USDT", "ETHUSDT": "ETH/USDT"}
        return [{"symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "active": True, "min_qty": 0.00001, "qty_step": 0.00001, "price_tick": 0.01, "min_notional": 5.0},
                {"symbol": "ETH/USDT", "base": "ETH", "quote": "USDT", "active": True, "min_qty": 0.0001, "qty_step": 0.0001, "price_tick": 0.01, "min_notional": 5.0}]

    async def fetch_tickers(self):
        if self.fail:
            raise MarketDataError("timeout calling fetch_tickers")
        ts = time.time()
        return {"BTC/USDT": {"symbol": "BTC/USDT", "last": 60000.0, "bid": 59999.0, "ask": 60001.0, "quote_volume": 2e9, "change_pct": 1.0, "ts": ts},
                "ETH/USDT": {"symbol": "ETH/USDT", "last": 3000.0, "bid": 2999.9, "ask": 3000.1, "quote_volume": 9e8, "change_pct": -2.0, "ts": ts},
                "DUST/USDT": {"symbol": "DUST/USDT", "last": 0.01, "bid": None, "ask": None, "quote_volume": 100.0, "change_pct": 0.0, "ts": ts}}

    async def fetch_ohlcv(self, symbol, timeframe, limit=300, since=None):
        self.ohlcv_calls += 1
        base = (NOW_MS // H) * H
        return [[base - i * H, 100 + i, 101 + i, 99 + i, 100.5 + i, 10.0] for i in range(min(limit, 5), -1, -1)]  # last row = forming bar

    def timeframe_seconds(self, tf):
        return {"15m": 900, "1h": 3600, "4h": 14400}[tf]

    async def close(self):
        pass


async def test_markets_tickers_universe_and_cache(bus, db):
    settings = make_settings(candle_timeframes="1h")
    md = MarketDataService(settings, bus, FakeMDClient())
    assert await md.refresh_markets() == 2 and await md.refresh_markets() == 2  # idempotent upsert
    async with session_scope() as s:
        assets = (await s.execute(select(Asset).order_by(Asset.symbol))).scalars().all()
    assert [a.symbol for a in assets] == ["BTC/USDT", "ETH/USDT"] and str(assets[0].qty_step) == "0.00001"
    await md.refresh_tickers()
    assert md.universe == ["BTC/USDT", "ETH/USDT"] and await md.get_universe() == md.universe  # dust filtered by volume
    assert str(await md.get_price("BTC/USDT")) == "60000.0"
    st = await md.report_status()
    assert st["healthy"] and (await md.status())["fresh_tickers"] == 2


async def test_websocket_batches_update_prices_and_keep_bid_ask(bus, db):
    md = MarketDataService(make_settings(), bus, FakeMDClient())
    await md.refresh_markets()
    await md.refresh_tickers()
    changed = md.apply_ws_batch([{"e": "24hrMiniTicker", "E": NOW_MS, "s": "BTCUSDT", "c": "61000.5", "o": "60000", "h": "1", "l": "1", "v": "1", "q": "2100000000"},
                                 {"s": "UNKNOWNPAIR", "c": "1", "o": "1"}, {"s": "ETHUSDT", "c": "bad", "o": "1"}])
    assert list(changed) == ["BTC/USDT"]
    await bus.set_tickers(changed)
    t = await md.get_ticker("BTC/USDT")
    assert t["last"] == 61000.5 and t["bid"] == 59999.0 and t["change_pct"] == pytest.approx(1.6675)


async def test_stale_and_missing_data_is_detected_not_served(bus, db):
    md = MarketDataService(make_settings(), bus, FakeMDClient())
    await md.refresh_markets()
    with pytest.raises(StaleDataError, match="no ticker"):
        await md.get_price("BTC/USDT")
    assert (await md.report_status())["healthy"] is False
    await md.refresh_tickers()
    await set_ticker(bus, "BTC/USDT", 60000, age=120)
    with pytest.raises(StaleDataError, match="old"):
        await md.get_price("BTC/USDT")
    # exchange outage: REST fails, in-memory tickers age out -> unhealthy
    md.client.fail = True
    with pytest.raises(MarketDataError):
        await md.refresh_tickers()
    for t in md.tickers.values():
        t["ts"] = time.time() - 300
    st = await md.report_status()
    assert st["healthy"] is False and "fresh" in st["reason"]


async def test_candle_ingestion_stores_closed_bars_once(bus, db):
    settings = make_settings(candle_timeframes="1h")
    client = FakeMDClient()
    md = MarketDataService(settings, bus, client)
    await md.refresh_markets()
    await md.refresh_tickers()
    assert await md.ingest_candles("BTC/USDT", "1h") == 5  # forming bar excluded
    assert await md.ingest_candles("BTC/USDT", "1h") == 5
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Candle))).scalar_one() == 5
    df = await md.get_candles("BTC/USDT", "1h")
    assert list(df["close"]) == sorted(df["close"], reverse=True) and df["open_time"].is_monotonic_increasing
    assert df["open_time"].iloc[-1] <= datetime.now(UTC) - timedelta(hours=1)
    client.ohlcv_calls = 0
    await md.ingest_universe_candles()
    first = client.ohlcv_calls
    await md.ingest_universe_candles()
    assert first == 2 and client.ohlcv_calls == 2, "no refetch until a new bar has closed"


RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Test Wire</title>
<item><title>Bitcoin ETF sees record inflows</title><link>https://news.test/a</link><description>&lt;p&gt;BTC demand rises&lt;/p&gt;</description><pubDate>{recent}</pubDate></item>
<item><title>Solana outage resolved</title><link>https://news.test/b</link><description>SOL validators restart</description><pubDate>{recent}</pubDate></item>
<item><title>Undated rumour about ETH</title><link>https://news.test/c</link></item>
<item><title>Ancient story</title><link>https://news.test/d</link><pubDate>Mon, 01 Jan 2018 00:00:00 GMT</pubDate></item>
</channel></rss>"""


async def test_news_ingestion_is_real_source_only(bus, db):
    from email.utils import format_datetime
    body = RSS.format(recent=format_datetime(datetime.now(UTC) - timedelta(hours=2)))
    assert tag_symbols("Ethereum and $SOL rally while OP lags", {"ETH", "SOL", "OP", "BTC"}) == ["ETH", "SOL"]
    items = parse_feed(body, "https://news.test/rss", {"BTC", "SOL", "ETH"})
    assert [i["title"] for i in items] == ["Bitcoin ETF sees record inflows", "Solana outage resolved", "Ancient story"]  # undated dropped
    assert items[0]["symbols"] == ["BTC"] and items[0]["summary"] == "BTC demand rises" and items[0]["source"] == "Test Wire"

    def handler(req: httpx.Request) -> httpx.Response:
        if "fng" in str(req.url):
            return httpx.Response(200, json={"data": [{"value": "72", "value_classification": "Greed", "timestamp": str(int(time.time()))}]})
        if "down.test" in str(req.url):
            return httpx.Response(503)
        return httpx.Response(200, content=body.encode())

    settings = make_settings(news_rss_feeds="https://news.test/rss,https://down.test/rss")
    svc = NewsService(settings, bus, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await svc.refresh({"BTC", "SOL", "ETH"}) == 2 and await svc.refresh({"BTC", "SOL", "ETH"}) == 0  # dedup + age cut-off
    ctx = await news_context(settings, bus, "BTC/USDT")
    assert [n["title"] for n in ctx["asset_news"]] == ["Bitcoin ETF sees record inflows"] and len(ctx["market_news"]) == 1
    assert ctx["fear_greed"]["value"] == 72 and ctx["fear_greed"]["classification"] == "Greed"
    # every source down -> empty context, nothing invented
    async with session_scope() as s:
        for n in (await s.execute(select(NewsItem))).scalars():
            await s.delete(n)
    await bus.r.flushall()
    dead = NewsService(settings, bus, httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
    assert await dead.refresh({"BTC"}) == 0
    assert await news_context(settings, bus, "BTC/USDT") == {"asset_news": [], "market_news": [], "fear_greed": None, "window_hours": 24}
