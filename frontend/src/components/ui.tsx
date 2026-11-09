import type { ButtonHTMLAttributes, ReactNode } from "react";

export function Card({ title, right, children, className = "" }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section className={`rounded-xl border border-line bg-surface ${className}`}>
      {(title || right) && (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
          <h2 className="text-sm font-semibold text-ink">{title}</h2>
          {right}
        </header>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub, tone = "text-ink" }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <div className="text-xs text-muted">{label}</div>
      <div className={`num mt-1 text-xl font-semibold ${tone}`}>{value}</div>
      {sub && <div className="num mt-0.5 text-xs text-ink2">{sub}</div>}
    </div>
  );
}

type Tone = "good" | "warn" | "serious" | "crit" | "neutral" | "info";
const TONES: Record<Tone, string> = {
  good: "border-good/50 text-ink bg-good/15",
  warn: "border-warn/50 text-ink bg-warn/15",
  serious: "border-serious/50 text-ink bg-serious/15",
  crit: "border-crit/60 text-ink bg-crit/20",
  neutral: "border-axis text-ink2 bg-raised",
  info: "border-series/50 text-ink bg-series/15",
};
const ICONS: Record<Tone, string> = { good: "●", warn: "▲", serious: "◆", crit: "■", neutral: "○", info: "●" };
const ICON_COLOR: Record<Tone, string> = { good: "text-good", warn: "text-warn", serious: "text-serious", crit: "text-crit", neutral: "text-muted", info: "text-series" };

/** Status is never colour-alone: every badge carries a shape icon and a text label. */
export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-md border px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      <span aria-hidden className={`text-[9px] ${ICON_COLOR[tone]}`}>{ICONS[tone]}</span>
      {children}
    </span>
  );
}

export function statusTone(status: string | null | undefined): Tone {
  switch (status) {
    case "RUNNING": case "SUCCESS": case "FILLED": case "APPROVED": case "EXECUTED": case "COMPLETED": case "OPEN": case "approved": case "APPROVE":
      return "good";
    case "PAUSED": case "PENDING": case "EXECUTING": case "SUBMITTED": case "SKIPPED": case "WARNING": case "CLOSING": case "REDUCE": case "skipped":
      return "warn";
    case "SUSPENDED": case "TIMEOUT": case "EXPIRED": case "ABORTED":
      return "serious";
    case "EMERGENCY_STOP": case "ERROR": case "FAILED": case "REJECTED": case "REJECTED_AT_EXECUTION": case "CRITICAL": case "rejected": case "REJECT": case "error":
      return "crit";
    default:
      return "neutral";
  }
}

export function Button({ variant = "default", className = "", ...p }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: "default" | "primary" | "danger" | "ghost" }) {
  const v = {
    default: "border-axis bg-raised text-ink hover:bg-axis",
    primary: "border-series bg-series text-white hover:brightness-110",
    danger: "border-crit bg-crit text-white hover:brightness-110",
    ghost: "border-transparent text-ink2 hover:bg-raised",
  }[variant];
  return <button {...p} className={`min-h-9 rounded-lg border px-3 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-40 ${v} ${className}`} />;
}

/** Usage meter against a limit. One hue for the fill; the state is spelled out in the label. */
export function Meter({ label, value, limit, format }: { label: string; value: number; limit: number; format: (n: number) => string }) {
  const ratio = limit > 0 ? Math.min(1, Math.max(0, value / limit)) : 0;
  const tone = ratio >= 1 ? "bg-crit" : ratio >= 0.8 ? "bg-serious" : "bg-series";
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2 text-sm">
        <span className="text-ink2">{label}</span>
        <span className="num text-ink">
          {format(value)} <span className="text-muted">/ {format(limit)}</span>
          {ratio >= 1 && <span className="ml-2 text-xs font-semibold text-crit">■ LIMIT REACHED</span>}
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-raised" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={limit} aria-valuenow={value}>
        <div className={`h-full rounded-full ${tone}`} style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

export function Table({ head, rows, empty }: { head: ReactNode[]; rows: ReactNode[][]; empty: string }) {
  if (!rows.length) return <p className="py-6 text-center text-sm text-muted">{empty}</p>;
  return (
    <div className="-mx-4 overflow-x-auto px-4">
      <table className="w-full min-w-max text-left text-sm">
        <thead>
          <tr className="text-xs text-muted">
            {head.map((h, i) => (
              <th key={i} className="whitespace-nowrap px-2 pb-2 font-medium first:pl-0">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-line">
              {r.map((c, j) => (
                <td key={j} className="num whitespace-nowrap px-2 py-2 first:pl-0">{c}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export const ErrorNote = ({ error }: { error: string | null }) =>
  error ? <p role="alert" className="mb-3 rounded-lg border border-crit/50 bg-crit/15 px-3 py-2 text-sm text-ink">Could not load data: {error}</p> : null;

export function Json({ value }: { value: unknown }) {
  return <pre className="max-h-80 overflow-auto rounded-lg bg-page p-3 text-xs leading-relaxed text-ink2">{JSON.stringify(value, null, 2)}</pre>;
}
