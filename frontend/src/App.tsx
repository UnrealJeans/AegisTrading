import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken, onUnauthorized, setToken } from "./api";
import { Controls } from "./components/Controls";
import { Badge, Button, ErrorNote } from "./components/ui";
import { useLive, usePoll, type LiveEvent } from "./hooks";
import { Agents } from "./pages/Agents";
import { Analysis } from "./pages/Analysis";
import { Login } from "./pages/Login";
import { Overview } from "./pages/Overview";
import { Risk } from "./pages/Risk";
import { Settings } from "./pages/Settings";
import { Trading } from "./pages/Trading";

const TABS = ["Overview", "Agents", "Trading", "Analysis", "Risk", "Settings"] as const;
type Tab = (typeof TABS)[number];
const REFRESH_ON = new Set(["execution", "decision", "control", "risk_event", "cycle", "agent_execution"]);

export function ModeBanner({ mode }: { mode: string }) {
  if (mode === "live") {
    return <div role="status" className="bg-crit px-4 py-1.5 text-center text-sm font-semibold text-white">■ LIVE TRADING — REAL MONEY AT RISK</div>;
  }
  if (mode === "testnet") {
    return <div role="status" className="bg-warn px-4 py-1.5 text-center text-sm font-semibold text-black">▲ TESTNET — real orders on Binance Spot Testnet, no real funds</div>;
  }
  return <div role="status" className="bg-series/25 px-4 py-1.5 text-center text-sm font-medium text-ink">● PAPER TRADING — real market data, simulated orders, no real funds</div>;
}

function Shell({ onLogout }: { onLogout: () => void }) {
  const [tab, setTab] = useState<Tab>(() => (TABS.find((t) => t.toLowerCase() === location.hash.slice(1)) ?? "Overview"));
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [version, setVersion] = useState(0);
  const [proposal, setProposal] = useState<number | null>(null);
  const overview = usePoll<any>(`/overview?v=${version}`, 10000);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    api<LiveEvent[]>("/activity?limit=100").then(setEvents).catch(() => undefined);
  }, []);
  useEffect(() => {
    history.replaceState(null, "", `#${tab.toLowerCase()}`);
  }, [tab]);

  const onEvent = useCallback((e: LiveEvent) => {
    const silent = e.type === "prices" || e.type === "portfolio" || e.type === "agent_status";
    if (!silent) setEvents((prev) => [e, ...prev].slice(0, 300));
    if (REFRESH_ON.has(e.type) || e.type === "portfolio") {
      if (pending.current) return; // coalesce bursts into one refresh
      pending.current = setTimeout(() => {
        pending.current = null;
        setVersion((v) => v + 1);
      }, 800);
    }
  }, []);
  const connected = useLive(onEvent, true);
  const sys = overview.data?.system;

  return (
    <div className="flex min-h-full flex-col">
      {sys && <ModeBanner mode={sys.mode} />}
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-start justify-between gap-3 px-4 py-3">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold">Aegis Trading</h1>
              <Badge tone={connected ? "good" : "warn"}>{connected ? "Live" : "Reconnecting"}</Badge>
            </div>
            {sys && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                {Object.entries<any>(sys.services).map(([name, s]) => <Badge key={name} tone={s.online ? "good" : "crit"}>{name} {s.online ? "up" : "down"}</Badge>)}
                <Badge tone={sys.market_data.healthy ? "good" : "crit"}>market data {sys.market_data.healthy ? "fresh" : "stale"}</Badge>
              </div>
            )}
          </div>
          <div className="flex flex-wrap items-start gap-3">
            {sys && <Controls control={sys.control} onChange={() => setVersion((v) => v + 1)} />}
            <Button variant="ghost" onClick={onLogout}>Sign out</Button>
          </div>
        </div>
        <nav aria-label="Sections" className="mx-auto flex max-w-[1600px] gap-1 overflow-x-auto px-2">
          {TABS.map((t) => (
            <button key={t} onClick={() => setTab(t)} aria-current={tab === t ? "page" : undefined}
                    className={`whitespace-nowrap border-b-2 px-3 py-2.5 text-sm font-medium ${tab === t ? "border-series text-ink" : "border-transparent text-ink2 hover:text-ink"}`}>
              {t === "Analysis" ? "Trade Analysis" : t === "Agents" ? "Agent Monitor" : t === "Risk" ? "Risk Management" : t}
            </button>
          ))}
        </nav>
      </header>
      <main className="mx-auto w-full max-w-[1600px] flex-1 p-4">
        <ErrorNote error={overview.error} />
        {sys && !sys.market_data.healthy && <p className="mb-3 rounded-lg border border-warn/50 bg-warn/10 px-3 py-2 text-sm">▲ Market data is not healthy: {sys.market_data.reason}. No new trades will be opened until it recovers.</p>}
        {tab === "Overview" && (overview.data ? <Overview overview={overview.data} events={events} version={version} /> : <p className="text-sm text-muted">Loading…</p>)}
        {tab === "Agents" && <Agents events={events} version={version} />}
        {tab === "Trading" && <Trading version={version} onOpenProposal={(id) => { setProposal(id); setTab("Analysis"); }} />}
        {tab === "Analysis" && <Analysis version={version} selected={proposal} onSelect={setProposal} />}
        {tab === "Risk" && <Risk version={version} />}
        {tab === "Settings" && <Settings />}
      </main>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(() => Boolean(getToken()));
  useEffect(() => onUnauthorized(() => setAuthed(false)), []);
  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  return <Shell onLogout={() => { setToken(null); setAuthed(false); }} />;
}
