# Deployment (Ubuntu VPS, ~2 vCPU / 4 GB RAM / 40 GB SSD)

## 1. Prepare the server

```bash
git clone <this repository> aegis && cd aegis
sudo bash deploy/setup-vps.sh          # Docker, ufw (SSH only), fail2ban, unattended upgrades, 2 GB swap
```

## 2. Configure

```bash
cp .env.example .env && chmod 600 .env
python3 -c "import secrets; print(secrets.token_urlsafe(48))"   # → SECRET_KEY
```

Required: `SECRET_KEY` (≥32 chars), `ADMIN_PASSWORD` (≥12 chars), `POSTGRES_PASSWORD`, and one LLM key.
The API refuses to start with weak or missing secrets.

## 3. Start

```bash
docker compose up -d --build
docker compose ps                      # all services should become "healthy"
docker compose logs -f orchestrator    # JSON logs
```

Migrations run automatically (`migrate` service). All services use `restart: unless-stopped`, so the stack comes
back after a crash or reboot. State is in the `pgdata` volume.

## 4. Reach the dashboard securely

**Option A — SSH tunnel (default, nothing exposed):**
`ssh -L 8080:127.0.0.1:8080 user@vps` then open http://localhost:8080. Keep `CORS_ORIGINS=http://localhost:8080`.

**Option B — HTTPS with a domain:**
set `DOMAIN=trading.example.com` and `CORS_ORIGINS=https://trading.example.com` in `.env`, point DNS at the VPS, then

```bash
sudo OPEN_HTTPS=yes bash deploy/setup-vps.sh
docker compose --profile tls up -d
```

Caddy obtains a Let's Encrypt certificate. Strongly consider `ALLOWED_IPS=<your ip>/32` as well.

Sign in, check the service badges and the mode banner, then press **Start**. Trading status persists across restarts.

## 5. Binance Spot Testnet (Phase 7)

1. Create keys at https://testnet.binance.vision (GitHub login → Generate HMAC_SHA256 Key).
2. In `.env`: `TRADING_MODE=testnet`, `BINANCE_TESTNET_API_KEY=…`, `BINANCE_TESTNET_API_SECRET=…`.
3. `docker compose up -d` and confirm the banner says **TESTNET** and `trading-engine` is up.

Testnet and paper ledgers are separate (positions are tagged with their mode). The testnet is reset by Binance
about once a month; open ledger positions will then fail reconciliation and suspend trading, which is the intended
behaviour. Close or clear them before resuming.

## 6. Operations

| Task | Command |
|---|---|
| Status / health | `docker compose ps` |
| Logs | `docker compose logs -f --tail 200 <service>` |
| Update | `git pull && docker compose up -d --build` |
| Stop gracefully | `docker compose stop` (30 s grace; open positions are picked up again on start) |
| Backups | written nightly to `./backups/aegis-*.dump`, kept 14 days (`BACKUP_KEEP_DAYS`). Copy them off the server. |
| Restore | `./deploy/restore.sh backups/aegis-<timestamp>.dump` |
| Metrics | `docker compose exec backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())"` (Prometheus format; not exposed publicly) |

Monitoring built in: container health checks (API readiness; heartbeat probes for worker, orchestrator and engine),
service and market-data status in the dashboard header, risk and system event logs, optional Telegram/webhook
notifications for fills, risk events, status changes and critical errors. An external uptime check against
`/api/health` and a Prometheus scrape of `/metrics` are recommended additions.

## 7. Recovery behaviour

| Event | What happens |
|---|---|
| Any container crashes | Docker restarts it. |
| Trading engine restarts | Resolves in-flight orders by client order id, fails approvals that never produced an order, resumes stop/target monitoring, reconciles balances. |
| Orchestrator restarts mid-cycle | The run is marked ABORTED. No trade can result from a partial cycle. |
| Market data stops | Tickers go stale → no entries, no paper fills, CRITICAL events for open positions; recovers automatically. |
| Daily loss / drawdown limit | Status becomes SUSPENDED; an operator must confirm Resume. |
| Redis lost | Prices and heartbeats rebuild within seconds; nothing durable is lost. |
