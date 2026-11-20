import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, RotateCcw, ShieldAlert, Zap } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import { Chip, Empty, ErrorState, PageHeader, Panel, QueryState } from "../components/ui";
import { api, post } from "../lib/api";
import { useAuth } from "../lib/auth";
import { humanize, relTime } from "../lib/format";

interface Scenario { id: string; name: string; target: string; type: string; [k: string]: unknown }
interface ScenarioResp { enabled: boolean; environment: string; scenarios: Scenario[]; targets: string[]; limits: Record<string, unknown> }
interface ActiveResp { enabled: boolean; services: Record<string, { type: string; params: Record<string, unknown>; remaining_s: number }[]>;
  traffic: { config: { profile: string; rps: number; spike_multiplier: number }; stats: { sent: number; by_status: Record<string, number> } } | null }

const TYPES = ["latency", "error_500", "unavailable", "db_delay", "pool_exhaustion", "cpu_burn", "memory_pressure", "traffic_spike"];
const PARAM_FOR: Record<string, { key: string; label: string; min: number; max: number; step: number; def: number } | null> = {
  latency: { key: "latency_ms", label: "Added latency (ms)", min: 100, max: 10000, step: 100, def: 1500 },
  error_500: { key: "error_rate", label: "Error rate", min: 0.05, max: 1, step: 0.05, def: 0.5 },
  db_delay: { key: "db_delay_ms", label: "DB delay (ms)", min: 50, max: 5000, step: 50, def: 600 },
  cpu_burn: { key: "cpu_threads", label: "Busy threads", min: 1, max: 2, step: 1, def: 2 },
  memory_pressure: { key: "memory_mb", label: "Memory (MB)", min: 16, max: 384, step: 16, def: 256 },
  traffic_spike: { key: "multiplier", label: "Traffic multiplier", min: 1.5, max: 20, step: 0.5, def: 6 },
  unavailable: null, pool_exhaustion: null,
};

