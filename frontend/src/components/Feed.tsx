import type { LiveEvent } from "../hooks";
import { price, qty, time } from "../format";
import { Badge, statusTone } from "./ui";

export function describe(e: LiveEvent): { who: string; text: string; status?: string } | null {
  const d = e.data;
  switch (e.type) {
    case "agent_message":
      return { who: `${d.from} → ${d.to}`, text: `${d.symbol ? d.symbol + " · " : ""}${d.summary}` };
    case "agent_execution":
      return { who: d.agent, text: `${d.symbol ?? "market"} · ${d.llm_calls} LLM call${d.llm_calls === 1 ? "" : "s"} · ${d.duration_ms} ms${d.error ? " · " + d.error : ""}`, status: d.status };
    case "decision":
      return { who: "decision", text: `${d.symbol} at ${d.stage}${d.reasons?.length ? " · " + d.reasons[0] : ""}`, status: d.status };
    case "execution":
      return { who: "trading engine", text: `${d.purpose ?? ""} ${d.symbol ?? ""} ${d.quantity ? `qty ${qty(d.quantity)} @ ${price(d.price)}` : ""} ${d.reason ?? ""} ${d.error ?? d.reasons?.[0] ?? ""}`.trim(), status: d.status };
    case "risk_event":
      return { who: "risk", text: d.message, status: d.severity };
    case "system_event":
      return { who: d.service, text: d.message, status: d.severity === "INFO" ? undefined : d.severity };
    case "control":
      return { who: "control", text: d.reason || "status changed", status: d.status };
    case "cycle":
      return { who: "orchestrator", text: d.status === "started" ? `cycle ${d.run_id} started (${d.trigger})` : `cycle ${d.run_id}: ${d.candidates} candidates, ${d.approved} approved, ${d.rejected} rejected, ${d.no_trade} no-trade`, status: d.status === "started" ? undefined : d.status };
    default:
      return null;
  }
}

export function Feed({ events, limit = 60 }: { events: LiveEvent[]; limit?: number }) {
  const items = events.map((e) => ({ e, d: describe(e) })).filter((x) => x.d).slice(0, limit);
  if (!items.length) return <p className="py-6 text-center text-sm text-muted">No activity yet. Agent messages and decisions appear here in real time.</p>;
  return (
    <ol className="max-h-[28rem] space-y-2 overflow-y-auto pr-1" aria-label="Live activity">
      {items.map(({ e, d }, i) => (
        <li key={`${e.ts}-${i}`} className="rounded-lg bg-raised px-3 py-2 text-sm">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <span className="font-medium capitalize text-ink">{d!.who.replace(/_/g, " ")}</span>
            {d!.status && <Badge tone={statusTone(d!.status)}>{d!.status}</Badge>}
            <span className="num ml-auto text-xs text-muted">{time(e.ts)}</span>
          </div>
          <p className="mt-0.5 break-words text-ink2">{d!.text}</p>
        </li>
      ))}
    </ol>
  );
}
