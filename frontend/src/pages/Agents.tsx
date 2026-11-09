import { useState } from "react";
import { Feed } from "../components/Feed";
import { Badge, Button, Card, ErrorNote, Json, statusTone } from "../components/ui";
import { ago, pct } from "../format";
import { usePoll, type LiveEvent } from "../hooks";

export function Agents({ events, version }: { events: LiveEvent[]; version: number }) {
  const { data, error } = usePoll<any[]>(`/agents?v=${version}`, 8000);
  const [selected, setSelected] = useState<string | null>(null);
  const history = usePoll<any[]>(selected ? `/agents/${selected}/executions?limit=25&v=${version}` : null, 15000);
  const agentEvents = events.filter((e) => ["agent_message", "agent_execution", "decision", "cycle"].includes(e.type));
  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(data ?? []).map((a) => {
          const last = a.last_execution;
          return (
            <Card key={a.name} title={a.title}
                  right={<Badge tone={a.online ? (a.state === "working" ? "info" : "good") : "crit"}>{a.online ? (a.state === "working" ? "Working" : "Online") : "Offline"}</Badge>}>
              <p className="text-sm text-ink2">{a.role}</p>
              {!a.online && <p className="mt-2 rounded-md bg-crit/15 px-2 py-1 text-xs text-ink">Offline: {a.offline_reason}</p>}
              <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
                <dt className="text-muted">Model</dt><dd className="truncate">{a.provider} · {a.model || "not set"}</dd>
                <dt className="text-muted">Current task</dt><dd>{a.current_task ?? "Idle"}</dd>
                <dt className="text-muted">Last run</dt>
                <dd className="flex flex-wrap items-center gap-2">{last ? <>{ago(last.finished_at ?? last.created_at)} <Badge tone={statusTone(last.status)}>{last.status}</Badge></> : "never"}</dd>
                <dt className="text-muted">Confidence</dt><dd className="num">{last?.confidence != null ? pct(last.confidence * 100, 0) : "—"}</dd>
                <dt className="text-muted">Data tools</dt><dd className="text-ink2">{a.tools.join(", ")}</dd>
              </dl>
              {last?.output && (
                <details className="mt-3">
                  <summary className="cursor-pointer text-sm text-ink2">Recent output{last.symbol ? ` · ${last.symbol}` : ""}</summary>
                  <div className="mt-2"><Json value={last.output} /></div>
                </details>
              )}
              {last?.error && <p className="mt-2 break-words text-xs text-ink2">Note: {last.error}</p>}
              <Button variant="ghost" className="mt-2 -ml-3" onClick={() => setSelected(selected === a.name ? null : a.name)}>
                {selected === a.name ? "Hide history" : "Execution history"}
              </Button>
            </Card>
          );
        })}
      </div>
      {selected && (
        <Card title={`Execution history · ${selected.replace("_", " ")}`}>
          <ErrorNote error={history.error} />
          <ul className="space-y-2">
            {(history.data ?? []).map((e) => (
              <li key={e.id} className="rounded-lg bg-raised px-3 py-2 text-sm">
                <details>
                  <summary className="flex cursor-pointer flex-wrap items-center gap-2">
                    <Badge tone={statusTone(e.status)}>{e.status}</Badge>
                    <span>{e.symbol ?? "market"}</span>
                    <span className="num text-ink2">{e.llm_calls} LLM call{e.llm_calls === 1 ? "" : "s"} · {e.prompt_tokens + e.completion_tokens} tokens · {e.duration_ms} ms</span>
                    <span className="num ml-auto text-xs text-muted">{ago(e.created_at)}</span>
                  </summary>
                  {e.error && <p className="mt-2 text-xs text-ink2">{e.error}</p>}
                  {e.output && <div className="mt-2"><Json value={e.output} /></div>}
                </details>
              </li>
            ))}
            {history.data?.length === 0 && <p className="py-4 text-center text-sm text-muted">This agent has not run yet.</p>}
          </ul>
        </Card>
      )}
      <Card title="Agent communication and decisions (live)">
        <Feed events={agentEvents} />
      </Card>
    </div>
  );
}
