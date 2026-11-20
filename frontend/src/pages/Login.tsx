import { Radar } from "lucide-react";
import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function LoginPage() {
  const { login, user } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) return <Navigate to="/" replace />;

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      nav((loc.state as { from?: string } | null)?.from ?? "/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 429 ? err.message : "Invalid username or password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-full grid place-items-center px-4">
      <form onSubmit={submit} className="panel w-full max-w-[360px] p-6" aria-label="sign in">
        <div className="flex items-center gap-2 mb-1">
          <Radar className="text-lens" size={22} aria-hidden />
          <span className="text-[17px] font-semibold tracking-tight">FaultLens</span>
        </div>
        <p className="text-muted mb-5">Observability & root-cause analysis</p>
        <label className="block text-[12px] text-muted mb-1" htmlFor="u">Username</label>
        <input id="u" className="input w-full mb-3" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required />
        <label className="block text-[12px] text-muted mb-1" htmlFor="p">Password</label>
        <input id="p" type="password" className="input w-full mb-4" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {error && <p role="alert" className="text-bad mb-3">{error}</p>}
        <button className="btn-primary w-full justify-center" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <p className="text-faint text-[12px] mt-4">The first admin password is generated into your local <span className="font-mono">.env</span> by the setup script.</p>
      </form>
    </div>
  );
}
