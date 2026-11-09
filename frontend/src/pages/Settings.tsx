import { useEffect, useState, type ReactNode } from "react";
import { api } from "../api";
import { Badge, Button, Card, ErrorNote } from "../components/ui";
import { usePoll } from "../hooks";

const RISK_FIELDS: [string, string, string][] = [
  ["starting_capital", "Starting capital ($)", "Virtual capital the ledger starts from"],
  ["risk_per_trade_pct", "Max risk per trade (%)", "Hard cap 2%"],
  ["max_daily_loss_pct", "Max daily loss (%)", "Trading suspends when reached"],
  ["max_drawdown_pct", "Max drawdown (%)", "Trading suspends when reached"],
  ["max_exposure_pct", "Max portfolio exposure (%)", "Share of equity in positions"],
  ["max_open_positions", "Max simultaneous positions", ""],
  ["min_risk_reward", "Minimum risk/reward", "e.g. 2 means 2:1"],
  ["fee_rate", "Fee rate per side", "0.001 = 0.1%"],
  ["slippage_bps", "Slippage (bps)", "Applied to simulated fills and sizing"],
  ["max_entry_deviation_pct", "Max entry deviation (%)", "Market vs proposed entry"],
  ["min_stop_distance_pct", "Min stop distance (%)", ""],
  ["max_stop_distance_pct", "Max stop distance (%)", ""],
];
const inputCls = "w-full rounded-lg border border-axis bg-page px-3 py-2 text-sm text-ink";
const Configured = ({ ok }: { ok: boolean }) => <Badge tone={ok ? "good" : "neutral"}>{ok ? "Configured" : "Not configured"}</Badge>;
const Row = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line py-2 text-sm first:border-0"><span className="text-ink2">{label}</span><span>{children}</span></div>
);

