import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth } from "./auth";
import { LiveClient, wsUrl, type LiveStatus } from "./live";
import type { LiveEvent } from "./types";

interface LiveState {
  status: LiveStatus;
  events: LiveEvent[];
  unseen: number;
  markSeen: () => void;
}

const Ctx = createContext<LiveState>({ status: "offline", events: [], unseen: 0, markSeen: () => {} });

// Which cached queries a given event type makes stale. Data is always re-read over REST (the source of truth);
// the socket only says *that* something changed, so a dropped event can never leave the UI inconsistent.
const INVALIDATES: Record<string, string[][]> = {
  "anomaly.detected": [["anomalies"], ["overview"], ["services"]],
  "incident.created": [["incidents"], ["overview"]],
  "incident.updated": [["incidents"], ["incident"]],
  "incident.acknowledged": [["incidents"], ["incident"]],
  "incident.investigating": [["incidents"], ["incident"]],
  "incident.resolved": [["incidents"], ["incident"], ["overview"]],
  "investigation.completed": [["rca"], ["incident"], ["incidents"]],
  "fault.injected": [["faults"]],
  "fault.cleared": [["faults"]],
  "service.health_changed": [["services"], ["overview"], ["graph"]],
  "recovery.verified": [["incidents"], ["incident"]],
};

export function LiveProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  const qc = useQueryClient();
  const [status, setStatus] = useState<LiveStatus>("offline");
  const [events, setEvents] = useState<LiveEvent[]>([]);
  const [unseen, setUnseen] = useState(0);
  const clientRef = useRef<LiveClient | null>(null);

  useEffect(() => {
    if (!user) return;
    const c = new LiveClient(wsUrl());
    clientRef.current = c;
    const offStatus = c.onStatus(setStatus);
    const off = c.on((e) => {
      setEvents((prev) => [e, ...prev].slice(0, 100));
      setUnseen((n) => n + 1);
      (INVALIDATES[e.type] ?? []).forEach((key) => qc.invalidateQueries({ queryKey: key }));
    });
    c.start();
    return () => {
      off();
      offStatus();
      c.stop();
    };
  }, [user, qc]);

  const value = useMemo(() => ({ status, events, unseen, markSeen: () => setUnseen(0) }), [status, events, unseen]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export const useLive = () => useContext(Ctx);
