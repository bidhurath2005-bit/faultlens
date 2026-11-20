import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CircleDashed, Inbox, Loader2, OctagonAlert, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import { ApiError } from "../lib/api";
import type { Health, IncidentStatus, Severity } from "../lib/types";

export function Panel({ title, actions, children, className, bodyClass }: {
  title?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; bodyClass?: string;
}) {
  return (
    <section className={clsx("panel", className)}>
      {(title || actions) && (
        <header className="panel-head">
          {title && <h2 className="panel-title">{title}</h2>}
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={clsx(bodyClass ?? "p-4")}>{children}</div>
    </section>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3 mb-5">
      <div>
        <h1 className="text-[20px] font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="text-muted mt-0.5">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </div>
  );
}

const HEALTH: Record<Health, { cls: string; label: string; Icon: typeof CheckCircle2 }> = {
  healthy: { cls: "text-ok", label: "Healthy", Icon: CheckCircle2 },
  degraded: { cls: "text-warn", label: "Degraded", Icon: AlertTriangle },
  critical: { cls: "text-bad", label: "Critical", Icon: OctagonAlert },
  down: { cls: "text-bad", label: "Down", Icon: XCircle },
  unknown: { cls: "text-faint", label: "No data", Icon: CircleDashed },
  external: { cls: "text-faint", label: "External", Icon: CircleDashed },
};

/** Status always ships icon + label, never colour alone. */
export function HealthBadge({ health, compact }: { health: Health; compact?: boolean }) {
  const h = HEALTH[health] ?? HEALTH.unknown;
  return (
    <span className={clsx("inline-flex items-center gap-1.5 font-medium", h.cls)} title={h.label}>
      <h.Icon size={14} aria-hidden />
      {!compact && <span>{h.label}</span>}
      {compact && <span className="sr-only">{h.label}</span>}
    </span>
  );
}

const SEV: Record<Severity, string> = {
  critical: "bg-bad/15 text-bad border-bad/40",
  high: "bg-warn/15 text-warn border-warn/40",
  medium: "bg-info/12 text-info border-info/35",
  low: "bg-raised text-muted border-line",
};

export function SeverityBadge({ severity }: { severity: Severity }) {
  return (
    <span className={clsx("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wider border", SEV[severity])}>
      {severity === "critical" && <OctagonAlert size={11} aria-hidden />}
      {severity}
    </span>
  );
}

const STATUS: Record<IncidentStatus, string> = {
  open: "text-bad border-bad/40",
  acknowledged: "text-warn border-warn/40",
  investigating: "text-info border-info/40",
  resolved: "text-ok border-ok/40",
};

export function StatusBadge({ status }: { status: IncidentStatus }) {
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-2xs font-semibold uppercase tracking-wider", STATUS[status])}>
      <span className={clsx("h-1.5 w-1.5 rounded-full bg-current", status === "open" && "pulse-bad")} aria-hidden />
      {status}
    </span>
  );
}

export function Chip({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx("inline-flex items-center rounded border border-line bg-raised px-1.5 py-0.5 text-2xs text-muted", className)}>{children}</span>;
}

export function Mono({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={clsx("font-mono text-[12px]", className)}>{children}</span>;
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="flex items-center justify-center gap-2 py-10 text-muted">
      <Loader2 className="animate-spin" size={16} aria-hidden />
      {label}…
    </div>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded bg-raised", className)} />;
}

export function Empty({ title, hint, icon }: { title: string; hint?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 py-10 text-center">
      <div className="text-faint">{icon ?? <Inbox size={22} aria-hidden />}</div>
      <p className="font-medium">{title}</p>
      {hint && <p className="text-muted max-w-md">{hint}</p>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const msg = error instanceof ApiError ? `${error.message} (${error.status})` : error instanceof Error ? error.message : "Unknown error";
  return (
    <div role="alert" className="flex flex-col items-center gap-2 py-8 text-center">
      <XCircle className="text-bad" size={22} aria-hidden />
      <p className="font-medium">Could not load data</p>
      <p className="text-muted">{msg}</p>
      {onRetry && <button className="btn-ghost mt-1" onClick={onRetry}>Retry</button>}
    </div>
  );
}

/** Wraps a React Query result with consistent loading / error / empty handling. */
export function QueryState<T>({ q, empty, children, isEmpty }: {
  q: { isLoading: boolean; isError: boolean; error: unknown; data: T | undefined; refetch: () => unknown };
  empty?: ReactNode; isEmpty?: (d: T) => boolean; children: (d: T) => ReactNode;
}) {
  if (q.isLoading) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  if (q.data === undefined) return null;
  if (isEmpty && isEmpty(q.data)) return <>{empty ?? <Empty title="Nothing here yet" />}</>;
  return <>{children(q.data)}</>;
}

export function Stat({ label, value, sub, tone, icon }: {
  label: string; value: ReactNode; sub?: ReactNode; tone?: "ok" | "warn" | "bad"; icon?: ReactNode;
}) {
  return (
    <div className="panel px-4 py-3.5 min-w-0">
      <div className="flex items-center justify-between text-muted text-[12px]">
        <span className="truncate">{label}</span>
        {icon}
      </div>
      <div className={clsx("num text-[22px] font-medium mt-1 leading-tight truncate",
        tone === "bad" && "text-bad", tone === "warn" && "text-warn", tone === "ok" && "text-ok")}>{value}</div>
      {sub && <div className="text-faint text-[12px] mt-0.5 truncate">{sub}</div>}
    </div>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label }: {
  value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md border border-line bg-base p-0.5">
      {options.map((o) => (
        <button key={o.value} role="radio" aria-checked={value === o.value} onClick={() => onChange(o.value)}
          className={clsx("h-7 px-2.5 rounded text-[12px] font-medium", value === o.value ? "bg-raised text-ink" : "text-muted hover:text-ink")}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Pager({ total, limit, offset, onChange }: { total: number; limit: number; offset: number; onChange: (o: number) => void }) {
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="flex items-center justify-between px-3 py-2 text-muted text-[12px]">
      <span className="num">{total === 0 ? "0 results" : `${offset + 1}–${Math.min(offset + limit, total)} of ${total}`}</span>
      <div className="flex gap-1.5">
        <button className="btn-ghost h-7" disabled={page <= 1} onClick={() => onChange(Math.max(0, offset - limit))}>Prev</button>
        <button className="btn-ghost h-7" disabled={page >= pages} onClick={() => onChange(offset + limit)}>Next</button>
      </div>
    </div>
  );
}

export function KV({ k, children }: { k: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 border-b border-line/60 last:border-0">
      <dt className="text-muted">{k}</dt>
      <dd className="text-right min-w-0 truncate">{children}</dd>
    </div>
  );
}
