import { useQuery } from "@tanstack/react-query";
import { Background, Controls, MarkerType, ReactFlow, type Edge as RFEdge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";
import { HealthBadge, PageHeader, Panel, QueryState, Segmented, Empty } from "../components/ui";
import { api } from "../lib/api";
import { fmtMs, fmtPct, fmtRps, relTime } from "../lib/format";
import type { Edge, Health, MetricMap } from "../lib/types";

interface GraphResp { window: string; nodes: { id: string; kind: string; health: Health; metrics: MetricMap }[]; edges: Edge[]; source: string; generated_at: string }

const NODE_KIND_LABEL: Record<string, string> = {
  "traffic-generator": "load generator",
  user: "uninstrumented caller (servicegraph virtual node)",
};

const HEALTH_RING: Record<string, string> = {
  healthy: "rgb(var(--c-ok))", degraded: "rgb(var(--c-warn))", critical: "rgb(var(--c-bad))", down: "rgb(var(--c-bad))",
  unknown: "rgb(var(--c-faint))", external: "rgb(var(--c-faint))",
};

/** Layered layout computed from the *observed* edges (longest path from external clients). */
export function layout(nodes: string[], edges: Edge[]): Record<string, { x: number; y: number }> {
  const depth: Record<string, number> = Object.fromEntries(nodes.map((n) => [n, 0]));
  for (let i = 0; i < nodes.length; i++) {
    for (const e of edges) if (depth[e.target] < depth[e.source] + 1) depth[e.target] = depth[e.source] + 1;
  }
  const byLayer: Record<number, string[]> = {};
  nodes.forEach((n) => (byLayer[depth[n]] ??= []).push(n));
  const pos: Record<string, { x: number; y: number }> = {};
  Object.entries(byLayer).forEach(([layer, ns]) => {
    ns.sort().forEach((n, i) => (pos[n] = { x: Number(layer) * 300, y: (i - (ns.length - 1) / 2) * 120 }));
  });
  return pos;
}

export default function GraphPage() {
  const [window, setWindow] = useState<"1m" | "5m" | "15m" | "1h">("5m");
  const [sel, setSel] = useState<string | null>(null);
  const q = useQuery({ queryKey: ["graph", window], queryFn: () => api<GraphResp>("/dependencies", { query: { window } }), refetchInterval: 10_000 });

  const { nodes, edges } = useMemo(() => {
    if (!q.data) return { nodes: [] as Node[], edges: [] as RFEdge[] };
    const pos = layout(q.data.nodes.map((n) => n.id), q.data.edges);
    return {
      nodes: q.data.nodes.map((n) => ({
        id: n.id, position: pos[n.id] ?? { x: 0, y: 0 }, sourcePosition: "right", targetPosition: "left",
        data: { label: (
          <div className="text-left">
            <div className="font-medium text-[12px] text-ink">{n.id}</div>
            {n.kind === "service" ? (
              <>
                <div className="text-[11px] text-muted num mt-0.5">{fmtRps(n.metrics.rps)} · p95 {fmtMs(n.metrics.latency_p95)} · err {fmtPct(n.metrics.error_rate)}</div>
                <div className="mt-1"><HealthBadge health={n.health} /></div>
              </>
            ) : (
              <div className="text-[11px] text-faint mt-0.5">{NODE_KIND_LABEL[n.id] ?? (n.kind === "database" ? "database (observed from DB client spans)" : "external caller")}</div>
            )}
          </div>) },
        style: { background: n.kind === "service" ? "rgb(var(--c-surface))" : "rgb(var(--c-base))", border: `2px ${n.kind === "service" ? "solid" : "dashed"} ${HEALTH_RING[n.health]}`, borderRadius: 8, width: 210, padding: 8,
          boxShadow: n.health === "critical" || n.health === "down" ? "0 0 0 4px rgb(var(--c-bad) / 0.2)" : undefined },
      })) as Node[],
      edges: q.data.edges.map((e) => {
        const bad = e.error_rate >= 0.05;
        const slow = (e.p95_ms ?? 0) >= 500;
        const color = bad ? "rgb(var(--c-bad))" : slow ? "rgb(var(--c-warn))" : "rgb(var(--c-muted))";
        return {
          id: `${e.source}->${e.target}`, source: e.source, target: e.target, animated: bad || slow,
          label: `${fmtRps(e.rps)}${e.error_rate > 0 ? ` · ${fmtPct(e.error_rate)} err` : ""}`,
          labelStyle: { fill: "rgb(var(--c-muted))", fontSize: 11 }, labelBgStyle: { fill: "rgb(var(--c-base))" },
          style: { stroke: color, strokeWidth: Math.min(4, 1 + Math.log2(1 + e.rps)) },
          markerEnd: { type: MarkerType.ArrowClosed, color },
        };
      }) as RFEdge[],
    };
  }, [q.data]);

  const selected = q.data?.edges.filter((e) => e.source === sel || e.target === sel) ?? [];

  return (
    <div>
      <PageHeader title="Service Dependency Graph" subtitle="Built from caller→callee span pairs observed in real traces — nothing here is hard-coded"
        actions={<Segmented label="window" value={window} onChange={setWindow} options={[{ value: "1m", label: "1m" }, { value: "5m", label: "5m" }, { value: "15m", label: "15m" }, { value: "1h", label: "1h" }]} />} />
      <QueryState q={q} isEmpty={(d) => d.nodes.length === 0} empty={<Empty title="No observed traffic in this window" hint="Edges appear as soon as services call each other." />}>
        {(d) => (
          <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,1fr)_320px] gap-4">
            <div className="panel h-[560px]" aria-label="dependency graph">
              <ReactFlow nodes={nodes} edges={edges} fitView proOptions={{ hideAttribution: true }} colorMode="dark"
                onNodeClick={(_, n) => setSel(n.id)} nodesConnectable={false} minZoom={0.3}>
                <Background color="rgb(var(--c-line))" gap={24} />
                <Controls showInteractive={false} />
              </ReactFlow>
            </div>
            <div className="space-y-4">
              <Panel title="Edges" bodyClass="p-0">
                <table className="w-full text-[12px]">
                  <thead><tr><th className="th">Caller → callee</th><th className="th text-right">Rate</th><th className="th text-right">Err</th><th className="th text-right">P95</th></tr></thead>
                  <tbody>{(sel ? selected : d.edges).map((e) => (
                    <tr key={`${e.source}${e.target}`}>
                      <td className="td">{e.source.replace("-service", "")} → {e.target.replace("-service", "")}</td>
                      <td className="td num text-right">{fmtRps(e.rps)}</td>
                      <td className={`td num text-right ${e.error_rate >= 0.05 ? "text-bad" : ""}`}>{fmtPct(e.error_rate)}</td>
                      <td className="td num text-right">{e.p95_ms === null ? "—" : `${Math.round(e.p95_ms)} ms`}</td>
                    </tr>))}</tbody>
                </table>
                {sel && <button className="text-lens text-[12px] px-3 py-2" onClick={() => setSel(null)}>Show all edges</button>}
              </Panel>
              <p className="text-faint text-[12px]">Source: {d.source}. Updated {relTime(d.generated_at)}. Red edges: ≥5% errors; amber: P95 ≥ 500 ms.</p>
            </div>
          </div>
        )}
      </QueryState>
    </div>
  );
}
