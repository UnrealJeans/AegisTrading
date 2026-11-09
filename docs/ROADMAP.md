# Roadmap and verification status

"Verified" below means covered by the automated test suite or run by hand during development.
It is stated separately from "implemented" on purpose.

| Phase | Scope | Implemented | Verified how |
|---|---|---|---|
| 1 Foundation | config, models, Alembic, Redis, auth, Docker | yes | unit/API tests; migration up/down/`check` on PostgreSQL 16; services started against real PostgreSQL + Redis. **Docker images were not built or run in the authoring environment (no Docker daemon)**; `docker compose config` validates. CI builds them. |
| 2 Market data | CCXT REST, WebSocket stream, candles, scanner, indicators, news | yes | unit tests with the CCXT/HTTP boundary faked, **plus a live run of the real worker against Binance public data** (`data-api.binance.vision` REST + `data-stream.binance.vision` WebSocket): 775 markets loaded, 40-symbol universe, ~35,700 closed candles stored, sub-second ticks, health "fresh"; scanner, indicators, risk sizing and a paper entry/exit were then run on those live prices. `api.binance.com` returned HTTP 451 (geo-block) from the authoring environment, so the default endpoint itself was not exercised. News RSS feeds and the Fear & Greed API were not reachable there and are covered by tests only. |
| 3 Agents | six agents, provider abstraction, LangGraph, tracing | yes | agent and workflow tests with the LLM HTTP boundary scripted; OpenAI wire format tested with a mock transport. **No call to a real OpenAI/xAI endpoint has been made yet** (no keys available). |
| 4 Trading engine | paper trading, risk, portfolio, SL/TP, recovery | yes | engine tests incl. failure scenarios, on SQLite and PostgreSQL |
| 5 Dashboard | six pages, controls, live feed | yes | component tests; rendered in Chromium at desktop and phone sizes against the real API |
| 6 Testing & deployment | tests, compose, backups, monitoring | yes | see above; backup/restore scripts are syntax-checked only |
| 7 Exchange testnet | Binance Spot Testnet adapter, reconciliation | code complete | adapter tested against a faked CCXT client. **No real testnet order has been placed** (needs your testnet keys). |

## First-run checklist (closes the gaps above)

1. `docker compose up -d --build` on the VPS; confirm every service is healthy.
2. Worker log shows `markets_refreshed` and `ws_connected`; header shows "market data fresh". If the log shows HTTP 451, your VPS region is geo-blocked: set `BINANCE_REST_URL=https://data-api.binance.vision` and `BINANCE_WS_URL=wss://data-stream.binance.vision`. Check that `news_refreshed` reports new items.
3. Add an LLM key; Agent Monitor shows six agents online; trigger a cycle and read the first decisions.
4. Run in paper mode for at least a few weeks; review rejected and approved proposals in Trade Analysis.
5. Add testnet keys, switch `TRADING_MODE=testnet`, confirm one full entry → exit round trip and a clean
   reconciliation. Kill the engine container during an open position and confirm recovery.

## Simulated failure scenarios in the test suite

Exchange timeout (filled and not filled) · insufficient balance · market data outage · duplicate order ·
daily loss limit reached · restart during an open position · crash between order intent and send ·
crash mid-exit · exit order failure · reconciliation mismatch · LLM timeout / invalid output / provider down ·
price going stale mid-cycle · interrupted workflow run.

## Next

* Exchange-native protective orders (OCO) so stops survive engine downtime.
* Function-calling agents where it pays for itself (ADR-1), e.g. a Scout that can request extra timeframes.
* More sentiment sources (X/Reddit via official APIs), with source reliability scoring.
* Backtesting harness that replays stored candles through the same graph.
* Solana DEX adapter (implements `ExchangeAdapter`; needs a quote/route step and on-chain confirmation handling).
* Prometheus + Grafana + Alertmanager compose profile.
* **Live trading milestone**: only after a sustained, reviewed testnet record; requires exchange-native stops,
  2FA on the dashboard, and an independent review of the risk engine.
