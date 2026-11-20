import { useMemo } from "react";
import { Area, AreaChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { clock } from "../lib/format";
import type { Series } from "../lib/types";

/** Service identity colours: validated categorical palette (dataviz reference slots 1-5, dark steps),
 *  fixed per service so a filter never repaints survivors. Status colours are never reused here. */
export const SERVICE_COLORS: Record<string, string> = {
  "order-service": "#3987e5",
  "payment-service": "#d95926",
  "inventory-service": "#199e70",
  "user-service": "#c98500",
  "notification-service": "#d55181",
};
export const serviceColor = (s: string) => SERVICE_COLORS[s] ?? "#8b93a7";

const AXIS = { stroke: "rgb(var(--c-faint))", fontSize: 11, tickLine: false, axisLine: false } as const;
const GRID = { stroke: "rgb(var(--c-line))", strokeDasharray: "0", vertical: false } as const;

function TooltipBox({ active, payload, label, fmt }: {
  active?: boolean; payload?: { name: string; value: number; color: string }[]; label?: number; fmt: (v: number) => string;
}) {
  if (!active || !payload?.length) return null;
  return (
    <div className="panel px-2.5 py-2 shadow-xl text-[12px]">
      <div className="text-muted mb-1 num">{label !== undefined ? clock(label * 1000) : ""}</div>
      {payload.filter((p) => p.value !== null && p.value !== undefined).sort((a, b) => b.value - a.value).map((p) => (
        <div key={p.name} className="flex items-center gap-2">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: p.color }} aria-hidden />
          <span className="text-muted">{p.name}</span>
          <span className="num ml-auto pl-3 text-ink">{fmt(p.value)}</span>
        </div>
      ))}
    </div>
  );
}

/** Merge {series: [[t, v]]} into recharts rows keyed by timestamp. */
export function mergeSeries(bySeries: Record<string, Series>): Record<string, number>[] {
  const rows = new Map<number, Record<string, number>>();
  for (const [name, pts] of Object.entries(bySeries)) {
    for (const [t, v] of pts) {
      const row = rows.get(t) ?? { t };
      row[name] = v;
      rows.set(t, row);
    }
  }
  return [...rows.values()].sort((a, b) => a.t - b.t);
}

export function MultiLine({ data, fmt, height = 220, colorFor = serviceColor, yMax }: {
  data: Record<string, Series>; fmt: (v: number) => string; height?: number; colorFor?: (s: string) => string; yMax?: number;
}) {
  const rows = useMemo(() => mergeSeries(data), [data]);
  const names = Object.keys(data).sort();
  if (!rows.length) return <div className="grid place-items-center text-faint text-[12px]" style={{ height }}>No samples in range</div>;
  return (
    <div>
      <div style={{ height }} aria-label="time series chart" role="img">
        <ResponsiveContainer>
          <LineChart data={rows} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <CartesianGrid {...GRID} />
            <XAxis dataKey="t" {...AXIS} tickFormatter={(t) => clock(t * 1000).slice(0, 5)} minTickGap={40} />
            <YAxis {...AXIS} width={58} tickFormatter={fmt} domain={[0, yMax ?? "auto"]} />
            <Tooltip content={<TooltipBox fmt={fmt} />} cursor={{ stroke: "rgb(var(--c-muted))", strokeWidth: 1 }} />
            {names.map((n) => (
              <Line key={n} type="monotone" dataKey={n} name={n} stroke={colorFor(n)} strokeWidth={2} dot={false}
                isAnimationActive={false} connectNulls activeDot={{ r: 4, strokeWidth: 2, stroke: "rgb(var(--c-surface))" }} />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
      {names.length > 1 && <Legend names={names} colorFor={colorFor} />}
    </div>
  );
}

export function Legend({ names, colorFor = serviceColor }: { names: string[]; colorFor?: (s: string) => string }) {
  return (
    <ul className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[12px] text-muted" aria-label="legend">
      {names.map((n) => (
        <li key={n} className="flex items-center gap-1.5">
          <span className="inline-block h-0.5 w-3 rounded" style={{ background: colorFor(n) }} aria-hidden />
          {n}
        </li>
      ))}
    </ul>
  );
}

/** Single-series area (title names the series, so no legend). */
export function SingleArea({ data, fmt, height = 140, color = "rgb(var(--c-lens))" }: {
  data: Series; fmt: (v: number) => string; height?: number; color?: string;
}) {
  const rows = useMemo(() => data.map(([t, v]) => ({ t, v })), [data]);
  if (!rows.length) return <div className="grid place-items-center text-faint text-[12px]" style={{ height }}>No samples in range</div>;
  return (
    <div style={{ height }} role="img" aria-label="time series">
      <ResponsiveContainer>
        <AreaChart data={rows} margin={{ top: 6, right: 6, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id="fl-area" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.28} />
              <stop offset="100%" stopColor={color} stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid {...GRID} />
          <XAxis dataKey="t" {...AXIS} tickFormatter={(t) => clock(t * 1000).slice(0, 5)} minTickGap={40} />
          <YAxis {...AXIS} width={58} tickFormatter={fmt} />
          <Tooltip content={({ active, payload, label }) => (
            <TooltipBox active={active} label={label as number} fmt={fmt}
              payload={payload?.map((p) => ({ name: "value", value: p.value as number, color }))} />
          )} />
          <Area type="monotone" dataKey="v" stroke={color} strokeWidth={2} fill="url(#fl-area)" isAnimationActive={false} />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function Sparkline({ data, color = "rgb(var(--c-lens))", width = 96, height = 24 }: {
  data: Series; color?: string; width?: number; height?: number;
}) {
  if (data.length < 2) return <span className="text-faint">—</span>;
  const vs = data.map((d) => d[1]);
  const min = Math.min(...vs);
  const max = Math.max(...vs);
  const span = max - min || 1;
  const pts = vs.map((v, i) => `${(i / (vs.length - 1)) * width},${height - 2 - ((v - min) / span) * (height - 4)}`).join(" ");
  return (
    <svg width={width} height={height} aria-hidden className="overflow-visible">
      <polyline points={pts} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
    </svg>
  );
}

/** Horizontal meter for a 0..1 strength. */
export function Meter({ value, tone = "lens" }: { value: number; tone?: "lens" | "warn" | "bad" | "ok" }) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  const bg = { lens: "bg-lens", warn: "bg-warn", bad: "bg-bad", ok: "bg-ok" }[tone];
  return (
    <div className="h-1.5 w-full rounded-full bg-raised overflow-hidden" role="meter" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
      <div className={`h-full rounded-full ${bg}`} style={{ width: `${pct}%` }} />
    </div>
  );
}
