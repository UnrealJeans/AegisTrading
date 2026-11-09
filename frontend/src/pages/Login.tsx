import { useState } from "react";
import { login } from "../api";
import { Button } from "../components/ui";

export function Login({ onDone }: { onDone: () => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      onDone();
    } catch (err: any) {
      setError(err.status === 401 ? "Wrong username or password." : err.status === 429 ? "Too many attempts. Wait a minute and try again." : `Sign-in failed: ${err.message}`);
    } finally {
      setBusy(false);
    }
  };
  const cls = "mt-1 w-full rounded-lg border border-axis bg-page px-3 py-2.5 text-base text-ink";
  return (
    <main className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-xl border border-line bg-surface p-6">
        <h1 className="text-xl font-semibold">Aegis Trading</h1>
        <p className="mt-1 text-sm text-ink2">Sign in to the operator dashboard.</p>
        <label className="mt-5 block text-sm text-ink2">Username
          <input className={cls} autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <label className="mt-3 block text-sm text-ink2">Password
          <input className={cls} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p role="alert" className="mt-3 text-sm text-crit">{error}</p>}
        <Button variant="primary" type="submit" disabled={busy} className="mt-5 w-full">{busy ? "Signing in…" : "Sign in"}</Button>
      </form>
    </main>
  );
}
