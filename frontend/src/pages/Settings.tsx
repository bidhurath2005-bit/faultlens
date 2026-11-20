import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Chip, ErrorState, KV, PageHeader, Panel, QueryState, Segmented } from "../components/ui";
import { api, patch, post, put } from "../lib/api";
import { useAuth } from "../lib/auth";
import { relTime } from "../lib/format";
import type { Page, Role, User } from "../lib/types";

interface SettingsResp {
  detection: Record<string, unknown> & { thresholds: Record<string, number>; enabled_methods: string[] };
  preferences: Record<string, unknown>;
  telemetry: Record<string, string | number>;
  services: string[];
  ai: { provider: string; configured_provider: string; model: string | null; api_key_configured: boolean };
  fault_injection: { enabled: boolean; environment: string };
  auth: { access_token_ttl_minutes: number; refresh_token_ttl_hours: number };
}

const NUMERIC = ["zscore_threshold", "ewma_alpha", "ewma_ratio_threshold", "iforest_threshold", "min_baseline_points", "confirm_cycles", "cooldown_s"];
const METHODS = ["threshold", "zscore", "moving_average", "isolation_forest"];

export default function SettingsPage() {
  const { can } = useAuth();
  const q = useQuery({ queryKey: ["settings"], queryFn: () => api<SettingsResp>("/settings"), refetchInterval: false });
  return (
    <div>
      <PageHeader title="Settings" subtitle={can("settings:write") ? "Changes are validated, audited and hot-reloaded by the detector" : "Read-only for your role"} />
      <QueryState q={q}>
        {(s) => (
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
            <DetectionForm s={s} />
            <div className="space-y-4">
              <Preferences />
              <Panel title="Telemetry">
                <dl>{Object.entries(s.telemetry).map(([k, v]) => <KV key={k} k={k}><span className="font-mono text-[12px]">{String(v)}</span></KV>)}</dl>
              </Panel>
              <Panel title="Services & fault injection">
                <dl>
                  <KV k="Monitored services">{s.services.join(", ")}</KV>
                  <KV k="Fault injection">{s.fault_injection.enabled ? <span className="text-warn">enabled ({s.fault_injection.environment})</span> : "disabled"}</KV>
                  <KV k="Access token TTL">{s.auth.access_token_ttl_minutes} min</KV>
                  <KV k="Refresh token TTL">{s.auth.refresh_token_ttl_hours} h (rotated on use)</KV>
                </dl>
              </Panel>
              <Panel title="AI explanation provider">
                <dl>
                  <KV k="Active provider"><span className="font-mono text-[12px]">{s.ai.provider}</span></KV>
                  <KV k="Configured">{s.ai.configured_provider}</KV>
                  <KV k="Model">{s.ai.model ?? "—"}</KV>
                  <KV k="API key">{s.ai.api_key_configured ? "configured (hidden)" : "not set"}</KV>
                </dl>
                <p className="text-faint text-[12px] mt-2">Provider and keys are set via environment variables (LLM_PROVIDER, LLM_API_KEY, LLM_MODEL, LLM_BASE_URL) and are never exposed by the API. The deterministic narrator works without any key.</p>
              </Panel>
            </div>
            {can("users:manage") && <Users />}
            {can("audit:read") && <Audit />}
          </div>
        )}
      </QueryState>
    </div>
  );
}

function DetectionForm({ s }: { s: SettingsResp }) {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [cfg, setCfg] = useState(s.detection);
  useEffect(() => setCfg(s.detection), [s.detection]);
  const save = useMutation({ mutationFn: () => put("/settings", { detection: cfg }), onSuccess: () => qc.invalidateQueries({ queryKey: ["settings"] }) });
  const ro = !can("settings:write");
  const submit = (e: FormEvent) => { e.preventDefault(); save.mutate(); };
  return (
    <Panel title="Anomaly detection">
      <form onSubmit={submit}>
        <fieldset disabled={ro}>
          <div className="text-2xs uppercase tracking-wider text-faint mb-2">Static thresholds</div>
          <div className="grid grid-cols-2 gap-3 mb-4">
            {Object.entries(cfg.thresholds).map(([k, v]) => (
              <label key={k} className="text-[12px] text-muted">{k}
                <input type="number" step="any" className="input w-full mt-1 num" value={v}
                  onChange={(e) => setCfg({ ...cfg, thresholds: { ...cfg.thresholds, [k]: Number(e.target.value) } })} /></label>
            ))}
          </div>
          <div className="text-2xs uppercase tracking-wider text-faint mb-2">Statistical / ML</div>
          <div className="grid grid-cols-2 gap-3 mb-4">
            {NUMERIC.map((k) => (
              <label key={k} className="text-[12px] text-muted">{k}
                <input type="number" step="any" className="input w-full mt-1 num" value={cfg[k] as number}
                  onChange={(e) => setCfg({ ...cfg, [k]: Number(e.target.value) })} /></label>
            ))}
          </div>
          <div className="text-2xs uppercase tracking-wider text-faint mb-2">Enabled methods</div>
          <div className="flex flex-wrap gap-2 mb-4">
            {METHODS.map((m) => {
              const on = cfg.enabled_methods.includes(m);
              return <label key={m} className="flex items-center gap-1.5"><input type="checkbox" checked={on}
                onChange={() => setCfg({ ...cfg, enabled_methods: on ? cfg.enabled_methods.filter((x) => x !== m) : [...cfg.enabled_methods, m] })} /><span className="font-mono text-[12px]">{m}</span></label>;
            })}
          </div>
        </fieldset>
        {save.isError && <ErrorState error={save.error} />}
        {!ro && <button className="btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : "Save detection settings"}</button>}
        {save.isSuccess && <span className="text-ok ml-3">Saved</span>}
      </form>
    </Panel>
  );
}

function Preferences() {
  const [theme, setTheme] = useState<"dark" | "light">(() => (document.documentElement.dataset.theme as "dark" | "light") ?? "dark");
  const apply = (t: "dark" | "light") => {
    setTheme(t);
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("fl-theme", t); } catch { /* per-browser convenience only */ }
  };
  return (
    <Panel title="Application preferences">
      <div className="flex items-center justify-between"><span className="text-muted">Theme</span>
        <Segmented label="theme" value={theme} onChange={apply} options={[{ value: "dark", label: "Dark" }, { value: "light", label: "Light" }]} /></div>
      <ChangePassword />
    </Panel>
  );
}

