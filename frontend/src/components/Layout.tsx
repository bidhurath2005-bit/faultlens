import clsx from "clsx";
import {
  Activity, AlertOctagon, Boxes, FlaskConical, GitBranch, LayoutDashboard, LogOut, Radar, ScrollText, Search, Settings, Waypoints,
} from "lucide-react";
import { useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { clock, humanize } from "../lib/format";
import { useLive } from "../lib/liveContext";

const NAV = [
  { to: "/", label: "Overview", Icon: LayoutDashboard, end: true },
  { to: "/services", label: "Services", Icon: Boxes },
  { to: "/traces", label: "Tracing", Icon: Waypoints },
  { to: "/logs", label: "Logs", Icon: ScrollText },
  { to: "/anomalies", label: "Anomalies", Icon: Activity },
  { to: "/rca", label: "Root Cause", Icon: Search },
  { to: "/graph", label: "Dependencies", Icon: GitBranch },
  { to: "/incidents", label: "Incidents", Icon: AlertOctagon },
  { to: "/lab", label: "Fault Lab", Icon: FlaskConical },
  { to: "/settings", label: "Settings", Icon: Settings },
];

function LiveIndicator() {
  const { status, events, unseen, markSeen } = useLive();
  const [open, setOpen] = useState(false);
  const tone = status === "live" ? "bg-ok" : status === "offline" ? "bg-faint" : "bg-warn";
  return (
    <div className="relative">
      <button className="btn-ghost h-8" onClick={() => { setOpen((o) => !o); markSeen(); }} aria-label="live events" aria-expanded={open}>
        <span className={clsx("h-2 w-2 rounded-full", tone)} aria-hidden />
        <span className="text-muted">{status === "live" ? "Live" : humanize(status)}</span>
        {unseen > 0 && <span className="num rounded-full bg-lens/20 text-lens px-1.5 text-2xs">{unseen}</span>}
      </button>
      {open && (
        <div className="absolute right-0 mt-2 w-[380px] max-h-[420px] overflow-auto panel shadow-2xl z-40">
          <div className="panel-head"><span className="panel-title">Live event stream</span></div>
          {events.length === 0 ? (
            <p className="p-4 text-muted">No events since you connected. Events arrive here as they happen.</p>
          ) : (
            <ul>
              {events.map((e) => (
                <li key={e.id} className="px-4 py-2 border-b border-line/60">
                  <div className="flex justify-between gap-2">
                    <span className="font-mono text-[12px] text-lens">{e.type}</span>
                    <span className="num text-faint text-[11px]">{clock(e.occurred_at)}</span>
                  </div>
                  <div className="text-muted text-[12px] truncate">{describe(e.data)}</div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function describe(d: Record<string, unknown>): string {
  const inc = d.incident as { id?: number; title?: string } | undefined;
  if (inc) return `#${inc.id} ${inc.title ?? ""}`;
  if (d.service && d.metric) return `${d.service} · ${d.metric} · ${d.method}`;
  if (d.summary) return String(d.summary);
  if (d.target) return `${d.target} · ${d.type}`;
  if (d.service) return `${d.service} ${d.up === false ? "DOWN" : d.up ? "UP" : ""}`;
  return JSON.stringify(d).slice(0, 100);
}

export function Layout() {
  const { user, logout } = useAuth();
  return (
    <div className="flex h-full">
      <aside className="w-[212px] shrink-0 border-r border-line bg-surface flex flex-col">
        <div className="flex items-center gap-2 px-4 h-14 border-b border-line">
          <Radar className="text-lens" size={20} aria-hidden />
          <span className="font-semibold tracking-tight text-[15px]">FaultLens</span>
        </div>
        <nav className="flex-1 overflow-auto py-2" aria-label="primary">
          {NAV.map(({ to, label, Icon, end }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) => clsx("flex items-center gap-2.5 mx-2 px-2.5 h-8 rounded-md text-[13px]",
                isActive ? "bg-lens/12 text-ink font-medium" : "text-muted hover:text-ink hover:bg-raised")}>
              {({ isActive }) => (<><Icon size={16} className={isActive ? "text-lens" : ""} aria-hidden />{label}</>)}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-line p-3 text-[12px]">
          <div className="flex items-center justify-between">
            <div className="min-w-0">
              <div className="font-medium truncate">{user?.username}</div>
              <div className="text-faint uppercase tracking-wider text-2xs">{user?.role === "sre" ? "SRE Engineer" : user?.role}</div>
            </div>
            <button className="btn-ghost h-7 px-2" onClick={() => logout()} aria-label="sign out" title="Sign out"><LogOut size={14} /></button>
          </div>
        </div>
      </aside>
      <div className="flex-1 min-w-0 flex flex-col">
        <header className="h-14 shrink-0 border-b border-line flex items-center justify-end gap-3 px-6 bg-surface/60">
          <LiveIndicator />
        </header>
        <main className="flex-1 overflow-auto px-6 py-5">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
