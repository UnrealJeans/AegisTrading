import { useCallback, useEffect, useRef, useState } from "react";
import { api, getToken } from "./api";

export interface LiveEvent {
  type: string;
  ts: string;
  data: Record<string, any>;
}

/** Poll an endpoint; `refresh()` forces an immediate reload (used when a WebSocket event arrives). */
export function usePoll<T>(path: string | null, ms = 10000): { data: T | null; error: string | null; refresh: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) return;
    let alive = true;
    const load = () =>
      api<T>(path)
        .then((d) => alive && (setData(d), setError(null)))
        .catch((e) => alive && setError(e.message));
    load();
    const id = setInterval(load, ms);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [path, ms, tick]);
  return { data, error, refresh: useCallback(() => setTick((t) => t + 1), []) };
}

/** Authenticated WebSocket with auto-reconnect. The token is sent in the first frame, never in the URL. */
export function useLive(onEvent: (e: LiveEvent) => void, enabled: boolean): boolean {
  const [connected, setConnected] = useState(false);
  const handler = useRef(onEvent);
  handler.current = onEvent;
  useEffect(() => {
    if (!enabled || typeof WebSocket === "undefined") return;
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 1000;
    let timer: ReturnType<typeof setTimeout>;
    let ping: ReturnType<typeof setInterval>;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/api/ws`);
      ws.onopen = () => {
        ws!.send(JSON.stringify({ type: "auth", token: getToken() }));
        ping = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 25000);
      };
      ws.onmessage = (m) => {
        try {
          const e = JSON.parse(m.data) as LiveEvent;
          if (e.type === "hello") {
            setConnected(true);
            retry = 1000;
          } else handler.current(e);
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        setConnected(false);
        clearInterval(ping);
        if (!closed) timer = setTimeout(connect, (retry = Math.min(retry * 2, 15000)));
      };
    };
    connect();
    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(ping);
      ws?.close();
    };
  }, [enabled]);
  return connected;
}
