export type Role = "admin" | "sre" | "viewer";
export type Severity = "critical" | "high" | "medium" | "low";
export type IncidentStatus = "open" | "acknowledged" | "investigating" | "resolved";
export type Health = "healthy" | "degraded" | "critical" | "down" | "unknown" | "external";

export interface User {
  id: number;
  username: string;
  role: Role;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export type MetricMap = Partial<Record<
  "rps" | "error_rate" | "latency_p50" | "latency_p95" | "latency_p99" | "latency_avg" | "cpu" | "memory_rss" |
  "memory_util" | "db_pool_util" | "db_query_p95" | "availability", number>>;

export type Series = [number, number][];

export interface ServiceSummary {
  name: string;
  display_name: string;
  description: string;
  owner_team: string;
  tier: number;
  health: Health;
  metrics: MetricMap;
  dependencies: string[];
  dependents: string[];
  anomalies_1h: number;
}

export interface Anomaly {
  id: number;
  service: string;
  incident_id: number | null;
  detected_at: string;
  metric: string;
  method: "threshold" | "zscore" | "moving_average" | "isolation_forest";
  score: number;
  severity: Severity;
  observed_value: number | null;
  baseline_value: number | null;
  details: Record<string, unknown>;
}

export interface Overview {
  overall_health: Health;
  services_total: number;
  services_reporting: number;
  service_health: Record<string, Health>;
  active_incidents: number;
  active_incidents_by_severity: Partial<Record<Severity, number>>;
  current: Record<string, number | null>;
  series: Record<string, Series>;
  recent_anomalies: Anomaly[];
  generated_at: string;
}

export interface Incident {
  id: number;
  title: string;
  status: IncidentStatus;
  severity: Severity;
  source: "auto" | "manual";
  primary_service: string | null;
  services: string[];
  opened_at: string;
  last_activity_at: string;
  acknowledged_at: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  recovery_verified_at: string | null;
}

export interface TimelineEvent {
  id: number;
  occurred_at: string;
  kind: string;
  message: string;
  actor: string | null;
  data: Record<string, unknown>;
}

export interface IncidentDetail extends Incident {
  timeline: TimelineEvent[];
  anomalies: Anomaly[];
  investigations: { id: number; status: string; started_at: string; summary: string }[];
}

export interface Signal {
  name: string;
  description: string;
  weight: number;
  strength: number;
  contribution: number;
  value: string;
  source: string;
  primary: boolean;
}

export interface Finding {
  id: number;
  rank: number;
  service: string | null;
  cause_type: string;
  title: string;
  score: number;
  confidence: "high" | "medium" | "low" | "insufficient";
  signals: Signal[];
  evidence: { locality: number; locality_reason: string; raw_score: number; related_services: string[]; upstream_affected: string[] };
  recommendations: { priority: number; action: string; rationale: string; risk: string }[];
}

export interface LogRecord {
  timestamp: string;
  service: string;
  level: string;
  message: string;
  trace_id: string | null;
  span_id: string | null;
  request_id: string | null;
  attributes: Record<string, string>;
}

export interface Investigation {
  id: number;
  incident_id: number;
  status: "running" | "completed" | "inconclusive" | "failed";
  trigger: string;
  started_at: string;
  completed_at: string | null;
  window_start: string;
  window_end: string;
  duration_ms: number | null;
  summary: string;
  engine_version: string;
  findings: Finding[];
  evidence: {
    sources?: Record<string, string>;
    metrics?: Record<string, Record<string, { current: number | null; baseline: number | null; ratio: number; series: Series }>>;
    log_highlights?: { service: string; template: string; count: number; sample: LogRecord }[];
    traces?: { analysed: number; latency_share: Record<string, number>; error_origin_share: Record<string, number>;
      examples: { trace_id: string; duration_ms: number; root_service: string; has_error: boolean; error_origins: { service: string; reason: string }[] }[] };
    dependencies?: { nodes: string[]; edges: Edge[] };
    hypotheses_considered?: number;
  };
}

export interface Edge {
  source: string;
  target: string;
  rps: number;
  error_rate: number;
  p95_ms: number | null;
}

export interface Span {
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  service: string;
  name: string;
  kind: string;
  start_time: string;
  start_ns: number;
  duration_ms: number;
  status: "ok" | "error" | "unset";
  status_message: string | null;
  attributes: Record<string, unknown>;
}

export interface TraceSummary {
  trace_id: string;
  root_service: string;
  root_name: string;
  start_time: string;
  duration_ms: number;
  span_count: number;
}

export interface LiveEvent {
  id: string;
  type: string;
  occurred_at: string;
  source: string;
  data: Record<string, unknown>;
}
