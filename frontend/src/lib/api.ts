/**
 * API client. The access token lives only in memory (never localStorage); the refresh token is an
 * HttpOnly cookie the browser sends to /api/v1/auth/refresh. A 401 triggers one refresh + retry.
 */
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

type TokenListener = (token: string | null) => void;

let accessToken: string | null = null;
let refreshing: Promise<string | null> | null = null;
const listeners = new Set<TokenListener>();

export const BASE = "/api/v1";

export function getToken(): string | null {
  return accessToken;
}

export function setToken(t: string | null): void {
  accessToken = t;
  listeners.forEach((l) => l(t));
}

export function onToken(l: TokenListener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

export async function refreshToken(): Promise<string | null> {
  if (!refreshing) {
    refreshing = fetch(`${BASE}/auth/refresh`, { method: "POST", credentials: "same-origin" })
      .then(async (r) => {
        if (!r.ok) return null;
        const body = await r.json();
        return body.access_token as string;
      })
      .catch(() => null)
      .then((t) => {
        setToken(t);
        return t;
      })
      .finally(() => {
        refreshing = null;
      });
  }
  return refreshing;
}

async function parseError(r: Response): Promise<ApiError> {
  try {
    const body = await r.json();
    const e = body.error ?? {};
    return new ApiError(r.status, e.code ?? "error", e.message ?? r.statusText, e.details);
  } catch {
    return new ApiError(r.status, "error", r.statusText || `HTTP ${r.status}`);
  }
}

export async function api<T>(path: string, init: RequestInit & { query?: Record<string, unknown> } = {}): Promise<T> {
  const { query, ...rest } = init;
  let url = `${BASE}${path}`;
  if (query) {
    const qs = new URLSearchParams();
    Object.entries(query).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    });
    const s = qs.toString();
    if (s) url += `?${s}`;
  }
  const doFetch = (token: string | null) =>
    fetch(url, {
      ...rest,
      credentials: "same-origin",
      headers: {
        ...(rest.body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(rest.headers ?? {}),
      },
    });
  let r = await doFetch(accessToken);
  if (r.status === 401 && !path.startsWith("/auth/")) {
    const t = await refreshToken();
    if (t) r = await doFetch(t);
  }
  if (!r.ok) throw await parseError(r);
  if (r.status === 204) return undefined as T;
  return (await r.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });
export const put = <T>(path: string, body: unknown) => api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const patch = <T>(path: string, body: unknown) => api<T>(path, { method: "PATCH", body: JSON.stringify(body) });
