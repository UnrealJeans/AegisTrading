# Architecture

## Processes

| Service (compose) | Entry point | Responsibility | Talks to |
|---|---|---|---|
| `backend` | `aegis.services.api` | REST + WebSocket for the dashboard, auth, operator controls. Never sends orders, never calls an LLM. | PostgreSQL, Redis |
| `worker` | `aegis.services.worker` | Market data: Binance WebSocket mini-ticker stream, REST 24h tickers, closed-candle history, universe selection, staleness detection, news/RSS ingestion, notifications. | Exchange (public), news feeds, PostgreSQL, Redis |
| `orchestrator` | `aegis.services.orchestrator` | Runs LangGraph cycles (schedule or operator request). The only service that calls LLM APIs. | LLM providers, PostgreSQL, Redis |
| `trading-engine` | `aegis.services.engine` | The only service that sends orders. Executes approved trades, manages stop-loss/take-profit, snapshots the portfolio, enforces circuit breakers, reconciles balances, recovers after crashes. | Exchange (private), PostgreSQL, Redis |
| `postgres` | | System of record. | |
| `redis` | | Ephemeral: price cache, pub/sub to the dashboard, heartbeats, rate-limit and LLM-budget counters. | |
| `migrate` | `alembic upgrade head` | One-shot schema migration before the app services start. | PostgreSQL |
| `frontend` | nginx | Static dashboard + reverse proxy for `/api`. | backend |
| `backup` | `deploy/backup.sh` | Nightly `pg_dump` with retention. | PostgreSQL |

Rule of thumb: **PostgreSQL holds everything that must survive a crash; Redis holds only what can be rebuilt.**
Losing Redis loses nothing but a few seconds of prices.

## The agent workflow (LangGraph)

`backend/aegis/orchestration/graph.py`

```
START → scan ─(no candidates)→ END
          │
        scout → next_candidate ─(queue empty)→ END
                   │  ├──────────► news ───────┐           parallel fan-out / fan-in
                   │  └──────────► technical ──┴► analyst ─(NO_TRADE / error)──────────┐
                   │                                 │                                 │
                   │                              skeptic ─(REJECT / unavailable)──────┤
                   │                                 │                                 │
                   │                               risk  (engine → Risk Manager)       │
                   │                                 │                                 │
                   └──────────────────────────── finalize ◄────────────────────────────┘
```

* **Shared state** is a typed `CycleState` dict. `results` and `trace` use reducer channels so parallel branches can
  append safely.
* **Structured communication**: every hand-off is a Pydantic-validated object, persisted as an `agent_messages`
  row with sender, recipient, kind and JSON payload. There is no shared free-text chat.
* **Retries and timeouts** live in `agents/base.py`: a per-agent wall-clock deadline, bounded retries on transport
  errors, and one "repair" retry when the model's JSON fails schema or semantic validation.
* **Tracing**: each node appends a timed step to `workflow_runs.trace`; each agent call is an `agent_executions`
  row (input, output, model, tokens, duration, status).
* **Fail-closed**: a failed/unavailable Skeptic or Risk Manager rejects the trade. A failed Technical Analyst means
  the Analyst is not asked at all.

### The six agents

| Agent | Input (deterministic "tools") | Output schema | Hard validation in code |
|---|---|---|---|
| Scout | scanner candidates | ranked picks + rejections | symbols must be scanner candidates; held symbols dropped |
| News & Sentiment | stored news items (with ids), Fear & Greed index | sentiment, score, key events, conflicts | every event must cite a provided item id, otherwise it is removed; no data → neutral, no LLM call |
| Technical Analyst | indicator snapshots for 15m/1h/4h | bias, signals, key levels, volatility | key levels are snapped to computed levels or nulled |
| Analyst | the three reports + reference data | BUY / SELL / NO_TRADE + proposal | price geometry; risk/reward recomputed in code; SELL only to close an open position |
| Skeptic | proposal + all evidence | verdict + graded objections | any `critical` objection forces REJECT |
| Risk Manager | proposal + engine assessment + portfolio | APPROVE / REDUCE / REJECT + multiplier | multiplier ≤ 1; cannot override an engine rejection |

Each agent has its own system prompt and its own provider/model (`AGENT_<NAME>_MODEL=provider:model` or the
Settings page). Agents are plain objects with a `run(input, ctx)` method and are unit-tested in isolation.

## Decision records

**ADR-1 — Agents receive data; they do not call tools in a loop.**
Each agent's "tools" (scanner, indicator engine, news store, risk engine, ledger) are executed deterministically
*before* a single LLM call, and their results form the agent's typed input. Alternative: native function-calling
loops. Rejected for now because loops multiply cost and latency unpredictably, make runs non-reproducible, and let a
model decide which facts to look at. Consequence: one LLM call per agent per candidate in the normal case, and
indicator values can never be "computed" by a model.

**ADR-2 — Cost control is structural.**
A free deterministic scanner decides whether a cycle starts. Symbols analysed recently are on cooldown. Agents skip
the LLM when there is nothing to reason about (no candidates, no news, not enough candles, engine veto, exit orders).
Conditional edges end a candidate's flow at the first NO. A per-day call budget is enforced in Redis. A typical idle
cycle costs zero LLM calls; a full approval costs six.

