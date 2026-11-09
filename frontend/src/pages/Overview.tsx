import { EquityChart, type EquityPoint } from "../components/EquityChart";
import { Feed } from "../components/Feed";
import { Card, ErrorNote, Stat, Table } from "../components/ui";
import { money, num, pct, pnlTone, price, qty } from "../format";
import { usePoll, type LiveEvent } from "../hooks";

export function Overview({ overview, events, version }: { overview: any; events: LiveEvent[]; version: number }) {
  const hist = usePoll<EquityPoint[]>(`/portfolio/history?hours=168&v=${version}`, 60000);
  const open = usePoll<any[]>(`/positions?status=open&v=${version}`, 5000);
  const p = overview.portfolio, s = overview.stats;
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="Portfolio value" value={money(p.equity)} sub={`Cash ${money(p.cash)}`} />
        <Stat label="Total P&L" value={money(p.total_pnl, { sign: true })} tone={pnlTone(p.total_pnl)}
              sub={pct((num(p.total_pnl) ?? 0) / (num(p.starting_capital) || 1) * 100, 2, { sign: true })} />
        <Stat label="Daily P&L" value={money(p.daily_pnl, { sign: true })} tone={pnlTone(p.daily_pnl)} sub={`Daily loss used ${pct(p.daily_loss_pct)}`} />
        <Stat label="Win rate" value={s.win_rate === null ? "—" : pct(s.win_rate, 1)} sub={`${s.wins}W / ${s.losses}L`} />
        <Stat label="Closed trades" value={s.closed_trades} />
        <Stat label="Max drawdown" value={pct(s.max_drawdown_pct)} sub={`Current ${pct(p.drawdown_pct)}`} />
        <Stat label="Open positions" value={s.open_positions} sub={`Exposure ${pct(p.exposure_pct, 1)}`} />
        <Stat label="Unrealized P&L" value={money(p.unrealized_pnl, { sign: true })} tone={pnlTone(p.unrealized_pnl)} sub={`Realized ${money(p.realized_pnl, { sign: true })}`} />
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Card title="Portfolio equity (7 days)" className="xl:col-span-2">
          <ErrorNote error={hist.error} />
          <EquityChart points={hist.data ?? []} baseline={num(p.starting_capital) ?? 0} />
        </Card>
        <Card title="Live activity">
          <Feed events={events} limit={25} />
        </Card>
      </div>
      <Card title="Open positions">
        <ErrorNote error={open.error} />
        <Table empty="No open positions."
               head={["Symbol", "Quantity", "Entry", "Current", "Stop-loss", "Take-profit", "Unrealized P&L"]}
               rows={(open.data ?? []).map((x) => [
                 <span className="font-medium">{x.symbol}</span>, qty(x.quantity), price(x.entry_price),
                 <span>{price(x.current_price)}{x.price_stale && <span className="ml-1 text-xs text-warn">▲ stale</span>}</span>,
                 price(x.stop_loss), price(x.take_profit), <span className={pnlTone(x.unrealized_pnl)}>{money(x.unrealized_pnl, { sign: true })}</span>,
               ])} />
      </Card>
    </div>
  );
}
