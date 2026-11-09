import { Badge, Card, ErrorNote, Meter, Stat, statusTone, Table } from "../components/ui";
import { money, num, pct, time } from "../format";
import { usePoll } from "../hooks";

export function Risk({ version }: { version: number }) {
  const { data, error } = usePoll<any>(`/risk?v=${version}`, 8000);
  if (!data) return <ErrorNote error={error} />;
  const u = data.usage, p = data.params;
  const n = (v: unknown) => num(v) ?? 0;
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Portfolio exposure" value={pct(u.exposure_pct, 1)} sub={`${money(u.exposure)} of ${money(u.max_exposure_amount)}`} />
        <Stat label="Daily loss" value={pct(u.daily_loss_pct)} sub={`Limit ${pct(p.max_daily_loss_pct, 1)}`} />
        <Stat label="Drawdown" value={pct(u.drawdown_pct)} sub={`Limit ${pct(p.max_drawdown_pct, 1)}`} />
        <Stat label="Risk per trade" value={money(u.risk_per_trade_amount)} sub={`${pct(p.risk_per_trade_pct, 1)} of equity`} />
      </div>
      <Card title="Limit usage">
        <div className="grid gap-4 md:grid-cols-2">
          <Meter label="Exposure" value={n(u.exposure)} limit={n(u.max_exposure_amount)} format={(v) => money(v)} />
          <Meter label="Open positions (incl. pending)" value={u.open_positions} limit={p.max_open_positions} format={(v) => String(v)} />
          <Meter label="Daily loss" value={Math.max(0, -n(u.daily_pnl))} limit={n(u.daily_loss_limit_amount)} format={(v) => money(v)} />
          <Meter label="Drawdown" value={n(u.drawdown_pct)} limit={n(p.max_drawdown_pct)} format={(v) => pct(v)} />
        </div>
        {u.stale_marks.length > 0 && <p className="mt-3 text-sm text-ink2">▲ Marked at cost because the price feed is stale: {u.stale_marks.join(", ")}</p>}
        <p className="mt-4 text-xs text-muted">Position sizing: quantity = (equity × {pct(p.risk_per_trade_pct, 1)}) ÷ (entry − stop + fees + slippage), then capped by exposure, cash and
          0.1% of 24h volume. Minimum risk/reward {Number(p.min_risk_reward).toFixed(1)} : 1. No leverage. These checks run in code and cannot be overridden by an agent.</p>
      </Card>
      <Card title="Risk events">
        <Table empty="No risk events recorded." head={["Time", "Severity", "Type", "Symbol", "Message"]}
               rows={data.risk_events.map((e: any) => [time(e.created_at), <Badge tone={statusTone(e.severity)}>{e.severity}</Badge>, e.kind.replace(/_/g, " "), e.symbol ?? "—",
                 <span className="block max-w-xl whitespace-normal text-ink2">{e.message}</span>])} />
      </Card>
      <Card title="System events">
        <Table empty="No system events recorded." head={["Time", "Severity", "Service", "Type", "Message"]}
               rows={data.system_events.map((e: any) => [time(e.created_at), <Badge tone={statusTone(e.severity)}>{e.severity}</Badge>, e.service, e.kind.replace(/_/g, " "),
                 <span className="block max-w-xl whitespace-normal text-ink2">{e.message}</span>])} />
      </Card>
    </div>
  );
}
