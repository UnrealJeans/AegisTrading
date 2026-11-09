export const num = (v: unknown): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

export function money(v: unknown, opts: { sign?: boolean } = {}): string {
  const n = num(v);
  if (n === null) return "—";
  const s = Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${n < 0 ? "−" : opts.sign && n > 0 ? "+" : ""}$${s}`;
}

export function price(v: unknown): string {
  const n = num(v);
  if (n === null) return "—";
  const digits = n >= 1000 ? 2 : n >= 1 ? 4 : n >= 0.01 ? 6 : 8;
  return n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits });
}

export function qty(v: unknown): string {
  const n = num(v);
  return n === null ? "—" : n.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

export function pct(v: unknown, digits = 2, opts: { sign?: boolean } = {}): string {
  const n = num(v);
  if (n === null) return "—";
  return `${n < 0 ? "−" : opts.sign && n > 0 ? "+" : ""}${Math.abs(n).toFixed(digits)}%`;
}

export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export const time = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "—";

/** Tone for a signed P&L number: text stays readable ink; colour is reinforced by the explicit sign. */
export const pnlTone = (v: unknown) => {
  const n = num(v);
  return n === null || n === 0 ? "text-ink2" : n > 0 ? "text-good" : "text-crit";
};
