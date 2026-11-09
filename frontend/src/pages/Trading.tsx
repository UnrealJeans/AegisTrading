import { Badge, Card, ErrorNote, statusTone, Table } from "../components/ui";
import { money, pnlTone, price, qty, time } from "../format";
import { usePoll } from "../hooks";

export function Trading({ version, onOpenProposal }: { version: number; onOpenProposal: (id: number) => void }) {
  const open = usePoll<any[]>(`/positions?status=open&v=${version}`, 5000);
  const closed = usePoll<any[]>(`/positions?status=closed&v=${version}`, 20000);
  const pending = usePoll<any[]>(`/approved?v=${version}`, 5000);
  const orders = usePoll<any[]>(`/orders?limit=50&v=${version}`, 10000);
  const pnl = (v: unknown) => <span className={pnlTone(v)}>{money(v, { sign: true })}</span>;
  return (
    <div className="space-y-4">
      <ErrorNote error={open.error || closed.error || pending.error || orders.error} />
      <Card title="Open positions">
        <Table empty="No open positions." head={["Symbol", "Status", "Quantity", "Entry", "Current", "Stop-loss", "Take-profit", "Unrealized P&L", "Opened"]}
               rows={(open.data ?? []).map((x) => [
                 <span className="font-medium">{x.symbol}</span>, <Badge tone={statusTone(x.status)}>{x.status}</Badge>, qty(x.quantity), price(x.entry_price),
                 <span>{price(x.current_price)}{x.price_stale && <span className="ml-1 text-xs text-warn">▲ stale</span>}</span>,
                 price(x.stop_loss), price(x.take_profit), pnl(x.unrealized_pnl), time(x.opened_at),
               ])} />
      </Card>
      <Card title="Pending orders (approved, awaiting execution)">
        <Table empty="Nothing is waiting for execution." head={["Symbol", "Side", "Status", "Quantity", "Entry", "Stop-loss", "Take-profit", "Risk", "Expires", ""]}
               rows={(pending.data ?? []).map((x) => [
                 <span className="font-medium">{x.symbol}</span>, x.side, <Badge tone={statusTone(x.status)}>{x.status}</Badge>, qty(x.quantity), price(x.entry_price),
                 price(x.stop_loss), price(x.take_profit), money(x.risk_amount), time(x.expires_at),
                 <button className="text-series underline" onClick={() => onOpenProposal(x.proposal_id)}>Analysis</button>,
               ])} />
      </Card>
      <Card title="Closed positions">
        <Table empty="No closed positions yet." head={["Symbol", "Quantity", "Entry", "Exit", "Realized P&L", "Fees", "Reason", "Closed"]}
               rows={(closed.data ?? []).map((x) => [
                 <span className="font-medium">{x.symbol}</span>, qty(x.quantity), price(x.entry_price), price(x.exit_price), pnl(x.realized_pnl),
                 money(Number(x.entry_fee) + Number(x.exit_fee)), (x.close_reason ?? "").replace("_", " "), time(x.closed_at),
               ])} />
      </Card>
      <Card title="Order log">
        <Table empty="No orders have been sent." head={["Time", "Symbol", "Side", "Purpose", "Status", "Quantity", "Fill price", "Fee", "Venue", "Client order id"]}
               rows={(orders.data ?? []).map((o) => [
                 time(o.created_at), <span className="font-medium">{o.symbol}</span>, o.side, o.purpose, <Badge tone={statusTone(o.status)}>{o.status}</Badge>,
                 qty(o.filled_quantity), price(o.avg_fill_price), money(o.fee), o.exchange, <span className="text-xs text-ink2">{o.client_order_id}</span>,
               ])} />
      </Card>
    </div>
  );
}
