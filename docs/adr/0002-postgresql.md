# ADR-002: PostgreSQL for persistent application data

- **Status:** Accepted
- **Date:** 2026-11-19

## Context
FaultLens persists incidents and their timeline, anomalies, investigations with ranked findings and
recommendations, users and refresh tokens, audit events, settings and model-evaluation records. Incident state
changes must stay consistent under concurrency (two engineers acknowledging at once; the worker correlating
anomalies while a user resolves). Evidence snapshots are semi-structured.

## Decision
Use **PostgreSQL 16** with a normalised schema managed exclusively by **Alembic** migrations.
JSONB is used only for genuinely semi-structured payloads (signal breakdowns, evidence snapshots, audit details).

## Alternatives considered
- **MongoDB**: flexible documents, but weaker relational integrity for incident ↔ service ↔ finding links.
- **SQLite**: zero-ops, but poor concurrent writes from several API workers plus the worker process.
- **Storing telemetry in PostgreSQL**: rejected; Prometheus/Loki/Tempo own raw telemetry (ADR-009).

## Consequences
- Foreign keys, CHECK constraints (e.g. `status='resolved' ⇒ resolved_at IS NOT NULL`, `0 ≤ score ≤ 1`),
  unique constraints and indexes enforce invariants in the database, not only in code.
- State transitions take a row lock (`SELECT … FOR UPDATE`) plus an optimistic `version` column, so two
  concurrent transitions cannot both succeed.
- Two databases in one cluster: `faultlens` (platform) and `shop` (demo services), with separate least-privilege roles.

## Implementation
`backend/app/db/models.py`, `backend/migrations/versions/0001_initial_schema.py`, `DATABASE_DESIGN.md`.
