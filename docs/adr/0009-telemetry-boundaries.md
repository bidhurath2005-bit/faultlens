# ADR-009: Prometheus, Loki and Tempo responsibilities and data boundaries

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
Telemetry volume is high and append-only; FaultLens' own state is low-volume and relational.

## Decision

| Store | Owns | Retention (local) | FaultLens queries it for |
|---|---|---|---|
| **Prometheus** | numeric time series: RED metrics, resources, DB pool, probes, servicegraph edges | 3 days | detection features, baselines, RCA metric evidence, dependency graph |
| **Loki** | structured logs: `service_name` label; `trace_id`/`span_id`/`request_id` as structured metadata | 72 h | log search, trace→log lookup, RCA log patterns |
| **Tempo** | distributed traces | 24 h | TraceQL search, waterfall, RCA self-time and error-origin analysis |
| **PostgreSQL** | derived, durable decisions: incidents, anomalies, investigations, findings, users, audit | indefinite | everything else |

FaultLens never copies raw telemetry into PostgreSQL. Each investigation stores a **bounded evidence snapshot**
(aggregates, top log templates, example trace ids), so a report stays readable after raw telemetry expires.

## Consequences
- Each store does what it is good at, and PostgreSQL stays small.
- Trace ids in old investigations can outlive the trace in Tempo. The UI then reports "trace not found (may
  have expired)" rather than failing.
- High-cardinality values (trace ids, request ids) are kept out of Loki labels and stored as structured metadata.

## Implementation
`deploy/*`, `backend/app/telemetry/*`, `backend/app/engines/rca/evidence.py` (`bundle_summary`).