function ChangePassword() {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const m = useMutation({ mutationFn: () => post("/auth/password", { current_password: cur, new_password: next }), onSuccess: () => { setCur(""); setNext(""); } });
  return (
    <form className="mt-4 grid grid-cols-[1fr_1fr_auto] gap-2 items-end" onSubmit={(e) => { e.preventDefault(); m.mutate(); }}>
      <label className="text-[12px] text-muted">Current password<input type="password" className="input w-full mt-1" value={cur} onChange={(e) => setCur(e.target.value)} autoComplete="current-password" required /></label>
      <label className="text-[12px] text-muted">New password (12+)<input type="password" className="input w-full mt-1" value={next} onChange={(e) => setNext(e.target.value)} minLength={12} autoComplete="new-password" required /></label>
      <button className="btn-ghost" disabled={m.isPending}>Change</button>
      {m.isError && <div className="col-span-3"><ErrorState error={m.error} /></div>}
      {m.isSuccess && <p className="col-span-3 text-ok">Password changed. Other sessions were signed out.</p>}
    </form>
  );
}

function Users() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["users"], queryFn: () => api<Page<User>>("/users") });
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const create = useMutation({ mutationFn: () => post("/users", { username, password, role }),
    onSuccess: () => { setUsername(""); setPassword(""); qc.invalidateQueries({ queryKey: ["users"] }); } });
  const update = useMutation({ mutationFn: ({ id, body }: { id: number; body: Record<string, unknown> }) => patch(`/users/${id}`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }) });
  return (
    <Panel title="Users & roles (admin)" bodyClass="p-0">
      <QueryState q={q}>
        {(d) => (
          <table className="w-full"><thead><tr>{["User", "Role", "Status", "Last login", ""].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>{d.items.map((u) => (
              <tr key={u.id}>
                <td className="td">{u.username}</td>
                <td className="td"><select className="input h-7" value={u.role} onChange={(e) => update.mutate({ id: u.id, body: { role: e.target.value } })} aria-label={`role for ${u.username}`}>
                  <option value="admin">admin</option><option value="sre">sre</option><option value="viewer">viewer</option></select></td>
                <td className="td">{u.is_active ? <Chip className="text-ok border-ok/40">active</Chip> : <Chip>disabled</Chip>}</td>
                <td className="td text-muted num">{relTime(u.last_login_at)}</td>
                <td className="td text-right"><button className="btn-ghost h-7" onClick={() => update.mutate({ id: u.id, body: { is_active: !u.is_active } })}>{u.is_active ? "Disable" : "Enable"}</button></td>
              </tr>))}</tbody></table>
        )}
      </QueryState>
      {update.isError && <ErrorState error={update.error} />}
      <form className="grid grid-cols-[1fr_1fr_110px_auto] gap-2 p-4 border-t border-line items-end" onSubmit={(e) => { e.preventDefault(); create.mutate(); }}>
        <label className="text-[12px] text-muted">Username<input className="input w-full mt-1" value={username} onChange={(e) => setUsername(e.target.value)} pattern="[a-zA-Z0-9_.\-]+" minLength={3} required /></label>
        <label className="text-[12px] text-muted">Password (12+)<input type="password" className="input w-full mt-1" value={password} onChange={(e) => setPassword(e.target.value)} minLength={12} required autoComplete="new-password" /></label>
        <label className="text-[12px] text-muted">Role<select className="input w-full mt-1" value={role} onChange={(e) => setRole(e.target.value as Role)}>
          <option value="viewer">viewer</option><option value="sre">sre</option><option value="admin">admin</option></select></label>
        <button className="btn-primary" disabled={create.isPending}>Add user</button>
        {create.isError && <div className="col-span-4"><ErrorState error={create.error} /></div>}
      </form>
    </Panel>
  );
}

function Audit() {
  const q = useQuery({ queryKey: ["audit"], queryFn: () => api<{ items: { id: number; occurred_at: string; actor: string; action: string; target_type: string | null; target_id: string | null; outcome: string }[] }>("/audit", { query: { limit: 25 } }) });
  return (
    <Panel title="Audit log (admin)" bodyClass="p-0">
      <QueryState q={q}>
        {(d) => (
          <table className="w-full text-[12px]"><thead><tr>{["When", "Actor", "Action", "Target", "Outcome"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
            <tbody>{d.items.map((a) => (
              <tr key={a.id}><td className="td num text-muted">{relTime(a.occurred_at)}</td><td className="td">{a.actor}</td><td className="td font-mono">{a.action}</td>
                <td className="td text-muted">{a.target_type ? `${a.target_type}:${a.target_id}` : "—"}</td>
                <td className={`td ${a.outcome === "success" ? "text-ok" : "text-bad"}`}>{a.outcome}</td></tr>))}</tbody></table>
        )}
      </QueryState>
    </Panel>
  );
}
