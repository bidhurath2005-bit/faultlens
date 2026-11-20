import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { Bot, ChevronDown, ChevronRight, Play, Sparkles, Waypoints } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Meter, MultiLine } from "../components/charts";
import { Chip, Empty, ErrorState, Loading, Mono, PageHeader, Panel, QueryState } from "../components/ui";
import { ApiError, api, post } from "../lib/api";
import { useAuth } from "../lib/auth";
import { CAUSE_LABEL, clock, fmtDurMs, fmtMetric, METRIC_LABEL, relTime } from "../lib/format";
import type { Finding, Investigation } from "../lib/types";

interface InvRow { id: number; incident_id: number; incident_title: string | null; incident_status: string | null; status: string;
  started_at: string; duration_ms: number | null; summary: string; top_cause: string | null; top_service: string | null; top_score: number | null }

export default function RcaPage() {
  const { incidentId } = useParams();
  const nav = useNavigate();
  const list = useQuery({ queryKey: ["rca", "list"], queryFn: () => api<{ items: InvRow[] }>("/rca", { query: { limit: 30 } }) });
  // Latest investigation per incident, newest first (Object.values would order by numeric key, i.e. oldest first).
  const latestPerIncident = list.data ? Object.values(
    list.data.items.reduce<Record<number, InvRow>>((acc, r) => (acc[r.incident_id] ??= r, acc), {}))
    .sort((a, b) => b.started_at.localeCompare(a.started_at)) : [];
  return (
    <div>
      <PageHeader title="Root Cause Analysis" subtitle="Evidence-based, ranked hypotheses from correlated metrics, logs, traces and dependencies" />
      <div className="grid grid-cols-1 xl:grid-cols-[340px_minmax(0,1fr)] gap-4">
        <Panel title="Investigations" bodyClass="p-0">
          <QueryState q={list} isEmpty={(d) => d.items.length === 0} empty={<Empty title="No investigations yet" hint="Investigations start automatically when an incident opens." />}>
            {() => (
              <ul>
                {latestPerIncident.map((r) => (
                  <li key={r.id}>
                    <button onClick={() => nav(`/rca/${r.incident_id}`)} className={clsx("w-full text-left px-4 py-3 border-b border-line/60 hover:bg-raised/60",
                      String(r.incident_id) === incidentId && "bg-lens/10")}>
                      <div className="flex justify-between gap-2 text-[12px] text-muted">
                        <span>#{r.incident_id} · {r.incident_status}</span><span className="num">{relTime(r.started_at)}</span>
                      </div>
                      <div className="font-medium mt-0.5 truncate">{r.top_cause ? CAUSE_LABEL[r.top_cause] : "Inconclusive"}{r.top_service && <span className="text-muted"> · {r.top_service}</span>}</div>
                      <div className="flex items-center gap-2 mt-1.5">
                        <Meter value={r.top_score ?? 0} tone={r.status === "completed" ? "lens" : "warn"} />
                        <span className="num text-[12px] w-9 text-right">{r.top_score?.toFixed(2) ?? "—"}</span>
                      </div>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </QueryState>
        </Panel>
        {incidentId ? <InvestigationView incidentId={Number(incidentId)} /> : (
          <Panel title="Investigation"><Empty title="Select an investigation" hint="Each investigation lists ranked root-cause hypotheses with the exact evidence and scoring behind them." /></Panel>
        )}
      </div>
    </div>
  );
}

export function InvestigationView({ incidentId, compact }: { incidentId: number; compact?: boolean }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["rca", incidentId], queryFn: () => api<Investigation>(`/rca/${incidentId}`) });
  const rerun = useMutation({ mutationFn: () => post<Investigation>(`/rca/${incidentId}/run`),
    onSuccess: (d) => { qc.setQueryData(["rca", incidentId], d); qc.invalidateQueries({ queryKey: ["rca", "list"] }); } });

  if (q.isLoading) return <Panel title="Investigation"><Loading /></Panel>;
  if (q.isError) {
    const notYet = q.error instanceof ApiError && q.error.status === 404;
    return (
      <Panel title="Investigation">
        {notYet ? <Empty title="No investigation yet" hint="The worker starts one ~20 s after an incident opens, to let evidence accumulate." /> : <ErrorState error={q.error} />}
        {can("investigation:run") && <div className="flex justify-center pb-4"><button className="btn-primary" onClick={() => rerun.mutate()} disabled={rerun.isPending}><Play size={14} />Run now</button></div>}
      </Panel>
    );
  }
  const inv = q.data!;
  const metricServices = Object.keys(inv.evidence.metrics ?? {});
  return (
    <div className="space-y-4">
      <Panel title={`Investigation #${inv.id} · incident #${inv.incident_id}`} actions={
        can("investigation:run") && <button className="btn-ghost h-7" onClick={() => rerun.mutate()} disabled={rerun.isPending}><Play size={13} />{rerun.isPending ? "Running…" : "Re-run"}</button>}>
        <p className={clsx("text-[14px]", inv.status === "inconclusive" && "text-warn")}>{inv.summary}</p>
        <div className="flex flex-wrap gap-2 mt-3 text-[12px]">
          <Chip>status: {inv.status}</Chip>
          <Chip>window {clock(inv.window_start)}–{clock(inv.window_end)}</Chip>
          <Chip>{inv.evidence.hypotheses_considered ?? 0} hypotheses scored</Chip>
          <Chip>took {fmtDurMs(inv.duration_ms)}</Chip>
          <Chip>engine v{inv.engine_version}</Chip>
          {Object.entries(inv.evidence.sources ?? {}).map(([k, v]) => (
            <Chip key={k} className={v === "ok" ? "" : "text-warn border-warn/40"}>{k}: {v === "ok" ? "ok" : "unavailable"}</Chip>
          ))}
        </div>
      </Panel>

      <Panel title="Ranked root-cause hypotheses" bodyClass="p-0">
        {inv.findings.length === 0 ? <Empty title="No hypothesis met the evidence gate" hint="Nothing is reported without primary evidence." /> :
          inv.findings.map((f, i) => <FindingRow key={f.id} f={f} defaultOpen={i === 0} />)}
      </Panel>

      {!compact && <Assistant incidentId={incidentId} />}

      {!compact && (
        <>
          <Panel title="Correlated logs" bodyClass="p-0">
            {(inv.evidence.log_highlights ?? []).length === 0 ? <Empty title="No warning/error log patterns in the window" /> : (
              <table className="w-full">
                <thead><tr><th className="th">Service</th><th className="th">Pattern</th><th className="th text-right">Count</th><th className="th">Example trace</th></tr></thead>
                <tbody>{inv.evidence.log_highlights!.map((h, i) => (
                  <tr key={i}>
                    <td className="td">{h.service}</td>
                    <td className="td font-mono text-[12px]">{h.template}</td>
                    <td className="td num text-right">{h.count}</td>
                    <td className="td">{h.sample.trace_id ? <Link className="text-lens font-mono text-[12px]" to={`/traces/${h.sample.trace_id}`}>{h.sample.trace_id.slice(0, 12)}…</Link> : <span className="text-faint">no trace context</span>}</td>
                  </tr>))}</tbody>
              </table>
            )}
          </Panel>
          <Panel title="Distributed traces examined" bodyClass="p-0">
            {(inv.evidence.traces?.examples ?? []).length === 0 ? <Empty title="No slow or failing traces in the window" /> : (
              <table className="w-full">
                <thead><tr><th className="th">Trace</th><th className="th">Root</th><th className="th text-right">Duration</th><th className="th">Error origin</th></tr></thead>
                <tbody>{inv.evidence.traces!.examples.map((t) => (
                  <tr key={t.trace_id}>
                    <td className="td"><Link className="text-lens font-mono text-[12px] inline-flex items-center gap-1" to={`/traces/${t.trace_id}`}><Waypoints size={12} />{t.trace_id.slice(0, 16)}…</Link></td>
                    <td className="td">{t.root_service}</td>
                    <td className="td num text-right">{fmtDurMs(t.duration_ms)}</td>
                    <td className="td text-[12px]">{t.error_origins.length ? t.error_origins.map((o) => `${o.service} (${o.reason})`).join(", ") : <span className="text-faint">—</span>}</td>
                  </tr>))}</tbody>
              </table>
            )}
          </Panel>
          {metricServices.length > 0 && (
            <Panel title="Related metrics during the investigation window">
              <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
                {["latency_p95", "error_rate", "db_pool_util", "cpu"].map((m) => (
                  <div key={m}>
                    <div className="text-muted text-[12px] mb-1">{METRIC_LABEL[m]}</div>
                    <MultiLine height={150} fmt={(v) => fmtMetric(m, v)}
                      data={Object.fromEntries(metricServices.map((s) => [s, inv.evidence.metrics![s][m]?.series ?? []]))} />
                  </div>
                ))}
              </div>
            </Panel>
          )}
        </>
      )}
    </div>
  );
}

function FindingRow({ f, defaultOpen }: { f: Finding; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const tone = f.confidence === "high" ? "lens" : f.confidence === "medium" ? "ok" : "warn";
  return (
    <div className="border-b border-line/60 last:border-0">
      <button className="w-full grid grid-cols-[28px_minmax(0,1fr)_160px_70px] gap-3 items-center px-4 py-3 text-left hover:bg-raised/50" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="num text-muted">#{f.rank}</span>
        <span className="min-w-0">
          <span className="font-medium flex items-center gap-1.5">{open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}{f.title}</span>
          <span className="text-faint text-[12px] ml-5">{f.confidence} confidence · locality ×{f.evidence.locality}</span>
        </span>
        <Meter value={f.score} tone={tone} />
        <span className="num text-right">{f.score.toFixed(3)}</span>
      </button>
      {open && (
        <div className="px-4 pb-4 pl-12 space-y-3">
          <div>
            <div className="text-2xs uppercase tracking-wider text-faint mb-1.5">Score breakdown: Σ weight × strength = {f.evidence.raw_score.toFixed(3)} × locality {f.evidence.locality} = {f.score.toFixed(3)}</div>
            <table className="w-full text-[12px]">
              <thead><tr><th className="th">Signal</th><th className="th">Source</th><th className="th">Observed</th><th className="th text-right">Weight</th><th className="th w-[120px]">Strength</th><th className="th text-right">Contribution</th></tr></thead>
              <tbody>{f.signals.map((s) => (
                <tr key={s.name}>
                  <td className="td">{s.description}{s.primary && <Chip className="ml-1.5">primary</Chip>}</td>
                  <td className="td text-muted">{s.source}</td>
                  <td className="td font-mono">{s.value || "—"}</td>
                  <td className="td num text-right">{s.weight.toFixed(2)}</td>
                  <td className="td"><div className="flex items-center gap-2"><Meter value={s.strength} /><span className="num w-8">{s.strength.toFixed(2)}</span></div></td>
                  <td className="td num text-right">{s.contribution.toFixed(3)}</td>
                </tr>))}</tbody>
            </table>
            <p className="text-faint text-[12px] mt-1.5">Locality: {f.evidence.locality_reason}. {f.evidence.upstream_affected.length > 0 && <>Upstream potentially affected: {f.evidence.upstream_affected.join(", ")}.</>}</p>
          </div>
          {f.recommendations.length > 0 && (
            <div>
              <div className="text-2xs uppercase tracking-wider text-faint mb-1.5">Recommended remediation (advisory, never auto-executed)</div>
              <ol className="space-y-1.5">{f.recommendations.map((r) => (
                <li key={r.priority} className="flex gap-2"><span className="num text-lens">{r.priority}.</span>
                  <span>{r.action} <span className="text-faint">— {r.rationale} (risk: {r.risk})</span></span></li>))}</ol>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

interface Answer { text: string; provider: string; validated: boolean; notes: string[]; question: string | null }

function Assistant({ incidentId }: { incidentId: number }) {
  const [question, setQuestion] = useState("");
  const [history, setHistory] = useState<Answer[]>([]);
  const ask = useMutation({
    mutationFn: (q?: string) => post<Answer>(`/assistant/incidents/${incidentId}`, q ? { question: q } : undefined),
    onSuccess: (a) => setHistory((h) => [...h, a]),
  });
  const submit = (e: FormEvent) => { e.preventDefault(); if (question.trim().length >= 2) { ask.mutate(question.trim()); setQuestion(""); } };
  return (
    <Panel title={<span className="inline-flex items-center gap-1.5"><Bot size={14} />Investigation assistant</span>}
      actions={<button className="btn-ghost h-7" onClick={() => ask.mutate(undefined)} disabled={ask.isPending}><Sparkles size={13} />Summarise</button>}>
      <p className="text-faint text-[12px] mb-3">Answers are generated only from this investigation's verified findings. If an LLM provider is configured, its output is validated against the evidence and rejected on any unsupported claim.</p>
      <div className="space-y-3 mb-3">
        {history.map((a, i) => (
          <div key={i} className="rounded-md border border-line bg-base p-3">
            {a.question && <div className="text-muted text-[12px] mb-1">Q: {a.question}</div>}
            <p className="leading-relaxed">{a.text}</p>
            <div className="text-faint text-[11px] mt-1.5">via <Mono>{a.provider}</Mono> · {a.notes.join(" · ")}</div>
          </div>
        ))}
        {ask.isError && <ErrorState error={ask.error} />}
      </div>
      <form className="flex gap-2" onSubmit={submit}>
        <input className="input flex-1" value={question} maxLength={500} onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask: why this cause? what do the logs show? how do I fix it? has this happened before?" aria-label="question" />
        <button className="btn-primary" disabled={ask.isPending}>{ask.isPending ? "…" : "Ask"}</button>
      </form>
    </Panel>
  );
}
