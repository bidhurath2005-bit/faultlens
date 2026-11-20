export const fmtMs = (s: number | null | undefined): string => {
  if (s === null || s === undefined || Number.isNaN(s)) return "—";
  const ms = s * 1000;
  if (ms < 1) return `${ms.toFixed(2)} ms`;
  if (ms < 100) return `${ms.toFixed(1)} ms`;
  if (ms < 10_000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
};

export const fmtDurMs = (ms: number | null | undefined): string => (ms === null || ms === undefined ? "—" : fmtMs(ms / 1000));

export const fmtPct = (r: number | null | undefined, digits = 1): string =>
  r === null || r === undefined || Number.isNaN(r) ? "—" : `${(r * 100).toFixed(digits)}%`;

export const fmtRps = (r: number | null | undefined): string =>
  r === null || r === undefined ? "—" : r < 10 ? `${r.toFixed(2)}/s` : `${r.toFixed(1)}/s`;

export const fmtBytes = (b: number | null | undefined): string => {
  if (b === null || b === undefined) return "—";
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(0)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(0)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
};

export const fmtCores = (c: number | null | undefined): string => (c === null || c === undefined ? "—" : `${c.toFixed(2)} cores`);

export function fmtMetric(metric: string, v: number | null | undefined): string {
  if (metric.startsWith("latency") || metric.endsWith("_p95") || metric.includes("wait")) return fmtMs(v);
  if (metric === "error_rate" || metric.endsWith("_util") || metric === "availability") return fmtPct(v);
  if (metric === "rps") return fmtRps(v);
  if (metric.startsWith("memory_rss")) return fmtBytes(v);
  if (metric === "cpu") return fmtCores(v);
  return v === null || v === undefined ? "—" : v.toPrecision(3);
}

export function relTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "—";
  const d = (now - new Date(iso).getTime()) / 1000;
  if (d < 5) return "just now";
  if (d < 60) return `${Math.round(d)}s ago`;
  if (d < 3600) return `${Math.round(d / 60)}m ago`;
  if (d < 86400) return `${Math.round(d / 3600)}h ago`;
  return `${Math.round(d / 86400)}d ago`;
}

export const clock = (iso: string | number): string =>
  new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

export const humanize = (s: string): string => s.replace(/[_.]/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

export const METRIC_LABEL: Record<string, string> = {
  latency_p95: "P95 latency", latency_p99: "P99 latency", latency_p50: "P50 latency", error_rate: "Error rate",
  rps: "Request rate", cpu: "CPU", memory_rss: "Memory RSS", memory_util: "Memory util", db_pool_util: "DB pool util",
  db_query_p95: "DB query P95", availability: "Availability",
};

export const CAUSE_LABEL: Record<string, string> = {
  db_pool_exhaustion: "DB pool exhaustion", db_query_latency: "Slow DB queries", service_timeout: "Service slowdown / timeouts",
  high_cpu: "High CPU", memory_pressure: "Memory pressure", downstream_failure: "Service unavailable",
  traffic_overload: "Traffic overload", http_500_spike: "HTTP 5xx spike",
};
