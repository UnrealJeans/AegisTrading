import { useState } from "react";
import { api } from "../api";
import { Badge, Button, statusTone } from "./ui";

export interface ControlState {
  status: string;
  reason: string;
  liquidate: boolean;
}

/** Start / Pause / Resume / Emergency stop. Destructive or risk-increasing actions require a second confirmation. */
export function Controls({ control, onChange }: { control: ControlState; onChange: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<null | "emergency" | "resume">(null);
  const [liquidate, setLiquidate] = useState(false);
  const s = control.status;

  const call = async (path: string, body: unknown = {}) => {
    setBusy(true);
    setError(null);
    try {
      await api(`/control/${path}`, { method: "POST", body });
      setConfirm(null);
      onChange();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const needsConfirm = s === "SUSPENDED" || s === "EMERGENCY_STOP";

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={statusTone(s)}>{s.replace("_", " ")}</Badge>
        {s === "STOPPED" && <Button variant="primary" disabled={busy} onClick={() => call("start")}>Start</Button>}
        {s === "RUNNING" && <Button disabled={busy} onClick={() => call("pause")}>Pause</Button>}
        {(s === "PAUSED" || needsConfirm) && (
          <Button variant="primary" disabled={busy} onClick={() => (needsConfirm ? setConfirm("resume") : call("resume"))}>Resume</Button>
        )}
        {s !== "EMERGENCY_STOP" && <Button variant="danger" disabled={busy} onClick={() => setConfirm("emergency")}>Emergency stop</Button>}
      </div>
      {control.reason && s !== "RUNNING" && <p className="text-xs text-ink2">Reason: {control.reason}</p>}
      {error && <p role="alert" className="text-xs text-crit">{error}</p>}
      {confirm && (
        <div role="dialog" aria-modal="true" aria-label={confirm === "emergency" ? "Confirm emergency stop" : "Confirm resume"}
             className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-4 sm:items-center">
          <div className="w-full max-w-md rounded-xl border border-line bg-surface p-5">
            {confirm === "emergency" ? (
              <>
                <h3 className="text-base font-semibold">Emergency stop</h3>
                <p className="mt-2 text-sm text-ink2">Blocks all new trades immediately. Open positions keep their stop-loss and take-profit unless you also close them.</p>
                <label className="mt-3 flex items-center gap-2 text-sm">
                  <input type="checkbox" checked={liquidate} onChange={(e) => setLiquidate(e.target.checked)} className="size-4" />
                  Also close all open positions at market
                </label>
              </>
            ) : (
              <>
                <h3 className="text-base font-semibold">Resume trading?</h3>
                <p className="mt-2 text-sm text-ink2">Trading was halted: <span className="text-ink">{control.reason || s}</span>. Resuming allows new positions to be opened again.</p>
              </>
            )}
            <div className="mt-5 flex justify-end gap-2">
              <Button onClick={() => setConfirm(null)}>Cancel</Button>
              {confirm === "emergency" ? (
                <Button variant="danger" disabled={busy} onClick={() => call("emergency-stop", { liquidate, reason: "operator emergency stop" })}>Stop trading now</Button>
              ) : (
                <Button variant="primary" disabled={busy} onClick={() => call("resume", { confirm: true })}>Yes, resume</Button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
