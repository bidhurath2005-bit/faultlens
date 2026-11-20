import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Chip, Empty, PageHeader, Pager, Panel, QueryState, SeverityBadge } from "../components/ui";
import { api } from "../lib/api";
import { clock, fmtMetric, METRIC_LABEL, relTime } from "../lib/format";
import type { Anomaly, Page } from "../lib/types";

const METHODS = ["", "threshold", "zscore", "moving_average", "isolation_forest"];
const SERVICES = ["", "order-service", "user-service", "payment-service", "inventory-service", "notification-service"];

interface Evaluation { id: number; created_at: string; kind: string; detector: string; dataset: string; dataset_description: string;
  metrics: { precision: number; recall: number; f1: number; false_positive_rate: number } }

export default function AnomaliesPage() {
  const [service, setService] = useState("");
  const [method, setMethod] = useState("");
  const [severity, setSeverity] = useState("");
  const [offset, setOffset] = useState(0);
  const q = useQuery({ queryKey: ["anomalies", service, method, severity, offset],
    queryFn: () => api<Page<Anomaly>>("/anomalies", { query: { service, method, severity, limit: 25, offset } }) });
  const stats = useQuery({ queryKey: ["anomalies", "stats"], queryFn: () => api<{ by_method: Record<string, number>; trend: [string, number][] }>("/anomalies/stats", { query: { hours: 6 } }) });
  const evals = useQuery({ queryKey: ["evaluations"], queryFn: () => api<{ items: Evaluation[] }>("/evaluations", { query: { kind: "anomaly" } }), refetchInterval: false });

  return (
    <div>
      <PageHeader title="Anomaly Detection" subtitle="Threshold, EWMA moving-average, robust z-score and Isolation Forest detectors over live metrics" />
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 mb-4">
        <Panel title="Anomalies per minute (6h)" className="xl:col-span-2">
          {stats.data && stats.data.trend.length ? (
            <div className="h-[160px]">
              <ResponsiveContainer>
                <BarChart data={stats.data.trend.map(([t, n]) => ({ t: clock(t).slice(0, 5), n }))} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke="rgb(var(--c-line))" vertical={false} />
                  <XAxis dataKey="t" stroke="rgb(var(--c-faint))" fontSize={11} tickLine={false} axisLine={false} minTickGap={30} />
                  <YAxis stroke="rgb(var(--c-faint))" fontSize={11} tickLine={false} axisLine={false} width={30} allowDecimals={false} />
                  <Tooltip cursor={{ fill: "rgb(var(--c-raised))" }} contentStyle={{ background: "rgb(var(--c-surface))", border: "1px solid rgb(var(--c-line))", borderRadius: 8, fontSize: 12 }} />
                  <Bar dataKey="n" name="anomalies" fill="rgb(var(--c-lens))" radius={[3, 3, 0, 0]} maxBarSize={14} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          ) : <Empty title="No anomalies in the last 6 hours" />}
        </Panel>
        <Panel title="By detection method (6h)" bodyClass="p-0">
          <ul>
            {METHODS.slice(1).map((m) => (
              <li key={m} className="flex justify-between px-4 py-2.5 border-b border-line/60 last:border-0">
                <span className="font-mono text-[12px]">{m}</span><span className="num">{stats.data?.by_method[m] ?? 0}</span>
              </li>
            ))}
          </ul>
        </Panel>
      </div>

      <div className="flex flex-wrap gap-2 mb-3">
        <select className="input" value={service} onChange={(e) => { setService(e.target.value); setOffset(0); }} aria-label="service">
          {SERVICES.map((s) => <option key={s} value={s}>{s || "All services"}</option>)}</select>
        <select className="input" value={method} onChange={(e) => { setMethod(e.target.value); setOffset(0); }} aria-label="method">
          {METHODS.map((s) => <option key={s} value={s}>{s || "All methods"}</option>)}</select>
        <select className="input" value={severity} onChange={(e) => { setSeverity(e.target.value); setOffset(0); }} aria-label="severity">
          {["", "critical", "high", "medium", "low"].map((s) => <option key={s} value={s}>{s || "All severities"}</option>)}</select>
      </div>

      <Panel title="Detected anomalies" bodyClass="p-0">
        <QueryState q={q} isEmpty={(d) => d.items.length === 0} empty={<Empty title="No anomalies match" hint="Use the Fault Lab to inject a controlled failure." />}>
          {(d) => (
            <>
              <div className="overflow-x-auto"><table className="w-full">
                <thead><tr>{["Detected", "Service", "Metric", "Method", "Score", "Observed", "Baseline", "Severity", "Incident", "Detail"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
                <tbody>
                  {d.items.map((a) => (
                    <tr key={a.id}>
                      <td className="td num text-muted" title={a.detected_at}>{relTime(a.detected_at)}</td>
                      <td className="td">{a.service}</td>
                      <td className="td">{METRIC_LABEL[a.metric] ?? a.metric}</td>
                      <td className="td font-mono text-[12px]">{a.method}</td>
                      <td className="td num">{a.score.toFixed(a.score < 1 ? 3 : 1)}</td>
                      <td className="td num">{fmtMetric(a.metric, a.observed_value)}</td>
                      <td className="td num text-muted">{fmtMetric(a.metric, a.baseline_value)}</td>
                      <td className="td"><SeverityBadge severity={a.severity} /></td>
                      <td className="td">{a.incident_id ? <a className="text-lens" href={`/incidents/${a.incident_id}`}>#{a.incident_id}</a> : <span className="text-faint">—</span>}</td>
                      <td className="td text-[12px] text-faint max-w-[260px] truncate" title={JSON.stringify(a.details)}>{detail(a)}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
              <Pager total={d.total} limit={d.limit} offset={d.offset} onChange={setOffset} />
            </>
          )}
        </QueryState>
      </Panel>

      <Panel title="Model evaluation (labelled synthetic dataset)" className="mt-4" bodyClass="p-0">
        <QueryState q={evals} isEmpty={(d) => d.items.length === 0}
          empty={<Empty title="No evaluation recorded" hint={<>Run <span className="font-mono">python -m app.ml.evaluate --persist</span> in the API container.</>} />}>
          {(d) => {
            const latest = d.items.filter((e) => e.created_at === d.items[0].created_at);
            return (
              <div>
                <p className="px-4 pt-3 text-muted text-[12px]">{latest[0].dataset_description}</p>
                <table className="w-full mt-2">
                  <thead><tr>{["Detector", "Precision", "Recall", "F1", "False-positive rate"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
                  <tbody>{latest.map((e) => (
                    <tr key={e.id}>
                      <td className="td font-mono text-[12px]">{e.detector}{e.detector === "ensemble" && <Chip className="ml-2">production</Chip>}</td>
                      <td className="td num">{e.metrics.precision.toFixed(3)}</td><td className="td num">{e.metrics.recall.toFixed(3)}</td>
                      <td className="td num">{e.metrics.f1.toFixed(3)}</td><td className="td num">{(e.metrics.false_positive_rate * 100).toFixed(2)}%</td>
                    </tr>))}</tbody>
                </table>
              </div>
            );
          }}
        </QueryState>
      </Panel>
    </div>
  );
}

function detail(a: Anomaly): string {
  const d = a.details as Record<string, unknown>;
  if (a.method === "zscore") return `z=${d.z} over n=${d.n}`;
  if (a.method === "moving_average") return `EWMA α=${d.alpha}`;
  if (a.method === "threshold") return `threshold ${d.threshold}`;
  if (a.method === "isolation_forest") {
    const c = (d.contributions as { feature: string; z: number }[] | undefined)?.slice(0, 2).map((x) => `${x.feature} z=${x.z}`).join(", ");
    return `${d.model} model; ${c ?? ""}`;
  }
  return "";
}
