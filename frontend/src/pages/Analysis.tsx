import type { ReactNode } from "react";
import { Badge, Card, ErrorNote, Json, statusTone } from "../components/ui";
import { money, pct, price, qty, time } from "../format";
import { usePoll } from "../hooks";

function Step({ n, title, badge, children }: { n: number; title: string; badge?: ReactNode; children: ReactNode }) {
  return (
    <li className="relative border-l border-axis pb-5 pl-6 last:border-transparent last:pb-0">
      <span className="num absolute -left-3 top-0 flex size-6 items-center justify-center rounded-full border border-axis bg-raised text-xs text-ink2">{n}</span>
      <div className="flex flex-wrap items-center gap-2"><h3 className="text-sm font-semibold">{title}</h3>{badge}</div>
      <div className="mt-2 space-y-2 text-sm text-ink2">{children}</div>
    </li>
  );
}
const Raw = ({ value }: { value: unknown }) => (
  <details><summary className="cursor-pointer text-xs text-muted">Structured output</summary><div className="mt-1"><Json value={value} /></div></details>
);

function Detail({ id }: { id: number }) {
  const { data: d, error } = usePoll<any>(`/proposals/${id}`, 15000);
  if (error) return <ErrorNote error={error} />;
  if (!d) return <p className="text-sm text-muted">Loading…</p>;
  const ev = d.evidence ?? {}, a = ev.analyst?.proposal, risk = ev.risk ?? {}, out = ev.outcome ?? {};
  return (
    <Card title={`${d.side} ${d.symbol} · proposal #${d.id}`} right={<Badge tone={statusTone(d.status)}>{d.status}</Badge>}>
      <ol>
        <Step n={1} title="Scout findings">
          <p>{ev.scout?.pick?.thesis ?? "Scanner candidate (exit review)"}</p>
          <p className="text-xs">{(ev.scout?.scanner?.signals ?? []).join(" · ")}</p>
          <Raw value={ev.scout} />
        </Step>
        <Step n={2} title="News analysis" badge={ev.news?.sentiment && <Badge tone="neutral">{ev.news.sentiment} · quality {ev.news.data_quality}</Badge>}>
          <p>{ev.news?.error ? `Unavailable: ${ev.news.error}` : ev.news?.summary}</p>
          <Raw value={ev.news} />
        </Step>
        <Step n={3} title="Technical analysis" badge={ev.technical?.bias && <Badge tone="neutral">{ev.technical.bias} · {ev.technical.timeframe_alignment}</Badge>}>
          <p>{ev.technical?.summary}</p>
          <Raw value={ev.technical} />
        </Step>
        <Step n={4} title="Analyst proposal" badge={<Badge tone="info">confidence {pct(d.confidence * 100, 0)}</Badge>}>
          <dl className="num grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
            <div><dt className="text-xs text-muted">Entry</dt><dd className="text-ink">{price(d.entry_price)}</dd></div>
            <div><dt className="text-xs text-muted">Stop-loss</dt><dd className="text-ink">{price(d.stop_loss)}</dd></div>
            <div><dt className="text-xs text-muted">Take-profit</dt><dd className="text-ink">{price(d.take_profit)}</dd></div>
            <div><dt className="text-xs text-muted">Risk/reward</dt><dd className="text-ink">{Number(d.risk_reward).toFixed(2)} : 1</dd></div>
          </dl>
          <p>Holding period: {d.holding_period || "—"}</p>
          <ul className="list-disc pl-5">{(a?.supporting_evidence ?? []).map((e: string, i: number) => <li key={i}>{e}</li>)}</ul>
        </Step>
        <Step n={5} title="Skeptic objections" badge={ev.skeptic?.verdict && <Badge tone={statusTone(ev.skeptic.verdict)}>{ev.skeptic.verdict}</Badge>}>
          {ev.skeptic?.error ? <p>Review unavailable: {ev.skeptic.error}</p> : !ev.skeptic ? <p>Not reached.</p> : (
            <>
              <p>{ev.skeptic.summary}</p>
              <ul className="space-y-1">
                {(ev.skeptic.objections ?? []).map((o: any, i: number) => (
                  <li key={i}><Badge tone={o.severity === "critical" || o.severity === "high" ? "crit" : o.severity === "medium" ? "warn" : "neutral"}>{o.severity}</Badge>{" "}
                    <span className="text-ink">{o.category.replace(/_/g, " ")}</span>: {o.detail}</li>
                ))}
              </ul>
            </>
          )}
        </Step>
        <Step n={6} title="Risk Manager decision" badge={risk.manager?.decision && <Badge tone={statusTone(risk.manager.decision)}>{risk.manager.decision}</Badge>}>
          {!risk.engine ? <p>Not reached.</p> : (
            <>
              <p>{risk.manager?.reasoning ?? risk.manager?.error}</p>
              <p className="text-xs text-muted">Deterministic risk engine checks (authoritative):</p>
              <ul className="grid gap-1 sm:grid-cols-2">
                {(risk.engine.checks ?? []).map((c: any, i: number) => (
                  <li key={i} className="text-xs"><span className={c.passed ? "text-good" : "text-crit"}>{c.passed ? "✓" : "✗"}</span> <span className="text-ink">{c.name.replace(/_/g, " ")}</span>: {c.detail}</li>
                ))}
              </ul>
              {d.approved_trade && <p className="num">Approved size {qty(d.approved_trade.quantity)} · modelled loss at stop {money(d.approved_trade.risk_amount)}</p>}
            </>
          )}
        </Step>
        <Step n={7} title="Final result" badge={<Badge tone={statusTone(out.status)}>{out.status ?? d.status}{out.stage ? ` at ${out.stage.replace("_", " ")}` : ""}</Badge>}>
          {(out.reasons ?? []).map((r: string, i: number) => <p key={i}>• {r}</p>)}
          {d.approved_trade && <p>Execution status: <span className="text-ink">{d.approved_trade.status}</span>{d.approved_trade.status_reason ? ` — ${d.approved_trade.status_reason}` : ""}</p>}
          {d.orders.map((o: any) => (
            <p key={o.id} className="num">{o.purpose} order {o.status}: {qty(o.filled_quantity)} @ {price(o.avg_fill_price)} on {o.exchange} ({o.mode}), fee {money(o.fee)}</p>
          ))}
          {d.position && <p className="num">Position {d.position.status}{d.position.realized_pnl != null ? ` · realized ${money(d.position.realized_pnl, { sign: true })} (${(d.position.close_reason ?? "").replace("_", " ")})` : ""}</p>}
        </Step>
      </ol>
    </Card>
  );
}

