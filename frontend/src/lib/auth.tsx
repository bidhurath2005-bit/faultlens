import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, post, refreshToken, setToken } from "./api";
import type { Role, User } from "./types";

interface AuthState {
  user: User | null;
  ready: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  can: (perm: Permission) => boolean;
}

export type Permission = "read" | "incident:write" | "investigation:run" | "fault:inject" | "settings:write" | "users:manage" | "audit:read";

// Mirrors backend ROLE_PERMISSIONS for UI affordances only. The backend enforces every permission.
const ROLE_PERMS: Record<Role, Permission[]> = {
  viewer: ["read"],
  sre: ["read", "incident:write", "investigation:run", "fault:inject"],
  admin: ["read", "incident:write", "investigation:run", "fault:inject", "settings:write", "users:manage", "audit:read"],
};

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    // Restore a session from the HttpOnly refresh cookie (if any).
    refreshToken()
      .then(async (t) => {
        if (t) setUser(await api<User>("/auth/me"));
      })
      .finally(() => setReady(true));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const body = await post<{ access_token: string; user: User }>("/auth/login", { username, password });
    setToken(body.access_token);
    setUser(body.user);
  }, []);

  const logout = useCallback(async () => {
    try {
      await post("/auth/logout");
    } finally {
      setToken(null);
      setUser(null);
    }
  }, []);

  const can = useCallback((perm: Permission) => (user ? ROLE_PERMS[user.role].includes(perm) : false), [user]);

  const value = useMemo(() => ({ user, ready, login, logout, can }), [user, ready, login, logout, can]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