export default function FaultLabPage() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const sc = useQuery({ queryKey: ["faults", "scenarios"], queryFn: () => api<ScenarioResp>("/fault-injection/scenarios"), refetchInterval: false });
  const active = useQuery({ queryKey: ["faults", "active"], queryFn: () => api<ActiveResp>("/fault-injection"), refetchInterval: 5000 });
  const [target, setTarget] = useState("payment-service");
  const [type, setType] = useState("latency");
  const [duration, setDuration] = useState(180);
  const [value, setValue] = useState<number | null>(null);
  const param = PARAM_FOR[type];
  const v = value ?? param?.def ?? 0;
  const inject = useMutation({
    mutationFn: (body: Record<string, unknown>) => post("/fault-injection", body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["faults"] }),
  });
  const reset = useMutation({ mutationFn: () => post("/fault-injection/reset"), onSuccess: () => qc.invalidateQueries({ queryKey: ["faults"] }) });
  const allowed = can("fault:inject") && sc.data?.enabled;

  const fire = (s: Record<string, unknown>) => inject.mutate({ duration_s: duration, ...s });

  return (
    <div>
      <PageHeader title="Fault Injection Lab" subtitle="Controlled, bounded, auto-expiring failures for the local demo environment"
        actions={allowed && <button className="btn-danger" onClick={() => reset.mutate()} disabled={reset.isPending}><RotateCcw size={14} />Restore all</button>} />
      <QueryState q={sc}>
        {(s) => (
          <>
            {!s.enabled && (
              <div className="panel border-warn/40 p-4 mb-4 flex gap-3"><ShieldAlert className="text-warn shrink-0" size={18} />
                <p>Fault injection is disabled in environment <b>{s.environment}</b>. It requires <span className="font-mono">ENVIRONMENT=local</span> and <span className="font-mono">FAULT_INJECTION_ENABLED=true</span>.</p></div>
            )}
            {s.enabled && !can("fault:inject") && (
              <div className="panel p-4 mb-4 text-muted">Your role can observe the lab but cannot inject faults (requires SRE Engineer or Admin).</div>
            )}
            <div className="panel p-4 mb-4">
              <ol className="flex flex-wrap gap-x-6 gap-y-1 text-[12px] text-muted">
                {["Normal health", "Inject fault", "Telemetry shifts", "Anomaly detected (live)", "RCA ranks causes", "Resolve incident", "Recovery verified"].map((x, i) =>
                  <li key={x}><span className="num text-lens">{i + 1}.</span> {x}</li>)}
              </ol>
            </div>
            <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_380px] gap-4">
              <div className="space-y-4">
                <Panel title="Scenarios (one click)">
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                    {s.scenarios.map((x) => {
                      const { id, name, ...payload } = x;
                      return (
                        <button key={id} className="text-left rounded-md border border-line bg-base px-3 py-2.5 hover:border-lens/60 disabled:opacity-50"
                          disabled={!allowed || inject.isPending} onClick={() => fire(payload)}>
                          <div className="font-medium flex items-center gap-1.5"><Zap size={13} className="text-warn" />{name}</div>
                          <div className="text-faint text-[12px] font-mono">{x.target} · {x.type}</div>
                        </button>
                      );
                    })}
                  </div>
                </Panel>
                <Panel title="Custom fault">
                  <div className="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
                    <label className="text-[12px] text-muted">Target
                      <select className="input w-full mt-1" value={type === "traffic_spike" ? "traffic-generator" : target} disabled={type === "traffic_spike"}
                        onChange={(e) => setTarget(e.target.value)}>{s.targets.filter((t) => t !== "traffic-generator").map((t) => <option key={t}>{t}</option>)}
                        {type === "traffic_spike" && <option>traffic-generator</option>}</select></label>
                    <label className="text-[12px] text-muted">Fault
                      <select className="input w-full mt-1" value={type} onChange={(e) => { setType(e.target.value); setValue(null); }}>
                        {TYPES.map((t) => <option key={t} value={t}>{humanize(t)}</option>)}</select></label>
                    <label className="text-[12px] text-muted">Duration (s, max 900)
                      <input type="number" className="input w-full mt-1" min={10} max={900} value={duration} onChange={(e) => setDuration(Math.min(900, Math.max(10, Number(e.target.value))))} /></label>
                    {param ? (
                      <label className="text-[12px] text-muted">{param.label}: <span className="num text-ink">{v}</span>
                        <input type="range" className="w-full mt-2 accent-[rgb(var(--c-lens))]" min={param.min} max={param.max} step={param.step} value={v}
                          onChange={(e) => setValue(Number(e.target.value))} /></label>
                    ) : <div />}
                  </div>
                  <button className="btn-primary mt-4" disabled={!allowed || inject.isPending}
                    onClick={() => fire({ target: type === "traffic_spike" ? "traffic-generator" : target, type, ...(param ? { [param.key]: v } : {}) })}>
                    <FlaskConical size={14} />Inject
                  </button>
                  {inject.isError && <ErrorState error={inject.error} />}
                  {inject.isSuccess && <p className="text-ok mt-2">Fault injected. Watch <Link className="text-lens" to="/">Overview</Link>, <Link className="text-lens" to="/anomalies">Anomalies</Link> and <Link className="text-lens" to="/incidents">Incidents</Link> update live.</p>}
                </Panel>
              </div>
              <Panel title="Active faults" bodyClass="p-0">
                <QueryState q={active}>
                  {(a) => {
                    const rows = Object.entries(a.services).flatMap(([svc, fs]) => fs.map((f) => ({ svc, ...f })));
                    const spike = a.traffic && a.traffic.config.profile !== "normal";
                    return rows.length === 0 && !spike ? <Empty title="System running normally" hint="No faults are active." /> : (
                      <ul>
                        {rows.map((r) => (
                          <li key={r.svc + r.type} className="px-4 py-3 border-b border-line/60">
                            <div className="flex justify-between"><span className="font-medium">{r.svc}</span><Chip className="text-warn border-warn/40">{r.type}</Chip></div>
                            <div className="text-faint text-[12px] mt-0.5">auto-restores in <span className="num">{r.remaining_s}s</span></div>
                          </li>))}
                        {spike && <li className="px-4 py-3"><span className="font-medium">traffic-generator</span> <Chip className="text-warn border-warn/40">{a.traffic!.config.profile} ×{a.traffic!.config.spike_multiplier}</Chip></li>}
                      </ul>
                    );
                  }}
                </QueryState>
                {active.data?.traffic && (
                  <div className="px-4 py-3 border-t border-line text-[12px] text-muted">
                    Traffic generator: <span className="num">{active.data.traffic.config.rps}</span> req/s base · {active.data.traffic.stats.sent} sent ·
                    {" "}{Object.entries(active.data.traffic.stats.by_status).map(([k, n]) => `${k}:${n}`).join(" ")}
                  </div>
                )}
              </Panel>
            </div>
            <History />
          </>
        )}
      </QueryState>
    </div>
  );
}

function History() {
  const q = useQuery({ queryKey: ["faults", "history"], queryFn: () => api<{ items: { id: number; target: string; type: string; started_at: string; ended_at: string | null; expires_at: string }[] }>("/fault-injection/history") });
  return (
    <Panel title="Lab history" className="mt-4" bodyClass="p-0">
      <QueryState q={q} isEmpty={(d) => d.items.length === 0} empty={<Empty title="No faults injected yet" />}>
        {(d) => (
          <table className="w-full"><thead><tr>{["Started", "Target", "Fault", "Ended"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>{d.items.slice(0, 15).map((r) => (
              <tr key={r.id}><td className="td num text-muted">{relTime(r.started_at)}</td><td className="td">{r.target}</td><td className="td font-mono text-[12px]">{r.type}</td>
                <td className="td text-muted">{r.ended_at ? relTime(r.ended_at) : new Date(r.expires_at) < new Date() ? "expired" : "active"}</td></tr>))}</tbody></table>
        )}
      </QueryState>
    </Panel>
  );
}
