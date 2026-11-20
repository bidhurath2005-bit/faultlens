# ADR-004: Redis for caching, coordination and event fan-out

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
Dashboards issue many identical PromQL queries. The detector needs per-(service, metric) state across cycles
(confirmation streaks, cooldowns) that survives restarts. Only one worker replica should run detection. Login
and fault endpoints need rate limits. WebSocket events must reach clients connected to any API worker process.

## Decision
Use **Redis 7** for five concrete jobs:

1. **Read-through cache** of Prometheus results (5–10 s TTL) and the detector's baseline window (90 s).
2. **Detector state**: confirmation streak counters and per-method cooldown keys.
3. **Leader election**: a `SET NX EX` lease with a Lua renew, so one worker runs the detection loop.
4. **Fixed-window rate limiting** for login, refresh, password change and fault injection.
5. **Pub/Sub**: `faultlens:events` bridges Kafka to every API process's WebSocket hub, and
   `faultlens:settings` hot-reloads detection settings in the worker.

## Alternatives considered
- In-process caches/state: lost on restart and not shared across uvicorn workers or replicas.
- PostgreSQL advisory locks / LISTEN-NOTIFY: workable, but adds load to the system of record.

## Consequences
- Cache and rate limiting **fail open**: a Redis outage degrades performance, never correctness or availability.
- Pub/Sub is fire-and-forget: a client disconnected at publish time misses that event. The UI therefore treats
  events as "something changed" signals and re-reads state via REST (ADR-008).
- Fixed windows allow bursts of up to 2× the limit across a window boundary; acceptable for this use.

## Implementation
`backend/app/core/redis.py`, `backend/app/engines/anomaly/pipeline.py` (`Confirmer`), `backend/app/events/ws.py`.
