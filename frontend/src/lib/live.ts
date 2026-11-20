/**
 * Real-time event client for /api/v1/ws/events.
 *  - Authenticates with the in-memory access token as the first frame (never in the URL).
 *  - Reconnects with exponential backoff + jitter (1 s .. 30 s).
 *  - On close 4401 (expired/invalid token) it refreshes the token before reconnecting.
 *  - Detects silent connections: if no frame arrives for 45 s (server pings every 20 s) it reconnects.
 */
import { getToken, refreshToken } from "./api";
import type { LiveEvent } from "./types";

export type LiveStatus = "connecting" | "live" | "reconnecting" | "offline";

type Listener = (e: LiveEvent) => void;
type StatusListener = (s: LiveStatus) => void;

export interface SocketLike {
  readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  send(data: string): void;
  close(code?: number): void;
}

export class LiveClient {
  private ws: SocketLike | null = null;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setInterval> | null = null;
  private lastFrame = 0;
  private stopped = true;
  private listeners = new Set<Listener>();
  private statusListeners = new Set<StatusListener>();
  status: LiveStatus = "offline";

  constructor(
    private url: string,
    private factory: (url: string) => SocketLike = (u) => new WebSocket(u) as unknown as SocketLike,
    private tokenFn: () => string | null = getToken,
    private refreshFn: () => Promise<string | null> = refreshToken,
  ) {}

  on(l: Listener): () => void {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  }

  onStatus(l: StatusListener): () => void {
    this.statusListeners.add(l);
    l(this.status);
    return () => this.statusListeners.delete(l);
  }

  private setStatus(s: LiveStatus) {
    this.status = s;
    this.statusListeners.forEach((l) => l(s));
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
    this.watchdog = setInterval(() => {
      if (this.status === "live" && Date.now() - this.lastFrame > 45_000) this.ws?.close(4000);
    }, 5_000);
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    if (this.watchdog) clearInterval(this.watchdog);
    this.ws?.close(1000);
    this.ws = null;
    this.setStatus("offline");
  }

  backoffMs(attempt: number): number {
    const base = Math.min(30_000, 1000 * 2 ** attempt);
    return base / 2 + Math.random() * (base / 2);
  }

  private connect(): void {
    const token = this.tokenFn();
    if (!token) {
      this.scheduleReconnect(true);
      return;
    }
    this.setStatus(this.attempt === 0 ? "connecting" : "reconnecting");
    const ws = this.factory(this.url);
    this.ws = ws;
    ws.onopen = () => ws.send(JSON.stringify({ type: "auth", token }));
    ws.onmessage = (ev) => {
      this.lastFrame = Date.now();
      let msg: { type: string; event?: LiveEvent };
      try {
        msg = JSON.parse(ev.data as string);
      } catch {
        return;
      }
      if (msg.type === "auth_ok") {
        this.attempt = 0;
        this.setStatus("live");
      } else if (msg.type === "ping") {
        ws.send(JSON.stringify({ type: "pong" }));
      } else if (msg.type === "event" && msg.event) {
        this.listeners.forEach((l) => l(msg.event!));
      }
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.stopped) return;
      this.scheduleReconnect(ev.code === 4401);
    };
    ws.onerror = () => {
      /* onclose follows */
    };
  }

  private scheduleReconnect(needsRefresh: boolean): void {
    this.setStatus("reconnecting");
    const delay = this.backoffMs(this.attempt++);
    this.timer = setTimeout(async () => {
      if (this.stopped) return;
      if (needsRefresh) await this.refreshFn();
      this.connect();
    }, delay);
  }
}

export function wsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/v1/ws/events`;
}
