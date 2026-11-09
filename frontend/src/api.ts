// Thin API client. The JWT lives in sessionStorage (cleared when the tab closes) and is sent as a Bearer header.
const TOKEN_KEY = "aegis.token";
type Listener = () => void;
const unauthorizedListeners = new Set<Listener>();

export const getToken = () => sessionStorage.getItem(TOKEN_KEY);
export const setToken = (t: string | null) => (t ? sessionStorage.setItem(TOKEN_KEY, t) : sessionStorage.removeItem(TOKEN_KEY));
export const onUnauthorized = (fn: Listener) => {
  unauthorizedListeners.add(fn);
  return () => void unauthorizedListeners.delete(fn);
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T = any>(path: string, init: { method?: string; body?: unknown } = {}): Promise<T> {
  const token = getToken();
  const res = await fetch(`/api${path}`, {
    method: init.method ?? "GET",
    headers: { ...(init.body !== undefined ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
  });
  if (res.status === 401 && path !== "/auth/login") {
    setToken(null);
    unauthorizedListeners.forEach((fn) => fn());
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : Array.isArray(j.detail) ? j.detail.map((d: any) => `${d.loc?.slice(-1)[0]}: ${d.msg}`).join("; ") : detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export async function login(username: string, password: string): Promise<void> {
  const r = await api<{ access_token: string }>("/auth/login", { method: "POST", body: { username, password } });
  setToken(r.access_token);
}
