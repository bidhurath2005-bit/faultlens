import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { CheckCircle2, HeartPulse, Plus, X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Chip, Empty, ErrorState, KV, PageHeader, Pager, Panel, QueryState, SeverityBadge, StatusBadge } from "../components/ui";
import { api, post } from "../lib/api";
import { useAuth } from "../lib/auth";
import { clock, fmtMetric, METRIC_LABEL, relTime } from "../lib/format";
import type { Incident, IncidentDetail, Page, Severity } from "../lib/types";
import { InvestigationView } from "./Rca";

const SERVICES = ["order-service", "user-service", "payment-service", "inventory-service", "notification-service"];

export default function IncidentsPage() {
  const { id } = useParams();
  const nav = useNavigate();
  const { can } = useAuth();
  const [status, setStatus] = useState("");
  const [severity, setSeverity] = useState("");
  const [offset, setOffset] = useState(0);
  const [creating, setCreating] = useState(false);
  const q = useQuery({ queryKey: ["incidents", status, severity, offset],
    queryFn: () => api<Page<Incident>>("/incidents", { query: { status, severity, limit: 20, offset } }) });

  return (
    <div>
      <PageHeader title="Incidents" subtitle="OPEN → ACKNOWLEDGED → INVESTIGATING → RESOLVED, with full timeline and audit"
        actions={can("incident:write") && <button className="btn-primary" onClick={() => setCreating(true)}><Plus size={14} />Declare incident</button>} />
      <div className="flex gap-2 mb-3">
        <select className="input" value={status} onChange={(e) => { setStatus(e.target.value); setOffset(0); }} aria-label="status">
          {["", "open", "acknowledged", "investigating", "resolved"].map((s) => <option key={s} value={s}>{s || "All statuses"}</option>)}</select>
        <select className="input" value={severity} onChange={(e) => { setSeverity(e.target.value); setOffset(0); }} aria-label="severity">
          {["", "critical", "high", "medium", "low"].map((s) => <option key={s} value={s}>{s || "All severities"}</option>)}</select>
      </div>
      <div className={clsx("grid gap-4", id ? "grid-cols-1 2xl:grid-cols-[minmax(0,0.85fr)_minmax(0,1.3fr)]" : "grid-cols-1")}>
        <Panel title="Incident list" bodyClass="p-0">
          <QueryState q={q} isEmpty={(d) => d.items.length === 0} empty={<Empty title="No incidents" hint="Incidents open automatically when correlated anomalies are detected." />}>
            {(d) => (
              <>
                <div className="overflow-x-auto">
                <table className="w-full table-fixed">
                  {/* Compact columns while the detail panel is open, so the list never overflows into it. */}
                  <colgroup>
                    <col className="w-12" /><col /><col className="w-[104px]" /><col className="w-[132px]" />
                    {!id && <col className="w-[300px]" />}<col className="w-[84px]" />
                  </colgroup>
                  <thead><tr>{["#", "Title", "Severity", "Status", ...(id ? [] : ["Services"]), "Opened"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
                  <tbody>{d.items.map((i) => (
                    <tr key={i.id} className={clsx("tr-hover", String(i.id) === id && "bg-lens/10")} onClick={() => nav(`/incidents/${i.id}`)}>
                      <td className="td num text-muted">{i.id}</td>
                      <td className="td font-medium truncate" title={i.title}>{i.title}</td>
                      <td className="td"><SeverityBadge severity={i.severity} /></td>
                      <td className="td"><StatusBadge status={i.status} /></td>
                      {!id && <td className="td"><div className="flex flex-wrap gap-1">{i.services.map((s) => <Chip key={s}>{s}</Chip>)}</div></td>}
                      <td className="td num text-muted" title={i.opened_at}>{relTime(i.opened_at)}</td>
                    </tr>))}</tbody>
                </table>
                </div>
                <Pager total={d.total} limit={d.limit} offset={d.offset} onChange={setOffset} />
              </>
            )}
          </QueryState>
        </Panel>
        {id && <IncidentDetailView id={Number(id)} />}
      </div>
      {creating && <CreateIncident onClose={() => setCreating(false)} />}
    </div>
  );
}

function IncidentDetailView({ id }: { id: number }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [note, setNote] = useState("");
  const q = useQuery({ queryKey: ["incident", id], queryFn: () => api<IncidentDetail>(`/incidents/${id}`) });
  const act = useMutation({
    mutationFn: ({ action, body }: { action: string; body?: unknown }) => post<Incident>(`/incidents/${id}/${action}`, body ?? {}),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["incident", id] }); qc.invalidateQueries({ queryKey: ["incidents"] }); setNote(""); },
  });

  return (
    <div className="space-y-4 min-w-0">
      <QueryState q={q}>
        {(inc) => (
          <>
            <Panel title={`Incident #${inc.id}`} actions={<StatusBadge status={inc.status} />}>
              <h2 className="text-[16px] font-semibold mb-3">{inc.title}</h2>
              <dl className="grid grid-cols-1 md:grid-cols-2 gap-x-8">
                <KV k="Severity"><SeverityBadge severity={inc.severity} /></KV>
                <KV k="Source">{inc.source}</KV>
                <KV k="Primary service">{inc.primary_service ?? "—"}</KV>
                <KV k="Affected"><span title={inc.services.join(", ")}>{inc.services.length > 2 ? `${inc.services.slice(0, 2).join(", ")} +${inc.services.length - 2}` : inc.services.join(", ")}</span></KV>
                <KV k="Opened"><span className="num">{clock(inc.opened_at)}</span> <span className="text-faint">({relTime(inc.opened_at)})</span></KV>
                <KV k="Acknowledged">{inc.acknowledged_at ? relTime(inc.acknowledged_at) : "—"}</KV>
                <KV k="Resolved">{inc.resolved_at ? relTime(inc.resolved_at) : "—"}</KV>
                <KV k="Recovery">{inc.recovery_verified_at
                  ? <span className="text-ok inline-flex items-center gap-1"><HeartPulse size={13} />verified {relTime(inc.recovery_verified_at)}</span>
                  : <span className="text-faint">not yet verified</span>}</KV>
              </dl>
              {inc.resolution_note && <p className="mt-3 text-muted">Resolution: {inc.resolution_note}</p>}
              {can("incident:write") && inc.status !== "resolved" && (
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  {inc.status === "open" && <button className="btn-primary" onClick={() => act.mutate({ action: "acknowledge" })} disabled={act.isPending}>Acknowledge</button>}
                  {inc.status === "acknowledged" && <button className="btn-ghost" onClick={() => act.mutate({ action: "investigate" })} disabled={act.isPending}>Start investigating</button>}
                  {inc.status !== "open" && (
                    <form className="flex gap-2 flex-1 min-w-[280px]" onSubmit={(e) => { e.preventDefault(); act.mutate({ action: "resolve", body: { note } }); }}>
                      <input className="input flex-1" placeholder="Resolution note (required)" value={note} onChange={(e) => setNote(e.target.value)} minLength={3} required aria-label="resolution note" />
                      <button className="btn-primary" disabled={act.isPending || note.trim().length < 3}><CheckCircle2 size={14} />Resolve</button>
                    </form>
                  )}
                </div>
              )}
              {!can("incident:write") && <p className="mt-3 text-faint text-[12px]">Read-only: your role cannot change incident state.</p>}
              {act.isError && <ErrorState error={act.error} />}
            </Panel>

            <InvestigationView incidentId={id} compact />
            <Link to={`/rca/${id}`} className="text-lens text-[12px] inline-block">Open full investigation with logs, traces and assistant →</Link>

            <Panel title="Timeline" bodyClass="p-0">
              <ol className="relative">
                {inc.timeline.map((e) => (
                  <li key={e.id} className="grid grid-cols-[76px_1fr] gap-3 px-4 py-2.5 border-b border-line/60 last:border-0">
                    <span className="num text-faint text-[12px] pt-0.5">{clock(e.occurred_at)}</span>
                    <div>
                      <div className="font-mono text-[11px] text-lens">{e.kind}</div>
                      <div>{e.message}</div>
                      {e.actor && <div className="text-faint text-[12px]">by {e.actor}</div>}
                    </div>
                  </li>))}
              </ol>
            </Panel>

            <Panel title={`Correlated anomalies (${inc.anomalies.length})`} bodyClass="p-0">
              <table className="w-full">
                <thead><tr>{["Time", "Service", "Metric", "Method", "Observed", "Severity"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
                <tbody>{inc.anomalies.slice(0, 50).map((a) => (
                  <tr key={a.id}><td className="td num text-muted">{clock(a.detected_at)}</td><td className="td">{a.service}</td>
                    <td className="td">{METRIC_LABEL[a.metric] ?? a.metric}</td><td className="td font-mono text-[12px]">{a.method}</td>
                    <td className="td num">{fmtMetric(a.metric, a.observed_value)}</td><td className="td"><SeverityBadge severity={a.severity} /></td></tr>))}</tbody>
              </table>
            </Panel>
          </>
        )}
      </QueryState>
    </div>
  );
}

function CreateIncident({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState<Severity>("medium");
  const [services, setServices] = useState<string[]>([]);
  const [description, setDescription] = useState("");
  const create = useMutation({
    mutationFn: () => post<Incident>("/incidents", { title, severity, services, description: description || undefined }),
    onSuccess: (inc) => { qc.invalidateQueries({ queryKey: ["incidents"] }); onClose(); nav(`/incidents/${inc.id}`); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); create.mutate(); };
  return (
    <div className="fixed inset-0 z-40 grid place-items-center bg-black/50 px-4" onClick={onClose}>
      <form className="panel w-full max-w-[480px] p-5" onClick={(e) => e.stopPropagation()} onSubmit={submit} aria-label="declare incident">
        <div className="flex justify-between mb-4"><h2 className="text-[16px] font-semibold">Declare incident</h2>
          <button type="button" className="btn-ghost h-7 px-2" onClick={onClose} aria-label="close"><X size={14} /></button></div>
        <label className="block text-[12px] text-muted mb-1" htmlFor="t">Title</label>
        <input id="t" className="input w-full mb-3" value={title} onChange={(e) => setTitle(e.target.value)} minLength={3} maxLength={255} required />
        <label className="block text-[12px] text-muted mb-1" htmlFor="s">Severity</label>
        <select id="s" className="input w-full mb-3" value={severity} onChange={(e) => setSeverity(e.target.value as Severity)}>
          {["critical", "high", "medium", "low"].map((s) => <option key={s}>{s}</option>)}</select>
        <fieldset className="mb-3"><legend className="text-[12px] text-muted mb-1">Affected services</legend>
          <div className="flex flex-wrap gap-1.5">{SERVICES.map((s) => {
            const on = services.includes(s);
            return <button type="button" key={s} aria-pressed={on} onClick={() => setServices(on ? services.filter((x) => x !== s) : [...services, s])}
              className={clsx("btn h-7 px-2 text-[12px]", on ? "border-lens/60 bg-lens/10" : "border-line text-muted")}>{s}</button>;
          })}</div></fieldset>
        <label className="block text-[12px] text-muted mb-1" htmlFor="d">Description</label>
        <textarea id="d" className="input w-full h-20 py-1.5 mb-4" value={description} onChange={(e) => setDescription(e.target.value)} maxLength={2000} />
        {create.isError && <ErrorState error={create.error} />}
        <div className="flex justify-end gap-2"><button type="button" className="btn-ghost" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={create.isPending || services.length === 0}>Declare</button></div>
      </form>
    </div>
  );
}
