# ADR-001: FastAPI for the backend and demo services

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
FaultLens needs an HTTP API (REST + WebSocket), async I/O to fan out to Prometheus, Loki, Tempo, PostgreSQL,
Redis and Kafka concurrently, request validation, and generated API documentation. The intelligence code
(NumPy, pandas, scikit-learn) is Python, and the demo microservices must be instrumented with OpenTelemetry.

## Decision
Use **FastAPI** (Starlette + Pydantic v2) on **uvicorn** for the platform API and for all five demo services.
Async SQLAlchemy 2 + asyncpg for the database; httpx for outbound HTTP.

## Alternatives considered
- **Django + DRF**: mature admin/ORM, but sync-first; async views and the async ORM are partial; WebSockets need Channels.
- **Flask**: minimal and sync; needs extra libraries for validation, OpenAPI and WebSockets.
- **Go (Gin/Echo)**: excellent performance, but the ML/analysis code would live in a second language and runtime.

## Consequences
- One language across API, worker, ML pipeline and services; Pydantic models validate every input.
- OpenAPI is generated (`/api/docs`), so API docs cannot drift from the code.
- Async everywhere: evidence collection queries the four telemetry backends concurrently (`asyncio.gather`),
  giving sub-second investigations. CPU-heavy work (Isolation Forest fitting) must stay small to avoid blocking the loop.
- Python throughput per process is modest; the API runs 2 uvicorn workers, and real-time fan-out across them
  is solved with Redis Pub/Sub (ADR-008).

## Implementation
`backend/app/main.py`, `backend/app/api/v1/*`, `services/common/faultlens_common/app.py`.
