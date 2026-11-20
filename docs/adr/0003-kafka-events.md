# ADR-003: Kafka for asynchronous event processing

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
Several components react to the same operational facts. A detected anomaly must be correlated into an
incident, which triggers an investigation, and every anomaly, incident change, fault-lab action, health change
and recovery must reach live dashboards. Producers (detector, API, correlator, prober) should not know about
consumers, and a slow consumer must not slow detection.

## Decision
Use **Apache Kafka** (single KRaft broker locally) with four explicitly created topics:
`faultlens.anomalies`, `faultlens.incidents`, `faultlens.faults`, `faultlens.health`
(broker auto-creation disabled). Two consumer groups run in the worker:

- `faultlens-correlator`: anomalies → incident creation/attachment → scheduled RCA. Manual offset commit,
  at-least-once, idempotent (an anomaly already attached to an incident is skipped).
- `faultlens-realtime-bridge`: all topics → Redis Pub/Sub for WebSocket fan-out (ADR-008).

Events are published **after** the database transaction commits, so a consumer never sees an event for a row
that does not exist yet. Publishing before commit was a real bug, found during live evaluation: it silently
dropped anomalies and delayed incidents (see BENCHMARKS.md).

## Alternatives considered
- **Redis Streams**: lighter, but Kafka gives durable, replayable logs and independent consumer groups that
  model "many consumers of one fact" directly.
- **Direct calls / DB polling**: couples producers to consumers; polling adds latency and load.
- **RabbitMQ**: strong for work queues; Kafka's log plus consumer groups fit event fan-out and replay better.

## Consequences
- Detection, correlation and real-time delivery scale and fail independently. A Kafka outage does not break
  user actions: state is committed to PostgreSQL first, and publish failures are logged (`emit()`).
- This is not a full transactional outbox: if the process dies between commit and publish, that event is lost.
  The database stays correct and dashboards re-read via REST. Documented limitation.
- Single broker with replication factor 1: suitable for local/demo use only.

## Implementation
`backend/app/events/bus.py`, `backend/app/workers/main.py`.
