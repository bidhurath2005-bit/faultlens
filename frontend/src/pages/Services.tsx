import { useQuery } from "@tanstack/react-query";
import { X } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { MultiLine, serviceColor } from "../components/charts";
import { Chip, HealthBadge, KV, PageHeader, Panel, QueryState, SeverityBadge } from "../components/ui";
import { api } from "../lib/api";
import { fmtBytes, fmtCores, fmtMetric, fmtMs, fmtPct, fmtRps, METRIC_LABEL, relTime } from "../lib/format";
import type { Anomaly, Health, MetricMap, Series, ServiceSummary } from "../lib/types";

interface ServiceDetail {
  name: string; display_name: string; description: string; owner_team: string; health: Health; metrics: MetricMap;
  series: Record<string, Record<string, Series>>; status_breakdown: Record<string, number>; recent_anomalies: Anomaly[];
}

export default function ServicesPage() {
  const { name } = useParams();
  const q = useQuery({ queryKey: ["services"], queryFn: () => api<{ items: ServiceSummary[] }>("/services") });
  return (
    <div>
      <PageHeader title="Services" subtitle="Golden signals, resources and observed dependencies per service" />
      <QueryState q={q} isEmpty={(d) => d.items.length === 0}>
        {(d) => (
          <div className="panel overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr>
                  {["Service", "Health", "Req/s", "P95", "P99", "Errors", "CPU", "Memory", "DB pool", "Depends on", "Anomalies (1h)"].map((h) =>
                    <th key={h} className="th">{h}</th>)}
                </tr>
              </thead>
              <tbody>
                {d.items.map((s) => (
                  <tr key={s.name} className="tr-hover" onClick={() => location.assign(`/services/${s.name}`)}>
                    <td className="td">
                      <Link to={`/services/${s.name}`} className="flex items-center gap-2 font-medium" onClick={(e) => e.stopPropagation()}>
                        <span className="h-2.5 w-2.5 rounded-sm" style={{ background: serviceColor(s.name) }} aria-hidden />
                        {s.name}
                      </Link>
                      <div className="text-faint text-[12px]">{s.owner_team} · tier {s.tier}</div>
                    </td>
                    <td className="td"><HealthBadge health={s.health} /></td>
                    <td className="td num">{fmtRps(s.metrics.rps)}</td>
                    <td className="td num">{fmtMs(s.metrics.latency_p95)}</td>
                    <td className="td num">{fmtMs(s.metrics.latency_p99)}</td>
                    <td className={`td num ${(s.metrics.error_rate ?? 0) > 0.02 ? "text-bad" : ""}`}>{fmtPct(s.metrics.error_rate, 2)}</td>
                    <td className="td num">{fmtCores(s.metrics.cpu)}</td>
                    <td className="td num">{fmtBytes(s.metrics.memory_rss)} <span className="text-faint">({fmtPct(s.metrics.memory_util, 0)})</span></td>
                    <td className="td num">{fmtPct(s.metrics.db_pool_util, 0)}</td>
                    <td className="td"><div className="flex flex-wrap gap-1">{s.dependencies.length ? s.dependencies.map((x) => <Chip key={x}>{x}</Chip>) : <span className="text-faint">—</span>}</div></td>
                    <td className={`td num ${s.anomalies_1h ? "text-warn" : "text-muted"}`}>{s.anomalies_1h}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
      {name && <ServiceDrawer name={name} />}
    </div>
  );
}

function ServiceDrawer({ name }: { name: string }) {
  const nav = useNavigate();
  const q = useQuery({ queryKey: ["services", name], queryFn: () => api<ServiceDetail>(`/services/${name}`, { query: { minutes: 30 } }) });
  const one = (f: string) => (q.data?.series[f] ? { [name]: q.data.series[f][name] ?? [] } : {});
  return (
    <div className="fixed inset-0 z-30 flex justify-end bg-black/40" onClick={() => nav("/services")}>
      <aside className="h-full w-full max-w-[760px] overflow-auto bg-base border-l border-line p-5" onClick={(e) => e.stopPropagation()} aria-label={`${name} details`}>
        <div className="flex items-start justify-between mb-4">
          <div>
            <h2 className="text-[18px] font-semibold">{name}</h2>
            <p className="text-muted">{q.data?.description}</p>
          </div>
          <button className="btn-ghost h-8 px-2" onClick={() => nav("/services")} aria-label="close"><X size={16} /></button>
        </div>
        <QueryState q={q}>
          {(d) => (
            <div className="space-y-4">
              <Panel title="Current">
                <dl className="grid grid-cols-2 gap-x-8">
                  <KV k="Health"><HealthBadge health={d.health} /></KV>
                  <KV k="Owner">{d.owner_team}</KV>
                  {(["rps", "latency_p50", "latency_p95", "latency_p99", "error_rate", "cpu", "memory_rss", "db_pool_util", "db_query_p95", "availability"] as const).map((m) =>
                    <KV key={m} k={METRIC_LABEL[m] ?? m}><span className="num">{fmtMetric(m, d.metrics[m])}</span></KV>)}
                </dl>
              </Panel>
              {([["latency_p95", "P95 latency", fmtMs], ["rps", "Request rate", fmtRps], ["error_rate", "Error rate", fmtPct],
                ["cpu", "CPU (cores)", fmtCores], ["memory_rss", "Memory RSS", fmtBytes], ["db_pool_util", "DB pool utilisation", fmtPct]] as const).map(([f, t, fmt]) => (
                <Panel key={f} title={t}><MultiLine data={one(f)} fmt={(v) => fmt(v)} height={150} /></Panel>
              ))}
              <Panel title="Recent anomalies" bodyClass="p-0">
                {d.recent_anomalies.length === 0 ? <p className="p-4 text-muted">None recorded.</p> : (
                  <ul>{d.recent_anomalies.map((a) => (
                    <li key={a.id} className="flex items-center justify-between px-4 py-2 border-b border-line/60">
                      <span>{METRIC_LABEL[a.metric] ?? a.metric} <span className="text-faint font-mono text-[12px]">{a.method}</span></span>
                      <span className="flex items-center gap-3"><span className="num text-muted">{relTime(a.detected_at)}</span><SeverityBadge severity={a.severity} /></span>
                    </li>))}</ul>
                )}
              </Panel>
            </div>
          )}
        </QueryState>
      </aside>
    </div>
  );
}
