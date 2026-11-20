import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AppRoutes } from "../App";
import { setToken } from "../lib/api";
import { AuthProvider } from "../lib/auth";
import { LiveProvider } from "../lib/liveContext";
import IncidentsPage from "../pages/Incidents";
import LogsPage from "../pages/Logs";

type Handler = (url: URL, init?: RequestInit) => { status?: number; body?: unknown } | undefined;

function mockFetch(handler: Handler) {
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const r = handler(url, init) ?? { status: 404, body: { error: { code: "not_found", message: "nf" } } };
    return new Response(r.status === 204 ? null : JSON.stringify(r.body ?? {}), { status: r.status ?? 200, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

const USER = (role: string) => ({ id: 1, username: role, role, is_active: true, created_at: "", last_login_at: null });
const INCIDENT = { id: 7, title: "Database connection pool exhaustion in payment-service", status: "open", severity: "critical", source: "auto",
  primary_service: "payment-service", services: ["payment-service"], opened_at: new Date().toISOString(), last_activity_at: new Date().toISOString(),
  acknowledged_at: null, resolved_at: null, resolution_note: null, recovery_verified_at: null };

function wrap(ui: React.ReactNode, path: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchInterval: false } } });
  return render(
    <QueryClientProvider client={qc}><AuthProvider><LiveProvider>
      <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
    </LiveProvider></AuthProvider></QueryClientProvider>);
}

class NoWS { close() {} send() {} }

beforeEach(() => { vi.stubGlobal("WebSocket", NoWS); setToken(null); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("auth & navigation", () => {
  it("redirects to login without a session and signs in", async () => {
    mockFetch((u) => {
      if (u.pathname === "/api/v1/auth/refresh") return { status: 401, body: { error: { code: "unauthorized", message: "no" } } };
      if (u.pathname === "/api/v1/auth/login") return { body: { access_token: "t", user: USER("sre"), expires_in: 900 } };
      if (u.pathname === "/api/v1/metrics/overview") return { body: { overall_health: "healthy", services_total: 5, services_reporting: 5,
        service_health: { "payment-service": "healthy" }, active_incidents: 0, active_incidents_by_severity: {}, current: { rps: 12 }, series: { rps: [] },
        recent_anomalies: [], generated_at: "" } };
      if (u.pathname === "/api/v1/metrics/services") return { body: { series: {} } };
      return undefined;
    });
    wrap(<AppRoutes />, "/");
    await screen.findByRole("form", { name: "sign in" });
    await userEvent.type(screen.getByLabelText("Username"), "sre");
    await userEvent.type(screen.getByLabelText("Password"), "Sre-Passw0rd-12345");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    await screen.findByRole("heading", { name: "Overview" });
    expect(await screen.findByText("12.0/s")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "primary" })).toBeInTheDocument();
  });

  it("shows a generic error on bad credentials", async () => {
    mockFetch((u) => {
      if (u.pathname === "/api/v1/auth/refresh") return { status: 401, body: {} };
      if (u.pathname === "/api/v1/auth/login") return { status: 401, body: { error: { code: "unauthorized", message: "invalid" } } };
      return undefined;
    });
    wrap(<AppRoutes />, "/login");
    await userEvent.type(await screen.findByLabelText("Username"), "x");
    await userEvent.type(screen.getByLabelText("Password"), "y");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid username or password");
  });
});

function sessionAs(role: string, extra: Handler): ReturnType<typeof mockFetch> {
  return mockFetch((u, init) => {
    if (u.pathname === "/api/v1/auth/refresh") return { body: { access_token: "t", user: USER(role) } };
    if (u.pathname === "/api/v1/auth/me") return { body: USER(role) };
    return extra(u, init);
  });
}

