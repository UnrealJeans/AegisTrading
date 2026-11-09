import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App, { ModeBanner } from "./App";
import { setToken } from "./api";
import { Controls } from "./components/Controls";
import { describe as describeEvent } from "./components/Feed";
import { Meter } from "./components/ui";
import { ago, money, pct, pnlTone, price } from "./format";

const overview = (status = "RUNNING", mode = "paper") => ({
  system: {
    mode, is_paper: mode === "paper", control: { status, reason: status === "SUSPENDED" ? "daily loss 3.5% reached the 3% limit" : "", liquidate: false },
    services: { worker: { online: true }, orchestrator: { online: true }, "trading-engine": { online: false } },
    market_data: { healthy: true, reason: "" }, orchestrator: null,
  },
  portfolio: { equity: "1012.50", cash: "800", total_pnl: "12.5", starting_capital: "1000", daily_pnl: "-4.2", daily_loss_pct: "0.42", drawdown_pct: "0.5",
               exposure_pct: "21.2", unrealized_pnl: "3.1", realized_pnl: "9.4" },
  stats: { closed_trades: 4, wins: 3, losses: 1, win_rate: 75, open_positions: 1, max_drawdown_pct: "1.2" },
});
const agents = [
  { name: "scout", title: "Scout", role: "Finds opportunities", tools: ["market_scanner"], provider: "xai", model: "grok-test", enabled: true, online: true, offline_reason: null,
    state: "idle", current_task: null, last_execution: { status: "SUCCESS", confidence: 0.8, created_at: new Date().toISOString(), finished_at: new Date().toISOString(), output: { picks: [] }, symbol: null } },
  { name: "analyst", title: "Analyst", role: "Builds proposals", tools: ["agent_reports"], provider: "openai", model: "gpt-test", enabled: true, online: false,
    offline_reason: "no API key for provider 'openai'", state: "idle", current_task: null, last_execution: null },
];

let calls: { url: string; method: string; body: any }[] = [];
function mockApi(routes: Record<string, (body: any) => { status?: number; json: unknown }>) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    const path = url.replace(/^\/api/, "").split("?")[0];
    const body = init.body ? JSON.parse(init.body as string) : undefined;
    calls.push({ url: path, method: init.method ?? "GET", body });
    const key = `${init.method ?? "GET"} ${path}`;
    const r = (routes[key] ?? routes[path])?.(body) ?? { json: [] };
    return { ok: (r.status ?? 200) < 400, status: r.status ?? 200, statusText: "err", json: async () => r.json } as Response;
  }));
}

beforeEach(() => vi.unstubAllGlobals());

describe("formatting", () => {
  it("formats money, percentages and prices without losing sign", () => {
    expect(money("1234.5")).toBe("$1,234.50");
    expect(money(-3.456, { sign: true })).toBe("−$3.46");
    expect(money(2, { sign: true })).toBe("+$2.00");
    expect(money(null)).toBe("—");
    expect(pct("3.456")).toBe("3.46%");
    expect(price("0.00001234")).toBe("0.00001234");
    expect(price(64250.129)).toBe("64,250.13");
    expect(pnlTone(-1)).toBe("text-crit");
    expect(pnlTone(1)).toBe("text-good");
    expect(ago(new Date(Date.now() - 125000).toISOString())).toBe("2m ago");
    expect(ago(null)).toBe("never");
  });
  it("describes live events for the activity feed", () => {
    expect(describeEvent({ type: "agent_message", ts: "", data: { from: "analyst", to: "skeptic", symbol: "BTC/USDT", summary: "BUY BTC" } })).toEqual({ who: "analyst → skeptic", text: "BTC/USDT · BUY BTC" });
    expect(describeEvent({ type: "decision", ts: "", data: { symbol: "ETH/USDT", stage: "skeptic", status: "rejected", reasons: ["pump"] } })?.status).toBe("rejected");
    expect(describeEvent({ type: "prices", ts: "", data: {} })).toBeNull();
  });
});

describe("mode banner", () => {
  it("clearly distinguishes paper, testnet and live", () => {
    const { rerender } = render(<ModeBanner mode="paper" />);
    expect(screen.getByRole("status")).toHaveTextContent(/PAPER TRADING/);
    expect(screen.getByRole("status")).toHaveTextContent(/no real funds/);
    rerender(<ModeBanner mode="testnet" />);
    expect(screen.getByRole("status")).toHaveTextContent(/TESTNET/);
    rerender(<ModeBanner mode="live" />);
    expect(screen.getByRole("status")).toHaveTextContent(/LIVE TRADING — REAL MONEY AT RISK/);
  });
});

