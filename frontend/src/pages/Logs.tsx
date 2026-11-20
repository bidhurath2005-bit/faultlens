import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { Waypoints } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Empty, Mono, PageHeader, Panel, QueryState } from "../components/ui";
import { api } from "../lib/api";
import { clock } from "../lib/format";
import type { LogRecord } from "../lib/types";

const LEVELS = ["ERROR", "WARNING", "INFO"];
const SERVICES = ["", "order-service", "user-service", "payment-service", "inventory-service", "notification-service"];
const LEVEL_CLS: Record<string, string> = { ERROR: "text-bad", CRITICAL: "text-bad", WARNING: "text-warn", INFO: "text-info", DEBUG: "text-faint" };

export default function LogsPage() {
  const [params, setParams] = useSearchParams();
  const [text, setText] = useState(params.get("q") ?? "");
  const [expanded, setExpanded] = useState<number | null>(null);
  const service = params.get("service") ?? "";
  const levels = (params.get("level") ?? "ERROR,WARNING").split(",").filter(Boolean);
  const traceId = params.get("trace_id") ?? "";
  const minutes = params.get("minutes") ?? (traceId ? "360" : "15");

  const update = (k: string, v: string) => {
    const p = new URLSearchParams(params);
    if (v) p.set(k, v); else p.delete(k);
    setParams(p, { replace: true });
  };

  const q = useQuery({
    queryKey: ["logs", service, levels.join(","), params.get("q"), traceId, minutes],
    queryFn: () => api<{ query: string; items: LogRecord[] }>("/logs", {
      query: { service, level: levels.join(",") || undefined, q: params.get("q") || undefined, trace_id: traceId || undefined, minutes, limit: 300 },
    }),
  });

  return (
    <div>
      <PageHeader title="Logs Explorer" subtitle="Structured JSON logs from Loki, with trace_id / span_id correlation" />
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); update("q", text.trim()); }}>
          <input className="input w-[280px]" placeholder="Search text (line contains)…" value={text} onChange={(e) => setText(e.target.value)} aria-label="search logs" maxLength={200} />
          <button className="btn-ghost">Search</button>
        </form>
        <select className="input" value={service} onChange={(e) => update("service", e.target.value)} aria-label="service filter">
          {SERVICES.map((s) => <option key={s} value={s}>{s || "All services"}</option>)}
        </select>
        <div className="flex gap-1" role="group" aria-label="severity filter">
          {LEVELS.map((l) => {
            const on = levels.includes(l);
            return (
              <button key={l} aria-pressed={on} className={clsx("btn h-8 px-2.5 text-[12px]", on ? "border-lens/60 bg-lens/10 text-ink" : "border-line text-muted")}
                onClick={() => update("level", (on ? levels.filter((x) => x !== l) : [...levels, l]).join(","))}>{l}</button>
            );
          })}
        </div>
        <select className="input" value={minutes} onChange={(e) => update("minutes", e.target.value)} aria-label="time range">
          <option value="5">Last 5m</option><option value="15">Last 15m</option><option value="60">Last 1h</option><option value="360">Last 6h</option><option value="1440">Last 24h</option>
        </select>
        <input className="input w-[290px] font-mono text-[12px]" placeholder="trace_id" value={traceId} aria-label="trace id filter"
          onChange={(e) => update("trace_id", e.target.value.trim().toLowerCase())} />
      </div>
      <Panel title="Log lines" bodyClass="p-0" actions={q.data && <Mono className="text-faint truncate max-w-[520px]">{q.data.query}</Mono>}>
        {traceId && !/^[0-9a-f]{32}$/.test(traceId) ? <Empty title="Invalid trace id" hint="Trace ids are 32 lowercase hex characters." /> : (
          <QueryState q={q} isEmpty={(d) => d.items.length === 0} empty={<Empty title="No log lines match" hint="Try another time range, include INFO, or clear filters." />}>
            {(d) => (
              <div className="font-mono text-[12px]" role="log">
                {d.items.map((l, i) => (
                  <div key={i} className="border-b border-line/50">
                    <button className="w-full grid grid-cols-[92px_74px_150px_1fr_auto] gap-3 px-3 py-1.5 text-left hover:bg-raised/60" onClick={() => setExpanded(expanded === i ? null : i)}
                      aria-expanded={expanded === i}>
                      <span className="text-faint">{clock(l.timestamp)}</span>
                      <span className={LEVEL_CLS[l.level] ?? ""}>{l.level}</span>
                      <span className="text-muted truncate">{l.service}</span>
                      <span className="truncate">{l.message} <span className="text-faint">{summarise(l.attributes)}</span></span>
                      {l.trace_id ? <Link to={`/traces/${l.trace_id}`} onClick={(e) => e.stopPropagation()} className="text-lens flex items-center gap-1" title="Open trace">
                        <Waypoints size={12} aria-hidden />{l.trace_id.slice(0, 8)}</Link> : <span className="text-faint" title="no trace context on this record">—</span>}
                    </button>
                    {expanded === i && (
                      <pre className="px-4 py-3 bg-base text-muted overflow-x-auto">{JSON.stringify(l, null, 2)}</pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </QueryState>
        )}
      </Panel>
    </div>
  );
}

function summarise(a: Record<string, string>): string {
  const keys = ["http_method", "http_route", "status_code", "duration_ms", "peer_service", "waited_ms", "rss_mb"];
  return keys.filter((k) => a[k]).map((k) => `${k}=${a[k]}`).join(" ");
}
