import { CartesianGrid, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { money } from "../format";

export interface EquityPoint {
  t: string;
  equity: number;
}

/** Single series (the title names it, so no legend): 2px line, hairline grid, crosshair + tooltip, one y-axis. */
export function EquityChart({ points, baseline }: { points: EquityPoint[]; baseline: number }) {
  if (points.length < 2) {
    return <p className="flex h-64 items-center justify-center text-center text-sm text-muted">The equity curve appears after the trading engine has recorded at least two portfolio snapshots (one per minute).</p>;
  }
  const data = points.map((p) => ({ ts: new Date(p.t).getTime(), equity: p.equity }));
  const values = data.map((d) => d.equity).concat(baseline);
  const pad = Math.max((Math.max(...values) - Math.min(...values)) * 0.15, baseline * 0.002);
  const span = data[data.length - 1].ts - data[0].ts;
  const tick = (ts: number) => new Date(ts).toLocaleString(undefined, span > 36 * 3600e3 ? { month: "short", day: "numeric" } : { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="h-64 w-full" role="img" aria-label={`Portfolio equity over time, latest ${money(data[data.length - 1].equity)}`}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="#2c2c2a" vertical={false} />
          <XAxis dataKey="ts" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={tick} stroke="#383835" tick={{ fill: "#898781", fontSize: 11 }} tickLine={false} minTickGap={48} />
          <YAxis domain={[Math.min(...values) - pad, Math.max(...values) + pad]} tickFormatter={(v: number) => `$${Math.round(v).toLocaleString()}`} stroke="#383835"
                 tick={{ fill: "#898781", fontSize: 11 }} tickLine={false} axisLine={false} width={64} />
          <ReferenceLine y={baseline} stroke="#898781" strokeDasharray="4 4" label={{ value: "Starting capital", fill: "#898781", fontSize: 11, position: "insideTopLeft" }} />
          <Tooltip cursor={{ stroke: "#898781", strokeWidth: 1 }} contentStyle={{ background: "#232322", border: "1px solid #383835", borderRadius: 8, color: "#f0efec", fontSize: 12 }}
                   labelFormatter={(ts) => new Date(Number(ts)).toLocaleString()} formatter={(v) => [money(v), "Equity"]} />
          <Line type="monotone" dataKey="equity" stroke="#3987e5" strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: "#1a1a19", strokeWidth: 2 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