export function Settings() {
  const { data, error, refresh } = usePoll<any>("/settings", 60000);
  const [risk, setRisk] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    if (data) setRisk(Object.fromEntries(Object.entries(data.risk).map(([k, v]) => [k, String(v)])));
  }, [data]);
  if (!data) return <ErrorNote error={error} />;

  const save = async (path: string, body: unknown, label: string) => {
    try {
      await api(path, { method: "PUT", body });
      setMsg({ ok: true, text: `${label} saved.` });
      refresh();
    } catch (e: any) {
      setMsg({ ok: false, text: `${label} not saved: ${e.message}` });
    }
  };
  const live = data.trading_mode === "live";

  return (
    <div className="space-y-4">
      {msg && <p role="status" className={`rounded-lg border px-3 py-2 text-sm ${msg.ok ? "border-good/50 bg-good/15" : "border-crit/50 bg-crit/15"}`}>{msg.text}</p>}
      <div className="grid gap-4 xl:grid-cols-2">
        <Card title="Trading mode">
          <Row label="Current mode"><Badge tone={live ? "crit" : data.trading_mode === "testnet" ? "warn" : "info"}>{data.trading_mode.toUpperCase()}</Badge></Row>
          <Row label="Live trading enabled on server"><Badge tone={data.live_trading_enabled ? "crit" : "good"}>{data.live_trading_enabled ? "Yes" : "No (default)"}</Badge></Row>
          {data.mode_problems.map((p: string) => <p key={p} className="mt-1 text-sm text-ink2">▲ {p}</p>)}
          <p className="mt-3 text-xs text-muted">The mode is set with TRADING_MODE in the server's .env file and cannot be changed from the browser. Live trading additionally needs
            LIVE_TRADING_ENABLED, a confirmation phrase and separate live credentials on the server.</p>
        </Card>
        <Card title="Exchange configuration">
          <Row label="Market data source">{data.exchange.market_data_source}</Row>
          <Row label="Quote asset">{data.exchange.quote_asset}</Row>
          <Row label="Tracked universe">top {data.exchange.universe_max_symbols} by volume · {data.exchange.timeframes.join(", ")}</Row>
          <Row label="Binance testnet API key"><span className="flex items-center gap-2">{data.exchange.testnet_credentials.key_hint && <code className="text-xs text-ink2">{data.exchange.testnet_credentials.key_hint}</code>}<Configured ok={data.exchange.testnet_credentials.configured} /></span></Row>
          <Row label="Live API key"><Configured ok={data.exchange.live_credentials.configured} /></Row>
        </Card>
        <Card title="AI providers and API credentials">
          {data.providers.map((p: any) => (
            <Row key={p.name} label={`${p.name} · ${p.base_url || "no endpoint"}`}>
              <span className="flex items-center gap-2">{p.key_hint && <code className="text-xs text-ink2">{p.key_hint}</code>}<Configured ok={p.configured} /></span>
            </Row>
          ))}
          <Row label="LLM calls today"><span className="num">{data.llm.calls_today} / {data.llm.daily_call_budget}</span></Row>
          <Row label="Cycle interval"><span className="num">{data.llm.cycle_interval_seconds}s</span></Row>
          <p className="mt-3 text-xs text-muted">API keys are stored only in the server environment. They are never sent to this browser; only the last four characters are shown.</p>
        </Card>
        <Card title="Agent models">
          <div className="space-y-2">
            {Object.entries<any>(data.agents).map(([name, cfg]) => <AgentRow key={name} name={name} cfg={cfg} providers={data.providers} onSave={(b) => save(`/settings/agents/${name}`, b, `Agent ${name}`)} />)}
          </div>
        </Card>
        <Card title="Risk parameters" className="xl:col-span-2">
          <form className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4" onSubmit={(e) => { e.preventDefault(); save("/settings/risk", { ...risk, max_open_positions: Number(risk.max_open_positions) }, "Risk parameters"); }}>
            {RISK_FIELDS.map(([key, label, hint]) => (
              <label key={key} className="text-sm">
                <span className="text-ink2">{label}</span>
                <input className={`${inputCls} num mt-1`} inputMode="decimal" value={risk[key] ?? ""} onChange={(e) => setRisk({ ...risk, [key]: e.target.value })} />
                {hint && <span className="mt-0.5 block text-xs text-muted">{hint}</span>}
              </label>
            ))}
            <div className="flex items-end sm:col-span-2 lg:col-span-4"><Button variant="primary" type="submit">Save risk parameters</Button></div>
          </form>
          <p className="mt-3 text-xs text-muted">Values are validated on the server against hard bounds. Changes are written to the audit log and apply to the next trade.</p>
        </Card>
        <Card title="Notifications">
          <Row label="Telegram"><Configured ok={data.notifications.telegram_configured} /></Row>
          <Row label="Webhook"><Configured ok={data.notifications.webhook_configured} /></Row>
          {(["enabled", "on_trade", "on_risk_event", "on_system_error"] as const).map((k) => (
            <label key={k} className="flex items-center justify-between border-t border-line py-2 text-sm">
              <span className="text-ink2">{{ enabled: "Notifications enabled", on_trade: "Trade executions", on_risk_event: "Risk events and status changes", on_system_error: "Critical system errors" }[k]}</span>
              <input type="checkbox" className="size-4" checked={data.notifications[k]}
                     onChange={(e) => save("/settings/notifications", { enabled: data.notifications.enabled, on_trade: data.notifications.on_trade, on_risk_event: data.notifications.on_risk_event, on_system_error: data.notifications.on_system_error, [k]: e.target.checked }, "Notifications")} />
            </label>
          ))}
        </Card>
        <Card title="Security">
          <Row label="IP allowlist"><Badge tone={data.security.ip_allowlist_active ? "good" : "neutral"}>{data.security.ip_allowlist_active ? "Active" : "Not set"}</Badge></Row>
          <Row label="Allowed origins"><span className="text-xs text-ink2">{data.security.cors_origins.join(", ")}</span></Row>
        </Card>
      </div>
    </div>
  );
}

function AgentRow({ name, cfg, providers, onSave }: { name: string; cfg: any; providers: any[]; onSave: (b: any) => void }) {
  const [provider, setProvider] = useState(cfg.provider);
  const [model, setModel] = useState(cfg.model);
  const dirty = provider !== cfg.provider || model !== cfg.model;
  return (
    <div className="grid grid-cols-[6.5rem_1fr] items-center gap-2 sm:grid-cols-[7rem_7rem_1fr_auto]">
      <span className="text-sm capitalize">{name.replace("_", " ")}</span>
      <select aria-label={`${name} provider`} className={inputCls} value={provider} onChange={(e) => setProvider(e.target.value)}>
        {providers.map((p) => <option key={p.name} value={p.name}>{p.name}{p.configured ? "" : " (no key)"}</option>)}
      </select>
      <input aria-label={`${name} model`} className={`${inputCls} col-span-2 sm:col-span-1`} value={model} onChange={(e) => setModel(e.target.value)} placeholder="model id" />
      <Button disabled={!dirty || !model} onClick={() => onSave({ provider, model, enabled: cfg.enabled, temperature: cfg.temperature })} className="col-span-2 sm:col-span-1">Save</Button>
    </div>
  );
}
