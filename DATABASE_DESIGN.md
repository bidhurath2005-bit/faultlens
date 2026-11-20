# Database Design

PostgreSQL 16 holds FaultLens' **derived, durable state**. Raw telemetry stays in Prometheus/Loki/Tempo
([ADR-009](docs/adr/0009-telemetry-boundaries.md)). The schema is created only by Alembic
(`backend/migrations/versions/0001_initial_schema.py`). `alembic check` verifies the models and migrations
match, and the migration is tested by running upgrade → downgrade → upgrade.

## Entity-relationship diagram

```mermaid
erDiagram
  users ||--o{ refresh_tokens : has
  users ||--o{ audit_events : "acts in"
  users ||--o{ incident_events : "acts in"
  users ||--o{ fault_injections : "injects"
  users ||--o{ app_settings : "last updated"
  services ||--o{ anomalies : "observed on"
  services ||--o{ incident_services : ""
  incidents ||--o{ incident_services : affects
  incidents ||--o{ anomalies : groups
  incidents ||--o{ incident_events : timeline
  incidents ||--o{ investigations : "investigated by"
  investigations ||--o{ root_cause_findings : ranks
  root_cause_findings ||--o{ recommendations : suggests
  services ||--o{ root_cause_findings : "blamed service"

  users {
    int id PK
    varchar username UK
    varchar password_hash
    enum role
    bool is_active
    timestamptz last_login_at
  }
  refresh_tokens {
    uuid id PK
    int user_id FK
    varchar token_hash UK
    uuid family_id
    timestamptz expires_at
    timestamptz revoked_at
  }
  services {
    int id PK
    varchar name UK
    varchar display_name
    text description
    varchar owner_team
    int tier
  }
  incidents {
    int id PK
    varchar title
    enum status
    enum severity
    varchar source
    int primary_service_id FK
    timestamptz opened_at
    timestamptz resolved_at
    timestamptz recovery_verified_at
    int version
  }
  incident_services {
    int incident_id PK, FK
    int service_id PK, FK
  }
  incident_events {
    int id PK
    int incident_id FK
    varchar kind
    text message
    int actor_id FK
    jsonb data
  }
  anomalies {
    int id PK
    int service_id FK
    int incident_id FK
    timestamptz detected_at
    varchar metric
    varchar method
    float score
    enum severity
    float observed_value
    float baseline_value
    jsonb details
  }
  investigations {
    int id PK
    int incident_id FK
    enum status
    timestamptz window_start
    timestamptz window_end
    float duration_ms
    text summary
    jsonb evidence
  }
  root_cause_findings {
    int id PK
    int investigation_id FK
    int rank
    int service_id FK
    varchar cause_type
    float score
    varchar confidence
    jsonb signals
    jsonb evidence
  }
  recommendations {
    int id PK
    int finding_id FK
    int priority
    text action
    text rationale
    varchar risk
  }
  audit_events {
    int id PK
    timestamptz occurred_at
    int actor_id FK
    varchar action
    varchar target_type
    varchar target_id
    varchar outcome
    jsonb details
  }
  fault_injections {
    int id PK
    varchar target
    varchar fault_type
    jsonb params
    timestamptz started_at
    timestamptz expires_at
    timestamptz ended_at
    int actor_id FK
  }
  model_evaluations {
    int id PK
    varchar kind
    varchar detector
    varchar dataset
    text dataset_description
    jsonb metrics
    jsonb params
  }
  app_settings {
    varchar key PK
    jsonb value
    int updated_by_id FK
  }
```

## Constraints and integrity rules

| Table | Rule | Purpose |
|---|---|---|
| users | `UNIQUE(username)`, `CHECK char_length(username) >= 3`, enum role | identity |
| refresh_tokens | `UNIQUE(token_hash)`, FK → users `ON DELETE CASCADE`, index on `family_id` | rotation and family revocation |
| incidents | `CHECK status <> 'resolved' OR resolved_at IS NOT NULL`, `CHECK source IN ('auto','manual')`, `version` column for optimistic locking | lifecycle consistency |
| incident_services | composite PK | no duplicate affected-service links |
| anomalies | `CHECK method IN (threshold, zscore, moving_average, isolation_forest)` | valid detector provenance |
| investigations | `CHECK window_end >= window_start` | valid evidence window |
| root_cause_findings | `UNIQUE(investigation_id, rank)`, `CHECK 0 <= score <= 1` | well-formed ranking |
| model_evaluations | `CHECK kind IN ('anomaly','rca')` | evaluation provenance |
| services | `UNIQUE(name)`, `CHECK tier BETWEEN 1 AND 3` | catalogue |

Deletion behaviour: deleting an incident cascades to its timeline, links and investigations (and from there to
findings and recommendations); anomalies keep existing with `incident_id = NULL`; user deletion nulls actor
references, so audit history is preserved.

## Indexes (query-driven)

| Index | Serves |
|---|---|
| `ix_incidents_status_opened (status, opened_at)` | incident list filtered by status, newest first; correlator's "active incidents" scan |
| `ix_anomalies_service_time (service_id, detected_at)` | per-service anomaly history; recovery check ("any anomaly in the last 90 s?") |
| `ix_anomalies_detected_at` | anomaly list and per-minute trend |
| `ix_anomalies_incident_id` | incident detail |
| `ix_incident_events_incident_time` | incident timeline |
| `ix_investigations_incident_id` | latest investigation per incident |
| `ix_audit_events_occurred_at`, `ix_audit_events_action` | audit log browsing and filtering |
| `ix_fault_injections_active (ended_at, expires_at)` | active fault lookup |

## JSONB usage (deliberately limited)
- `root_cause_findings.signals`: the per-signal score breakdown (name, weight, strength, contribution, value, source).
- `investigations.evidence`: a bounded evidence snapshot (aggregates, ≤ 8 log templates per service, ≤ 6 example traces).
- `anomalies.details`: detector-specific parameters (z, robust scale, EWMA, IF contributions).
- `audit_events.details`, `incident_events.data`, `app_settings.value`, `model_evaluations.metrics/params`.

## Concurrency
- State transitions: `SELECT … FOR UPDATE` on the incident row, then validate the transition, then write; a
  stale write raises `StaleDataError`, which maps to HTTP 409.
- Correlation: the worker locks candidate incidents while attaching an anomaly; investigations use a Redis lock per incident.
- Recovery checks use `FOR UPDATE SKIP LOCKED` so they never block user transitions.

## Demo service data (`shop` database)
Separate database and role. Each service owns a schema (`users`, `orders`, `payments`, `inventory`,
`notifications`) created idempotently at start-up under a PostgreSQL advisory lock. This is business data for
the simulated company, not FaultLens state, so it is intentionally outside Alembic.
