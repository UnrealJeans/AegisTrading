# Aegis Trading

Autonomous multi-agent AI crypto trading platform. Six LLM agents, coordinated by LangGraph, analyse real
exchange data, argue about every trade, and hand approved proposals to a separate trading engine that is guarded
by a deterministic risk engine. Runs 24/7 on a small VPS with Docker Compose.

**Status: paper trading and Binance Spot Testnet. Live trading is disabled and is a separate future milestone.**
Nothing here is financial advice; the default risk settings are test values, not a profitable strategy.

```
            ┌────────────────────────── orchestrator (LangGraph) ───────────────────────────┐
 scanner →  │ Scout → ┬ News & Sentiment ┬ → Analyst → Skeptic → Risk engine + Risk Manager │ → approved_trades
 (no LLM)   │         └ Technical Analyst┘                       (code)        (LLM, veto)  │    (PostgreSQL)
            └───────────────────────────────────────────────────────────────────────────────┘         │
 worker: Binance WebSocket + REST, candles, news  ──► Redis (prices) / PostgreSQL (history)            ▼
 backend: FastAPI + WebSocket ──► React dashboard                    trading engine: re-validate → order → SL/TP
```

## What is in the box

| Path | Purpose |
|---|---|
| `backend/aegis/agents` | The six agents: prompts, typed input/output schemas, validation, execution history |
| `backend/aegis/llm` | Provider abstraction (OpenAI, xAI Grok, any OpenAI-compatible endpoint) |
| `backend/aegis/orchestration` | LangGraph workflow, cycle runner, tracing |
| `backend/aegis/risk` | Deterministic risk engine (pure Python, no LLM) |
| `backend/aegis/trading` | Trading engine, portfolio ledger, exchange adapters (paper, Binance Spot) |
| `backend/aegis/marketdata` | CCXT/WebSocket market data, indicators, scanner, news ingestion |
| `backend/aegis/api` | FastAPI REST + WebSocket for the dashboard |
| `backend/aegis/services` | Process entry points: `api`, `worker`, `orchestrator`, `engine` |
| `frontend/` | React + TypeScript + Vite + Tailwind + Recharts dashboard |
| `deploy/` | VPS setup, backup/restore, optional TLS proxy |
| `docs/` | [Architecture & decisions](docs/ARCHITECTURE.md), [Deployment](docs/DEPLOYMENT.md), [Roadmap & status](docs/ROADMAP.md) |

## Quick start (VPS or any Docker host)

```bash
cp .env.example .env
# fill in: SECRET_KEY, ADMIN_PASSWORD, POSTGRES_PASSWORD and at least one LLM key (OPENAI_API_KEY or XAI_API_KEY)
docker compose up -d --build
ssh -L 8080:127.0.0.1:8080 user@your-vps      # the dashboard is bound to localhost by default
# open http://localhost:8080, sign in, press Start
```

Without an LLM key the stack still runs, collects market data and shows the dashboard, but the agents are shown as
**offline** and no analysis happens. The system never substitutes fake agent output.

Full instructions, including HTTPS and Binance Testnet, are in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Credentials you need

| Credential | Where to get it | Needed for |
|---|---|---|
| `OPENAI_API_KEY` | https://platform.openai.com/api-keys | agents (if using OpenAI) |
| `XAI_API_KEY` | https://console.x.ai | agents (if using Grok) |
| `BINANCE_TESTNET_API_KEY/SECRET` | https://testnet.binance.vision → log in with GitHub → *Generate HMAC_SHA256 Key* | `TRADING_MODE=testnet` only |
| `CRYPTOPANIC_API_KEY` | https://cryptopanic.com/developers/api/ | optional extra news source |
| `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | @BotFather | optional notifications |

Paper mode needs no exchange key at all: it uses public market data.
Model names in `.env.example` are defaults; set whichever model ids your account offers.

## Development

```bash
# backend
cd backend && python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q                                   # SQLite + fakeredis, no services needed
TEST_DATABASE_URL=postgresql+asyncpg://... .venv/bin/pytest -q   # same suite on PostgreSQL
.venv/bin/alembic upgrade head                        # needs DATABASE_URL

# frontend
cd frontend && npm ci && npm test && npm run dev      # dev server proxies /api to localhost:8000
```

## Safety model in one paragraph

LLM output is never executed directly. A proposal must pass the Skeptic, then the deterministic risk engine
(position size, 1% risk, 30% exposure, 3% daily loss, drawdown, 2:1 reward/risk, liquidity, fresh data), then the
Risk Manager agent, which can only veto or shrink. The trading engine then **re-runs the deterministic checks**
against a fresh price before sending anything, uses an idempotency key per order, and suspends new trading
automatically on limit breaches, exchange errors or ledger mismatches. Any missing or failed review rejects the trade.