export function Analysis({ version, selected, onSelect }: { version: number; selected: number | null; onSelect: (id: number | null) => void }) {
  const { data, error } = usePoll<any[]>(`/proposals?limit=50&v=${version}`, 15000);
  const runs = usePoll<any[]>(`/runs?limit=10&v=${version}`, 20000);
  return (
    <div className="grid gap-4 xl:grid-cols-[22rem_1fr]">
      <div className="space-y-4">
        <Card title="Trade proposals">
          <ErrorNote error={error} />
          {data?.length === 0 && <p className="py-4 text-center text-sm text-muted">No proposals yet. Most analysis cycles correctly end without a trade.</p>}
          <ul className="max-h-[32rem] space-y-1 overflow-y-auto">
            {(data ?? []).map((p) => (
              <li key={p.id}>
                <button onClick={() => onSelect(p.id)} aria-current={selected === p.id}
                        className={`flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm hover:bg-raised ${selected === p.id ? "bg-raised" : ""}`}>
                  <span className="font-medium">{p.side} {p.symbol}</span>
                  <Badge tone={statusTone(p.status)}>{p.status}</Badge>
                  <span className="num ml-auto text-xs text-muted">{time(p.created_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        </Card>
        <Card title="Recent analysis cycles">
          <ul className="space-y-1 text-sm">
            {(runs.data ?? []).map((r) => (
              <li key={r.id} className="flex flex-wrap items-center gap-2">
                <span className="num text-ink2">#{r.id}</span><Badge tone={statusTone(r.status)}>{r.status}</Badge>
                <span className="num text-xs text-ink2">{r.summary?.candidates ?? 0} cand · {r.summary?.approved ?? 0} ok · {r.summary?.rejected ?? 0} rej · {r.summary?.no_trade ?? 0} none</span>
                <span className="num ml-auto text-xs text-muted">{time(r.created_at)}</span>
              </li>
            ))}
            {runs.data?.length === 0 && <p className="py-2 text-center text-muted">No cycles have run.</p>}
          </ul>
        </Card>
      </div>
      {selected ? <Detail key={selected} id={selected} /> : <Card><p className="py-10 text-center text-sm text-muted">Select a proposal to see the complete decision process.</p></Card>}
    </div>
  );
}
