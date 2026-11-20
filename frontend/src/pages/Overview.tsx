import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { MultiLine, SingleArea } from "../components/charts";
import { HealthBadge, PageHeader, Panel, QueryState, Segmented, SeverityBadge, Stat, Empty } from "../components/ui";
import { api } from "../lib/api";
import { fmtMetric, fmtMs, fmtPct, fmtRps, METRIC_LABEL, relTime } from "../lib/format";
import type { Health, Overview, Series } from "../lib/types";

const RANGES = [{ value: "15", label: "15m" }, { value: "30", label: "30m" }, { value: "60", label: "1h" }, { value: "180", label: "3h" }];

export default function OverviewPage() {
  const [range, setRange] = useState("30");
  const q = useQuery({ queryKey: ["overview", range], queryFn: () => api<Overview>("/metrics/overview", { query: { minutes: range } }) });
  const per = useQuery({
    queryKey: ["overview", "per-service", range],
    queryFn: () => api<{ series: Record<string, Record<string, Series>> }>("/metrics/services",
      { query: { minutes: range, features: "latency_p95,error_rate,rps" } }),
  });

  return (
    <div>
      <PageHeader title="Overview" subtitle="Live health of the checkout platform"
        actions={<Segmented label="time range" value={range} onChange={setRange} options={RANGES} />} />
      <QueryState q={q}>
        {(d) => {
          const err = d.current.error_rate;
          return (
            <div className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-7 gap-3">
                <Stat label="Overall health" value={<span className="font-sans text-[20px]"><HealthBadge health={d.overall_health} /></span>} sub={`${d.services_reporting}/${d.services_total} services reporting`} />
                <Stat label="Active incidents" value={d.active_incidents} tone={d.active_incidents ? "bad" : "ok"}
                  sub={Object.entries(d.active_incidents_by_severity).map(([k, v]) => `${v} ${k}`).join(" · ") || "none open"} />
                <Stat label="Request rate" value={fmtRps(d.current.rps)} sub="all services" />
                <Stat label="Error rate" value={fmtPct(err, 2)} tone={err && err > 0.05 ? "bad" : err && err > 0.01 ? "warn" : undefined} sub="5xx / total" />
                <Stat label="Avg latency" value={fmtMs(d.current.latency_avg)} />
                <Stat label="P95 latency" value={fmtMs(d.current.latency_p95)} />
                <Stat label="P99 latency" value={fmtMs(d.current.latency_p99)} />
              </div>

              <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
                <Panel title="Service health" className="xl:col-span-1" bodyClass="p-0">
                  <ul>
                    {Object.entries(d.service_health).sort().map(([svc, h]) => (
                      <li key={svc} className="flex items-center justify-between px-4 py-2.5 border-b border-line/60 last:border-0">
                        <Link to={`/services/${svc}`} className="hover:text-lens">{svc}</Link>
                        <HealthBadge health={h as Health} />
                      </li>
                    ))}
                  </ul>
                </Panel>
                <Panel title="Request rate (all services)" className="xl:col-span-2">
                  <SingleArea data={d.series.rps ?? []} fmt={fmtRps} height={180} />
                </Panel>
              </div>

              <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
                <Panel title="P95 latency by service">
                  {per.data ? <MultiLine data={per.data.series.latency_p95 ?? {}} fmt={fmtMs} /> : <div className="h-[220px]" />}
                </Panel>
                <Panel title="Error rate by service">
                  {per.data ? <MultiLine data={per.data.series.error_rate ?? {}} fmt={(v) => fmtPct(v)} /> : <div className="h-[220px]" />}
                </Panel>
              </div>

              <Panel title="Recent anomalies" actions={<Link to="/anomalies" className="text-lens text-[12px]">View all</Link>} bodyClass="p-0">
                {d.recent_anomalies.length === 0 ? <Empty title="No anomalies detected" hint="Detectors run every 10 s over live Prometheus metrics." /> : (
                  <table className="w-full">
                    <thead><tr><th className="th">When</th><th className="th">Service</th><th className="th">Metric</th><th className="th">Method</th><th className="th">Observed</th><th className="th">Baseline</th><th className="th">Severity</th></tr></thead>
                    <tbody>
                      {d.recent_anomalies.map((a) => (
                        <tr key={a.id}>
                          <td className="td num text-muted">{relTime(a.detected_at)}</td>
                          <td className="td">{a.service}</td>
                          <td className="td">{METRIC_LABEL[a.metric] ?? a.metric}</td>
                          <td className="td font-mono text-[12px] text-muted">{a.method}</td>
                          <td className="td num">{fmtMetric(a.metric, a.observed_value)}</td>
                          <td className="td num text-muted">{fmtMetric(a.metric, a.baseline_value)}</td>
                          <td className="td"><SeverityBadge severity={a.severity} /></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </Panel>
            </div>
          );
        }}
      </QueryState>
    </div>
  );
}
