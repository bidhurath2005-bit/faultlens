import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { AlertCircle, ScrollText } from "lucide-react";
import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { serviceColor } from "../components/charts";
import { Chip, Empty, Mono, PageHeader, Panel, QueryState } from "../components/ui";
import { api } from "../lib/api";
import { clock, fmtDurMs } from "../lib/format";
import type { Span, TraceSummary } from "../lib/types";

const SERVICES = ["", "order-service", "user-service", "payment-service", "inventory-service", "notification-service"];

export default function TracesPage() {
  const { traceId } = useParams();
  const nav = useNavigate();
  const [service, setService] = useState("");
  const [status, setStatus] = useState("any");
  const [minDur, setMinDur] = useState("");
  const [minutes, setMinutes] = useState("15");
  const [lookup, setLookup] = useState("");

  const q = useQuery({
    queryKey: ["traces", service, status, minDur, minutes],
    queryFn: () => api<{ query: string; items: TraceSummary[] }>("/traces",
      { query: { service, status, min_duration_ms: minDur || undefined, minutes, limit: 50 } }),
  });

  return (
    <div>
      <PageHeader title="Distributed Tracing" subtitle="Traces from Tempo, propagated with W3C trace context across all services" />
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <select className="input" value={service} onChange={(e) => setService(e.target.value)} aria-label="service filter">
          {SERVICES.map((s) => <option key={s} value={s}>{s || "All services"}</option>)}
        </select>
        <select className="input" value={status} onChange={(e) => setStatus(e.target.value)} aria-label="status filter">
          <option value="any">Any status</option><option value="error">Errors only</option><option value="ok">Non-error</option>
        </select>
        <input className="input w-[150px]" placeholder="Min duration (ms)" inputMode="numeric" value={minDur}
          onChange={(e) => setMinDur(e.target.value.replace(/\D/g, ""))} aria-label="minimum duration" />
        <select className="input" value={minutes} onChange={(e) => setMinutes(e.target.value)} aria-label="time range">
          <option value="5">Last 5m</option><option value="15">Last 15m</option><option value="60">Last 1h</option><option value="360">Last 6h</option>
        </select>
        <form className="flex gap-2 ml-auto" onSubmit={(e) => { e.preventDefault(); if (/^[0-9a-f]{32}$/.test(lookup.trim())) nav(`/traces/${lookup.trim()}`); }}>
          <input className="input w-[300px] font-mono text-[12px]" placeholder="Trace ID lookup (32 hex)" value={lookup} onChange={(e) => setLookup(e.target.value)} aria-label="trace id" />
          <button className="btn-ghost">Open</button>
        </form>
      </div>
      <div className="grid grid-cols-1 2xl:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-4">
        <Panel title="Traces" bodyClass="p-0" actions={q.data && <Mono className="text-faint">{q.data.query}</Mono>}>
          <QueryState q={q} isEmpty={(d) => d.items.length === 0} empty={<Empty title="No traces match" hint="Widen the time range or relax filters." />}>
            {(d) => (
              <table className="w-full">
                <thead><tr><th className="th">Start</th><th className="th">Root</th><th className="th">Trace ID</th><th className="th text-right">Duration</th><th className="th text-right">Spans</th></tr></thead>
                <tbody>
                  {d.items.map((t) => (
                    <tr key={t.trace_id} className={clsx("tr-hover", t.trace_id === traceId && "bg-lens/10")} onClick={() => nav(`/traces/${t.trace_id}`)}>
                      <td className="td num text-muted">{clock(t.start_time)}</td>
                      <td className="td"><span className="text-muted">{t.root_service}</span> {t.root_name}</td>
                      <td className="td"><Mono className="text-muted">{t.trace_id.slice(0, 16)}…</Mono></td>
                      <td className={clsx("td num text-right", t.duration_ms > 1000 && "text-warn")}>{fmtDurMs(t.duration_ms)}</td>
                      <td className="td num text-right text-muted">{t.span_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </QueryState>
        </Panel>
        {traceId ? <TraceView traceId={traceId} /> : (
          <Panel title="Trace detail"><Empty title="Select a trace" hint="Pick a trace to see its span waterfall, per-service breakdown and correlated logs." /></Panel>
        )}
      </div>
    </div>
  );
}

interface TraceDetail {
  trace_id: string;
  spans: Span[];
  service_breakdown: Record<string, { spans: number; errors: number }>;
  analysis: { self_time_ms: Record<string, Record<string, number>>; error_origins: { service: string; span: string; reason: string }[] } | null;
}

interface Row { span: Span; depth: number }

export function buildRows(spans: Span[]): Row[] {
  const ids = new Set(spans.map((s) => s.span_id));
  const kids = new Map<string, Span[]>();
  spans.forEach((s) => {
    const p = s.parent_span_id && ids.has(s.parent_span_id) ? s.parent_span_id : "root";
    kids.set(p, [...(kids.get(p) ?? []), s]);
  });
  const out: Row[] = [];
  const walk = (pid: string, depth: number) => {
    (kids.get(pid) ?? []).sort((a, b) => a.start_ns - b.start_ns).forEach((s) => {
      out.push({ span: s, depth });
      walk(s.span_id, depth + 1);
    });
  };
  walk("root", 0);
  return out;
}

export function TraceView({ traceId }: { traceId: string }) {
  const [selected, setSelected] = useState<Span | null>(null);
  const q = useQuery({ queryKey: ["trace", traceId], queryFn: () => api<TraceDetail>(`/traces/${traceId}`), refetchInterval: false });
  const rows = useMemo(() => (q.data ? buildRows(q.data.spans) : []), [q.data]);
  const t0 = rows.length ? Math.min(...rows.map((r) => r.span.start_ns)) : 0;
  const total = rows.length ? Math.max(...rows.map((r) => r.span.start_ns + r.span.duration_ms * 1e6)) - t0 : 1;

  return (
    <Panel title={<span>Trace <Mono className="normal-case tracking-normal text-ink">{traceId}</Mono></span>} bodyClass="p-0"
      actions={<Link className="btn-ghost h-7" to={`/logs?trace_id=${traceId}`}><ScrollText size={14} />Correlated logs</Link>}>
      <QueryState q={q}>
        {(d) => (
          <div>
            <div className="flex flex-wrap gap-2 px-4 py-3 border-b border-line">
              {Object.entries(d.service_breakdown).map(([svc, b]) => (
                <Chip key={svc} className="gap-1.5">
                  <span className="h-2 w-2 rounded-sm" style={{ background: serviceColor(svc) }} aria-hidden />
                  {svc} · {b.spans} spans{b.errors ? <span className="text-bad"> · {b.errors} err</span> : null}
                </Chip>
              ))}
            </div>
            {d.analysis && d.analysis.error_origins.length > 0 && (
              <div className="px-4 py-2 border-b border-line text-[12px] flex items-center gap-2 text-bad">
                <AlertCircle size={14} aria-hidden /> Error origin: {d.analysis.error_origins.map((o) => `${o.service} (${o.span}, ${o.reason})`).join("; ")}
              </div>
            )}
            <div className="overflow-x-auto">
              <div className="min-w-[640px]" role="tree" aria-label="span waterfall">
                {rows.map(({ span, depth }) => {
                  const left = ((span.start_ns - t0) / total) * 100;
                  const width = Math.max(0.4, ((span.duration_ms * 1e6) / total) * 100);
                  return (
                    <button key={span.span_id} role="treeitem" onClick={() => setSelected(span)}
                      className={clsx("grid grid-cols-[minmax(220px,38%)_1fr] w-full text-left items-center h-7 border-b border-line/40 hover:bg-raised/60",
                        selected?.span_id === span.span_id && "bg-lens/10")}>
                      <div className="truncate pr-2 text-[12px]" style={{ paddingLeft: 12 + depth * 14 }}>
                        <span className="text-muted">{span.service.replace("-service", "")}</span>{" "}
                        <span className={span.status === "error" ? "text-bad" : ""}>{span.name}</span>
                      </div>
                      <div className="relative h-full mr-3">
                        <div className={clsx("absolute top-1.5 h-4 rounded-sm", span.status === "error" && "ring-1 ring-bad")}
                          style={{ left: `${left}%`, width: `${width}%`, background: serviceColor(span.service), opacity: span.kind === "internal" ? 0.6 : 1 }} />
                        <span className="absolute top-1 num text-[11px] text-muted" style={{ left: `min(calc(${left + width}% + 4px), calc(100% - 60px))` }}>
                          {fmtDurMs(span.duration_ms)}
                        </span>
                      </div>
                    </button>
                  );
                })}
              </div>
            </div>
            {selected && (
              <div className="p-4 border-t border-line">
                <div className="flex justify-between mb-2">
                  <span className="font-medium">{selected.name}</span>
                  <Link className="text-lens text-[12px]" to={`/logs?trace_id=${selected.trace_id}`}>Logs for this trace →</Link>
                </div>
                <dl className="grid grid-cols-[140px_1fr] gap-x-3 gap-y-1 text-[12px]">
                  {[["service", selected.service], ["span id", selected.span_id], ["parent", selected.parent_span_id ?? "—"], ["kind", selected.kind],
                    ["start", selected.start_time], ["duration", fmtDurMs(selected.duration_ms)], ["status", `${selected.status}${selected.status_message ? `: ${selected.status_message}` : ""}`],
                    ...Object.entries(selected.attributes).map(([k, v]) => [k, String(v)])].map(([k, v]) => (
                    <div key={k} className="contents"><dt className="text-muted">{k}</dt><dd className="font-mono break-all">{v}</dd></div>
                  ))}
                </dl>
              </div>
            )}
          </div>
        )}
      </QueryState>
    </Panel>
  );
}
