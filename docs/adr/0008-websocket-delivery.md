# ADR-008: WebSocket event delivery for real-time dashboards

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
New incidents, anomalies, investigation results, fault-lab actions and recoveries must appear on open
dashboards without a manual refresh, for clients connected to any of several API processes.

## Decision
Kafka (source events) → worker realtime-bridge consumer → **Redis Pub/Sub** → each API process's in-memory
**hub** → authenticated **WebSocket** clients at `/api/v1/ws/events`.

- The first frame must be `{"type":"auth","token":…}` within 5 s. Invalid or expired tokens get close code
  4401; a foreign `Origin` is refused with HTTP 403 before the upgrade.
- The server pings every 20 s. The client watchdog reconnects if the socket is silent for 45 s, using
  exponential backoff with jitter (1 s → 30 s). After a 4401 the client refreshes its token before reconnecting.
- Payloads are sanitised (sensitive keys removed); per-client bounded queues drop events for slow clients.
- The UI treats events as **invalidation signals** and refetches the affected React Query keys over REST.
  REST remains the source of truth for history, filtering and pagination.

## Alternatives considered
- **Server-Sent Events**: simpler, but one-way (no in-band auth or pong), and header-based auth is awkward in browsers.
- **Polling**: simple but laggy and wasteful; kept only as a 15 s background refresh.
- **A Kafka consumer per API process**: each process would need its own consumer group and offsets; Redis Pub/Sub
  is lighter for ephemeral fan-out.

## Consequences
- A missed event (disconnect, slow client) cannot leave the UI inconsistent, because data is re-read via REST.
- At-most-once delivery per connection, with no replay of events missed while disconnected (by design).

## Implementation
`backend/app/events/ws.py`, `frontend/src/lib/live.ts`, `frontend/src/lib/liveContext.tsx`, `backend/tests/test_ws.py`.