describe("controls", () => {
  it("offers only the valid actions for each status", () => {
    mockApi({});
    const { rerender } = render(<Controls control={{ status: "STOPPED", reason: "", liquidate: false }} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Start" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    rerender(<Controls control={{ status: "RUNNING", reason: "", liquidate: false }} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start" })).toBeNull();
    rerender(<Controls control={{ status: "EMERGENCY_STOP", reason: "x", liquidate: false }} onChange={() => {}} />);
    expect(screen.queryByRole("button", { name: "Emergency stop" })).toBeNull();
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
  });

  it("requires confirmation for emergency stop and sends the liquidate choice", async () => {
    mockApi({ "POST /control/emergency-stop": () => ({ json: { status: "EMERGENCY_STOP" } }) });
    const onChange = vi.fn();
    render(<Controls control={{ status: "RUNNING", reason: "", liquidate: false }} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Emergency stop" }));
    expect(calls).toHaveLength(0); // nothing sent before confirmation
    const dialog = screen.getByRole("dialog", { name: "Confirm emergency stop" });
    await userEvent.click(within(dialog).getByRole("checkbox"));
    await userEvent.click(within(dialog).getByRole("button", { name: "Stop trading now" }));
    expect(calls[0]).toMatchObject({ url: "/control/emergency-stop", method: "POST", body: { liquidate: true } });
    expect(onChange).toHaveBeenCalled();
  });

  it("requires confirmation to resume after an automatic suspension and shows the reason", async () => {
    mockApi({ "POST /control/resume": () => ({ json: { status: "RUNNING" } }) });
    render(<Controls control={{ status: "SUSPENDED", reason: "daily loss limit", liquidate: false }} onChange={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Resume" }));
    expect(calls).toHaveLength(0);
    expect(screen.getByRole("dialog")).toHaveTextContent("daily loss limit");
    await userEvent.click(screen.getByRole("button", { name: "Yes, resume" }));
    expect(calls[0].body).toEqual({ confirm: true });
  });

  it("surfaces server refusals", async () => {
    mockApi({ "POST /control/start": () => ({ status: 409, json: { detail: "trading mode is not runnable" } }) });
    render(<Controls control={{ status: "STOPPED", reason: "", liquidate: false }} onChange={() => {}} />);
    await userEvent.click(screen.getByRole("button", { name: "Start" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("trading mode is not runnable");
  });
});

describe("meter", () => {
  it("flags a reached limit in text, not colour alone", () => {
    render(<Meter label="Daily loss" value={30} limit={30} format={(v) => `$${v}`} />);
    expect(screen.getByText(/LIMIT REACHED/)).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Daily loss" })).toHaveAttribute("aria-valuenow", "30");
  });
});

describe("app", () => {
  it("shows the login form, reports bad credentials and never stores a token on failure", async () => {
    mockApi({ "POST /auth/login": () => ({ status: 401, json: { detail: "invalid credentials" } }) });
    render(<App />);
    await userEvent.type(screen.getByLabelText("Password"), "wrong");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong username or password.");
    expect(sessionStorage.getItem("aegis.token")).toBeNull();
  });

  it("signs in, sends the bearer token and renders overview, mode and service health", async () => {
    mockApi({
      "POST /auth/login": (b) => (b.password === "pw" ? { json: { access_token: "tok-1" } } : { status: 401, json: {} }),
      "/overview": () => ({ json: overview() }),
    });
    render(<App />);
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("$1,012.50")).toBeInTheDocument();
    expect(screen.getByText(/PAPER TRADING/)).toBeInTheDocument();
    expect(screen.getByText("trading-engine down")).toBeInTheDocument();
    expect(screen.getByText("75.0%")).toBeInTheDocument();
    expect(screen.getByText("−$4.20")).toBeInTheDocument();
    const fetchMock = vi.mocked(fetch);
    const authed = fetchMock.mock.calls.find(([u]) => String(u).startsWith("/api/overview"))!;
    expect((authed[1] as any).headers.Authorization).toBe("Bearer tok-1");
  });

  it("agent monitor shows honest online/offline state with the reason", async () => {
    setToken("tok");
    mockApi({ "/overview": () => ({ json: overview() }), "/agents": () => ({ json: agents }) });
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Agent Monitor" }));
    expect(await screen.findByText("Scout")).toBeInTheDocument();
    expect(screen.getByText("Online")).toBeInTheDocument();
    expect(screen.getByText("Offline")).toBeInTheDocument();
    expect(screen.getByText(/no API key for provider 'openai'/)).toBeInTheDocument();
    expect(screen.getByText("xai · grok-test")).toBeInTheDocument();
  });

  it("returns to the login screen when the session expires", async () => {
    setToken("expired");
    mockApi({ "/overview": () => ({ status: 401, json: { detail: "not authenticated" } }) });
    render(<App />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument());
    expect(sessionStorage.getItem("aegis.token")).toBeNull();
  });
});