describe("incidents", () => {
  const detail = { ...INCIDENT, timeline: [{ id: 1, occurred_at: INCIDENT.opened_at, kind: "incident.opened", message: "opened automatically", actor: null, data: {} }],
    anomalies: [], investigations: [] };

  it("SRE can acknowledge; list, filters and timeline render", async () => {
    const f = sessionAs("sre", (u, init) => {
      if (u.pathname === "/api/v1/incidents") return { body: { items: [INCIDENT], total: 1, limit: 20, offset: 0 } };
      if (u.pathname === "/api/v1/incidents/7") return { body: detail };
      if (u.pathname === "/api/v1/incidents/7/acknowledge" && init?.method === "POST") return { body: { ...INCIDENT, status: "acknowledged" } };
      if (u.pathname === "/api/v1/rca/7") return { status: 404, body: { error: { code: "not_found", message: "none yet" } } };
      return undefined;
    });
    wrap(<Routes><Route path="/incidents/:id" element={<IncidentsPage />} /></Routes>, "/incidents/7");
    expect((await screen.findAllByText(INCIDENT.title)).length).toBeGreaterThan(0);
    expect(screen.getByText("opened automatically")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("severity"), "critical");
    await waitFor(() => expect(f.mock.calls.some(([u]) => String(u).includes("severity=critical"))).toBe(true));
    await userEvent.click(await screen.findByRole("button", { name: "Acknowledge" }));
    await waitFor(() => expect(f.mock.calls.some(([u, i]) => String(u).endsWith("/incidents/7/acknowledge") && i?.method === "POST")).toBe(true));
  });

  it("viewer sees read-only incident without state controls", async () => {
    sessionAs("viewer", (u) => {
      if (u.pathname === "/api/v1/incidents") return { body: { items: [INCIDENT], total: 1, limit: 20, offset: 0 } };
      if (u.pathname === "/api/v1/incidents/7") return { body: detail };
      if (u.pathname === "/api/v1/rca/7") return { status: 404, body: {} };
      return undefined;
    });
    wrap(<Routes><Route path="/incidents/:id" element={<IncidentsPage />} /></Routes>, "/incidents/7");
    await screen.findByText(/Read-only: your role cannot change incident state/);
    expect(screen.queryByRole("button", { name: "Acknowledge" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Declare incident/ })).toBeNull();
  });

  it("shows an error state with retry when the API fails", async () => {
    sessionAs("sre", (u) => (u.pathname === "/api/v1/incidents" ? { status: 503, body: { error: { code: "dependency_unavailable", message: "db down" } } } : undefined));
    wrap(<Routes><Route path="/incidents" element={<IncidentsPage />} /></Routes>, "/incidents");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/db down/)).toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    sessionAs("sre", (u) => (u.pathname === "/api/v1/incidents" ? { body: { items: [], total: 0, limit: 20, offset: 0 } } : undefined));
    wrap(<Routes><Route path="/incidents" element={<IncidentsPage />} /></Routes>, "/incidents");
    expect(await screen.findByText("No incidents")).toBeInTheDocument();
  });
});

describe("logs explorer", () => {
  it("passes filters to the API and links trace ids", async () => {
    const tid = "4bf92f3577b34da6a3ce929d0e0e4736";
    const f = sessionAs("viewer", (u) => (u.pathname === "/api/v1/logs" ? { body: { query: "{...}", items: [
      { timestamp: new Date().toISOString(), service: "payment-service", level: "ERROR", message: "database connection pool exhausted",
        trace_id: tid, span_id: "d".repeat(16), request_id: "r", attributes: {} },
      { timestamp: new Date().toISOString(), service: "order-service", level: "WARNING", message: "no ctx", trace_id: null, span_id: null, request_id: null, attributes: {} },
    ] } } : undefined));
    wrap(<Routes><Route path="/logs" element={<LogsPage />} /></Routes>, "/logs?service=payment-service");
    await screen.findByText("database connection pool exhausted");
    expect(screen.getByTitle("Open trace")).toHaveAttribute("href", `/traces/${tid}`);
    expect(screen.getByTitle("no trace context on this record")).toBeInTheDocument();
    const lastUrl = String(f.mock.calls.filter(([u]) => String(u).includes("/logs")).at(-1)![0]);
    expect(lastUrl).toContain("service=payment-service");
    expect(lastUrl).toContain("level=ERROR%2CWARNING");
    await userEvent.click(screen.getByRole("button", { name: "INFO" }));
    await waitFor(() => expect(String(f.mock.calls.at(-1)![0])).toContain("level=ERROR%2CWARNING%2CINFO"));
  });
});