**ADR-3 — Two risk layers; the deterministic one is the authority and runs twice.**
`risk/engine.py` is pure functions with no I/O. It runs in the workflow *before* the Risk Manager agent (so the LLM
sees real numbers and cannot be asked to approve something the code rejected), and again inside the trading engine
against a fresh price immediately before the order. The LLM can only veto or shrink. Runtime-editable risk
parameters have hard upper bounds in `RiskParams`.

**ADR-4 — Durable hand-off through PostgreSQL, not a message queue.**
An approval is one transaction that sets the proposal to APPROVED and inserts an `approved_trades` row with a TTL.
The engine polls and claims rows with `SELECT … FOR UPDATE SKIP LOCKED`. Alternative: Redis streams. Rejected because
the approval, its audit trail and its execution status would then live in two stores that can disagree after a crash.

**ADR-5 — Idempotent execution.**
Every order intent gets a deterministic client order id (`aegis-e-<approved_trade_id>` for entries,
`aegis-x-<position_id>-<attempt>` for exits) written to the `orders` table (UNIQUE) **before** the request is sent.
If the response is lost or the process dies, the order stays `SUBMITTED` and is resolved by looking it up on the
venue by that id. It is never blindly resent. `positions.approved_trade_id` is also UNIQUE, so one approval can
never create two positions.

**ADR-6 — Spot, long-only, no leverage.**
BUY opens a position. SELL from the Analyst is only accepted as a recommendation to close an existing position
(exit review is triggered when a held asset breaks down). Shorting is rejected by the engine.

**ADR-7 — Stops and targets are monitored by the engine, not resting on the exchange.**
This keeps the adapter interface small (market order, lookup, balances) so new venues are cheap to add, and behaves
identically in paper and testnet. Trade-off: if the engine is down, exits do not fire. Mitigations: containers
restart automatically, recovery checks every open position immediately, stale prices raise CRITICAL risk events,
and health/heartbeat is exposed. Exchange-native OCO orders are the planned improvement before any live milestone.

**ADR-8 — A virtual sub-account.**
The ledger (positions table) starts at `STARTING_CAPITAL` and is the source of truth for cash, equity and P&L.
Testnet accounts come pre-funded with arbitrary balances, so the engine does not trade "the whole account"; it
reconciles instead: every 5 minutes exchange balances must cover ledger positions, otherwise trading is suspended.
The engine never auto-corrects a mismatch.

**ADR-9 — Market data comes from the venue where fills happen.**
Paper mode reads Binance mainnet public data (real prices, real liquidity). Testnet mode reads testnet prices,
because stops must trigger on prices that are executable there. Only closed candles are stored, so indicators never
repaint. Stale tickers raise `StaleDataError` rather than returning an old price.

**ADR-10 — Trading mode is server-side configuration.**
`TRADING_MODE` cannot be changed from the dashboard. Live mode additionally requires `LIVE_TRADING_ENABLED=true`,
an exact confirmation phrase, and live credentials that differ from the testnet ones. If any is missing the engine
refuses to build an adapter and stays idle. Exchange keys must have withdrawals disabled; the code never calls a
withdrawal endpoint and the CCXT client is restricted to spot markets.

**ADR-11 — One OpenAI-compatible HTTP provider.**
OpenAI, xAI and most gateways share the Chat Completions wire format, so one small `httpx` implementation behind the
`LLMProvider` protocol covers them. A provider with a different wire format is a new class implementing one method.

## Data model

`workflow_runs` → `agent_executions`, `agent_messages` → `trade_proposals` → `approved_trades` | `rejected_trades`
→ `orders` → `positions`. Also: `assets`, `candles`, `news_items`, `portfolio_snapshots`, `risk_events`,
`system_events`, `system_state` (control status, runtime risk params, agent config), `users`, `audit_logs`.
Open and closed positions share one table with a `status` column. Money columns are `NUMERIC(36,18)`.

## Trading status

`STOPPED → RUNNING ⇄ PAUSED`, `RUNNING → SUSPENDED` (automatic: daily loss, drawdown, exit failure, engine error,
reconciliation mismatch), any `→ EMERGENCY_STOP` (operator; optional liquidation). Only `RUNNING` allows new
entries. Protective exits run in every status. Leaving `SUSPENDED` or `EMERGENCY_STOP` requires explicit confirmation.

## Security

Argon2id password hashing, short-lived JWT in `sessionStorage`, WebSocket auth in the first frame (not the URL),
restricted CORS and WebSocket origin check, optional IP allowlist, per-IP rate limits (stricter on login), strict
input validation, audit log for logins, control actions and settings changes, secrets only in server environment
(the settings endpoint returns "configured" flags and the last four characters). Containers run as a non-root user
with a read-only filesystem and no capabilities; only the dashboard port is published, on localhost by default.

## Known risks

| Risk | Mitigation / status |
|---|---|
| LLM quality varies; a model can be confidently wrong | Skeptic + deterministic engine + small fixed risk; paper mode first; all decisions logged for review |
| Engine downtime leaves positions unmanaged (ADR-7) | auto-restart, recovery on start, alerts; exchange-native stops planned |
| Paper fills are optimistic (no order-book depth) | configurable slippage/fees; order size capped at 0.1% of 24h volume |
| News coverage is limited to RSS + optional CryptoPanic; no direct social feed | agents are told data quality; missing data lowers confidence, never invents |
| Single VPS, single database | nightly dumps; restore script; no HA |
| Binance may geo-block a VPS region | endpoints are configurable (`BINANCE_REST_URL`, `BINANCE_WS_URL`) |
