"""Real news + sentiment inputs. Nothing here is generated: if sources are unreachable the result is empty and
downstream agents are told that no data is available."""
from __future__ import annotations

import calendar
import hashlib
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import feedparser
import httpx
from sqlalchemy import select

from aegis.config import Settings
from aegis.core.bus import Bus
from aegis.core.logging import get_logger
from aegis.core.timeutil import utcnow
from aegis.db.models import NewsItem
from aegis.db.session import session_scope

log = get_logger(__name__)

ASSET_NAMES: dict[str, list[str]] = {
    "BTC": ["bitcoin"], "ETH": ["ethereum", "ether"], "SOL": ["solana"], "XRP": ["ripple"], "BNB": ["binance coin"],
    "DOGE": ["dogecoin"], "ADA": ["cardano"], "AVAX": ["avalanche"], "LINK": ["chainlink"], "DOT": ["polkadot"],
    "LTC": ["litecoin"], "TRX": ["tron"], "SHIB": ["shiba inu"], "UNI": ["uniswap"], "ATOM": ["cosmos"],
    "NEAR": ["near protocol"], "APT": ["aptos"], "ARB": ["arbitrum"], "OP": ["optimism"], "SUI": ["sui"],
    "TON": ["toncoin"], "PEPE": ["pepe"], "AAVE": ["aave"], "FIL": ["filecoin"], "XLM": ["stellar"],
}
_TAG = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return re.sub(r"\s+", " ", _TAG.sub(" ", text or "")).strip()


def tag_symbols(text: str, bases: set[str]) -> list[str]:
    """Match base tickers (upper-case whole words, >=3 chars) and well-known project names."""
    found = {b for b in bases if len(b) >= 3 and re.search(rf"(?<![A-Za-z0-9])\$?{re.escape(b)}(?![A-Za-z0-9])", text)}
    lower = text.lower()
    for base, names in ASSET_NAMES.items():
        if base in bases and any(re.search(rf"\b{re.escape(n)}\b", lower) for n in names):
            found.add(base)
    return sorted(found)


def parse_feed(content: bytes | str, source_url: str, bases: set[str]) -> list[dict[str, Any]]:
    feed = feedparser.parse(content)
    source = feed.feed.get("title") or httpx.URL(source_url).host
    items = []
    for e in feed.entries:
        url, title = e.get("link"), strip_html(e.get("title", ""))
        parsed = e.get("published_parsed") or e.get("updated_parsed")
        if not url or not title or not parsed:
            continue  # undated items cannot be judged for freshness -> dropped
        summary = strip_html(e.get("summary", ""))[:600]
        items.append({
            "uid": hashlib.sha256(url.encode()).hexdigest(), "source": source[:128], "title": title, "url": url,
            "summary": summary, "published_at": datetime.fromtimestamp(calendar.timegm(parsed), UTC),
            "symbols": tag_symbols(f"{title} {summary}", bases),
        })
    return items


class NewsService:
    def __init__(self, settings: Settings, bus: Bus, http: httpx.AsyncClient | None = None) -> None:
        self.settings, self.bus = settings, bus
        self.http = http or httpx.AsyncClient(timeout=15, follow_redirects=True, headers={"User-Agent": "aegis-news/0.1"})

    async def close(self) -> None:
        await self.http.aclose()

    async def refresh(self, bases: set[str]) -> int:
        """Pull every configured source; returns number of new items stored."""
        collected: list[dict[str, Any]] = []
        for url in self.settings.rss_feeds:
            try:
                r = await self.http.get(url)
                r.raise_for_status()
                collected += parse_feed(r.content, url, bases)
            except Exception as e:
                log.warning("news_feed_failed", url=url, error=f"{type(e).__name__}: {e}")
        key = self.settings.cryptopanic_api_key.get_secret_value()
        if key:
            try:
                r = await self.http.get("https://cryptopanic.com/api/v1/posts/", params={"auth_token": key, "public": "true", "kind": "news"})
                r.raise_for_status()
                for p in r.json().get("results", []):
                    link = p.get("url")
                    if not link or not p.get("published_at"):
                        continue
                    collected.append({
                        "uid": hashlib.sha256(link.encode()).hexdigest(), "title": p.get("title", ""), "url": link, "summary": "",
                        "source": f"CryptoPanic/{(p.get('source') or {}).get('title', '')}"[:128],
                        "published_at": datetime.fromisoformat(p["published_at"].replace("Z", "+00:00")),
                        "symbols": sorted({c["code"] for c in p.get("currencies") or [] if c.get("code") in bases}),
                    })
            except Exception as e:
                log.warning("cryptopanic_failed", error=f"{type(e).__name__}: {e}")
        cutoff = utcnow() - timedelta(hours=self.settings.news_max_age_hours * 3)
        collected = [c for c in collected if c["published_at"] >= cutoff]
        new = 0
        async with session_scope() as s:
            uids = [c["uid"] for c in collected]
            known = set((await s.execute(select(NewsItem.uid).where(NewsItem.uid.in_(uids)))).scalars()) if uids else set()
            for c in collected:
                if c["uid"] not in known:
                    known.add(c["uid"])
                    s.add(NewsItem(**c))
                    new += 1
        await self.refresh_fear_greed()
        return new

    async def refresh_fear_greed(self) -> None:
        try:
            r = await self.http.get(self.settings.fear_greed_url)
            r.raise_for_status()
            d = r.json()["data"][0]
            await self.bus.cache_set("fear_greed", {
                "value": int(d["value"]), "classification": d["value_classification"],
                "as_of": datetime.fromtimestamp(int(d["timestamp"]), UTC).isoformat(), "source": "alternative.me Fear & Greed Index",
            }, ttl=6 * 3600)
        except Exception as e:
            log.warning("fear_greed_failed", error=f"{type(e).__name__}: {e}")


async def news_context(settings: Settings, bus: Bus, symbol: str, *, limit: int = 12) -> dict[str, Any]:
    """Evidence bundle for the News agent: asset-specific headlines, market-wide headlines, sentiment index."""
    base = symbol.split("/")[0]
    cutoff = utcnow() - timedelta(hours=settings.news_max_age_hours)
    async with session_scope() as s:
        rows = (await s.execute(
            select(NewsItem).where(NewsItem.published_at >= cutoff).order_by(NewsItem.published_at.desc()).limit(200)
        )).scalars().all()

    def fmt(n: NewsItem) -> dict[str, Any]:
        return {"id": n.id, "source": n.source, "title": n.title, "summary": n.summary[:300],
                "published_at": n.published_at.isoformat(), "url": n.url}

    specific = [fmt(n) for n in rows if base in (n.symbols or [])][:limit]
    market = [fmt(n) for n in rows if base not in (n.symbols or [])][: max(4, limit // 2)]
    return {"asset_news": specific, "market_news": market, "fear_greed": await bus.cache_get("fear_greed"),
            "window_hours": settings.news_max_age_hours}
