import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { LiveClient, type SocketLike } from "../lib/live";

class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  constructor(public url: string) { FakeSocket.all.push(this); }
  send(d: string) { this.sent.push(d); }
  close(code = 1000) { this.onclose?.({ code } as CloseEvent); }
  open() { this.onopen?.(new Event("open")); }
  recv(obj: unknown) { this.onmessage?.({ data: JSON.stringify(obj) } as MessageEvent); }
}

describe("LiveClient", () => {
  beforeEach(() => { FakeSocket.all = []; vi.useFakeTimers(); });
  afterEach(() => vi.useRealTimers());

  const make = (token: string | null = "tok", refresh = vi.fn(async () => "tok2")) =>
    new LiveClient("ws://x/api/v1/ws/events", (u) => new FakeSocket(u), () => token, refresh);

  it("authenticates with the first frame (token never in the URL) and goes live", () => {
    const c = make();
    const statuses: string[] = [];
    c.onStatus((s) => statuses.push(s));
    c.start();
    const ws = FakeSocket.all[0];
    expect(ws.url).not.toContain("tok");
    ws.open();
    expect(JSON.parse(ws.sent[0])).toEqual({ type: "auth", token: "tok" });
    ws.recv({ type: "auth_ok" });
    expect(c.status).toBe("live");
    expect(statuses).toContain("connecting");
    c.stop();
  });

  it("delivers events and answers pings", () => {
    const c = make();
    const got: string[] = [];
    c.on((e) => got.push(e.type));
    c.start();
    const ws = FakeSocket.all[0];
    ws.open();
    ws.recv({ type: "auth_ok" });
    ws.recv({ type: "event", event: { id: "1", type: "incident.created", occurred_at: "", source: "t", data: {} } });
    ws.recv({ type: "ping" });
    expect(got).toEqual(["incident.created"]);
    expect(JSON.parse(ws.sent.at(-1)!)).toEqual({ type: "pong" });
    c.stop();
  });

  it("reconnects with backoff after a drop and resumes delivery", async () => {
    const c = make();
    const got: string[] = [];
    c.on((e) => got.push(e.type));
    c.start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].recv({ type: "auth_ok" });
    FakeSocket.all[0].close(1006);
    expect(c.status).toBe("reconnecting");
    await vi.advanceTimersByTimeAsync(1100);
    expect(FakeSocket.all).toHaveLength(2);
    const ws2 = FakeSocket.all[1];
    ws2.open();
    ws2.recv({ type: "auth_ok" });
    ws2.recv({ type: "event", event: { id: "2", type: "anomaly.detected", occurred_at: "", source: "t", data: {} } });
    expect(c.status).toBe("live");
    expect(got).toEqual(["anomaly.detected"]);
    c.stop();
  });

  it("refreshes the token before reconnecting after an auth close (4401)", async () => {
    const refresh = vi.fn(async () => "fresh");
    const c = make("tok", refresh);
    c.start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].close(4401);
    await vi.advanceTimersByTimeAsync(1100);
    expect(refresh).toHaveBeenCalledOnce();
    c.stop();
  });

  it("backoff grows exponentially and is capped at 30s", () => {
    const c = make();
    for (let i = 0; i < 12; i++) {
      const d = c.backoffMs(i);
      expect(d).toBeLessThanOrEqual(30_000);
      expect(d).toBeGreaterThanOrEqual(Math.min(30_000, 1000 * 2 ** i) / 2);
    }
  });

  it("does not reconnect after stop()", async () => {
    const c = make();
    c.start();
    c.stop();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(FakeSocket.all).toHaveLength(1);
    expect(c.status).toBe("offline");
  });
});
