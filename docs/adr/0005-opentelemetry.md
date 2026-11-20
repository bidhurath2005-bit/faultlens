# ADR-005: OpenTelemetry-based instrumentation and telemetry architecture

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
RCA needs metrics, logs and traces that can be joined on service identity, time and trace id. Services must
propagate context across HTTP hops, and the storage backends should be swappable.

## Decision
Instrument every service with the **OpenTelemetry Python SDK** and export all three signals over **OTLP/gRPC**
to one **OpenTelemetry Collector**, which routes traces → Tempo, metrics → Prometheus exporter (scraped), and
logs → Loki's native OTLP endpoint. Context propagation uses W3C `traceparent`/`baggage` (FastAPI, httpx and
SQLAlchemy instrumentation). The Collector's **servicegraph connector** derives caller→callee metrics from
client/server span pairs.

Application metrics are explicit instruments with fixed names and explicit histogram buckets
(`faultlens.http.server.duration`, `faultlens.db.pool.wait`, `faultlens.db.query.duration`, …), defined in one
module and consumed by one PromQL catalogue (`backend/app/telemetry/promql.py`).

Structured JSON logs go to stdout *and* through the OTLP log bridge, which attaches the active span's
`trace_id`/`span_id`. If no valid span context exists, the fields are omitted, never invented.

## Alternatives considered
- Prometheus client libraries + promtail + Jaeger clients: three agents and three context models, with no shared resource identity.
- Vendor APM agents: neither free nor local.

## Consequences
- One resource identity (`service.name`) across signals makes correlation exact.
- Explicit instruments mean we know precisely what is measured (pool wait vs query time), which the RCA
  discriminators depend on.
- The Collector is a single ingestion point locally (no HA). Metric names depend on the Collector's Prometheus
  exporter conventions (unit suffixes), so the image version is pinned.

## Implementation
`services/common/faultlens_common/telemetry.py`, `deploy/otel-collector/config.yaml`.
