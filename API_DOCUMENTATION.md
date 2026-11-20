# API Documentation

The authoritative, always-current reference is the generated OpenAPI spec:

- Swagger UI: <http://localhost:8000/api/docs>
- ReDoc: <http://localhost:8000/api/redoc>
- JSON: <http://localhost:8000/api/openapi.json> (a snapshot is committed at `docs/openapi.json`)

Base path: `/api/v1`. Through the UI's nginx (<http://localhost:3080>) the same paths are proxied same-origin.

## Conventions
- **Auth**: `Authorization: Bearer <access_token>` (from `/auth/login` or `/auth/refresh`).
- **Errors** always look like `{"error": {"code": "…", "message": "…", "request_id": "…", "details"?: […]}}`.
  Codes: `unauthorized` 401, `forbidden` 403, `not_found` 404, `conflict` / `invalid_transition` 409,
  `validation_error` / `invalid_setting` / `weak_password` 422, `rate_limited` 429, `dependency_unavailable` 503,
  `internal_error` 500.
- **Pagination**: `limit` + `offset`; responses include `total`, `limit`, `offset`.
- **Request id**: send `X-Request-ID` (alphanumeric, ≤ 64 chars) or one is generated; it is echoed in the response.
- **Times** are ISO-8601 UTC.

## Permissions

| Permission | Viewer | SRE Engineer | Admin |
|---|:-:|:-:|:-:|
| `read` (dashboards, telemetry, incidents, RCA, assistant) | ✓ | ✓ | ✓ |
| `incident:write` (create, acknowledge, investigate, resolve) | | ✓ | ✓ |
| `investigation:run` (re-run RCA) | | ✓ | ✓ |
| `fault:inject` (Fault Lab, local demo only) | | ✓ | ✓ |
| `settings:write`, `users:manage`, `audit:read` | | | ✓ |

## Endpoints

### Health & auth
| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | none | Platform dependency health (postgres, redis, prometheus, loki, tempo); no data exposed |
| POST | `/auth/login` | none (rate-limited) | `{username, password}` → `{access_token, expires_in, user}` + refresh cookie |
| POST | `/auth/refresh` | refresh cookie | Rotates the refresh token, returns a new access token |
| POST | `/auth/logout` | refresh cookie | Revokes the token family; clears the cookie |
| GET | `/auth/me` | bearer | Current user |
| POST | `/auth/password` | bearer | `{current_password, new_password}`; revokes other sessions |

### Users & audit (Admin)
| Method | Path | Description |
|---|---|---|
| GET | `/users` | List users (paginated) |
| POST | `/users` | `{username, password, role}` |
| PATCH | `/users/{id}` | `{role?, is_active?, password?}`; revokes the user's sessions |
| GET | `/audit?action=` | Audit log |

### Observability (read)
| Method | Path | Notes |
|---|---|---|
| GET | `/services` | All services with health, current metrics, observed dependencies, 1 h anomaly count |
| GET | `/services/{name}?minutes=30` | Detail with time series, status-class breakdown, recent anomalies |
| GET | `/metrics/overview?minutes=30` | Platform KPIs + series, service health, active incidents, recent anomalies |
| GET | `/metrics/services?minutes=&features=rps,error_rate,latency_p95` | Per-service current values + series |
| GET | `/dependencies?window=5m` | Observed graph: nodes (health, metrics) and edges (rps, error rate, p95) |
| GET | `/traces?service=&status=any\|error\|ok&min_duration_ms=&minutes=&limit=` | TraceQL search (query echoed) |
| GET | `/traces/{trace_id}` | Spans, per-service breakdown, self-time and error-origin analysis |
| GET | `/logs?service=&level=ERROR,WARNING&q=&trace_id=&minutes=&start=&end=&limit=` | Loki search (query echoed); `trace_id`/`span_id` only when present on the record |

### Anomalies, incidents, RCA, assistant
| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/anomalies?service=&method=&severity=&since=&incident_id=&limit=&offset=` | read | |
| GET | `/anomalies/stats?hours=24` | read | counts by method and metric; per-minute trend |
| GET | `/incidents?status=&severity=&service=&since=&limit=&offset=` | read | |
| POST | `/incidents` | incident:write | `{title, severity, services[], description?}` |
| GET | `/incidents/{id}` | read | incident + timeline + anomalies + investigations |
| POST | `/incidents/{id}/acknowledge` | incident:write | `open → acknowledged` |
| POST | `/incidents/{id}/investigate` | incident:write | `acknowledged → investigating` |
| POST | `/incidents/{id}/resolve` | incident:write | `{note}`; from `acknowledged`/`investigating` |
| GET | `/rca` | read | recent investigations with top cause |
| GET | `/rca/{incident_id}` | read | latest investigation (findings, signals, recommendations, evidence) |
| GET | `/rca/investigations/{id}` | read | a specific investigation |
| POST | `/rca/{incident_id}/run` | investigation:run | run a new investigation now |
| POST | `/assistant/incidents/{incident_id}` | read | `{question?}` → `{text, provider, validated, notes, facts}` |

### Fault Injection Lab
| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/fault-injection/scenarios` | read | presets, targets, limits, whether enabled |
| GET | `/fault-injection` | read | active faults per service + traffic-generator state |
| POST | `/fault-injection` | fault:inject | `{target, type, duration_s≤900, latency_ms≤10000, db_delay_ms≤5000, error_rate, cpu_threads≤2, memory_mb≤384, multiplier≤20}` |
| POST | `/fault-injection/reset` | fault:inject | restore everything |
| GET | `/fault-injection/history` | read | |

Fault `type`: `latency`, `error_500`, `unavailable`, `db_delay`, `pool_exhaustion`, `cpu_burn`,
`memory_pressure`, `traffic_spike` (target `traffic-generator`). All return 403 `fault_injection_disabled`
outside local/demo environments.

### Settings & evaluations
| Method | Path | Permission |
|---|---|---|
| GET | `/settings` | read (secrets never returned) |
| PUT | `/settings` | settings:write; `{detection?: {...}, preferences?: {...}}`, deep-merged and bounds-checked |
| GET | `/evaluations?kind=anomaly\|rca` | read |

## WebSocket: `/api/v1/ws/events`

```text
client → {"type":"auth","token":"<access token>"}     (first frame, within 5 s)
server → {"type":"auth_ok","user":"…","role":"…","token_expires_at":…}
server → {"type":"event","event":{"id","type","occurred_at","source","data"}}
server → {"type":"ping","ts":…}            client → {"type":"pong"}
close 4401 = unauthenticated / token expired;  handshake 403 = Origin not allowed
```

Event types: `anomaly.detected`, `incident.created|updated|acknowledged|investigating|resolved`,
`investigation.completed`, `fault.injected`, `fault.cleared`, `service.health_changed`, `recovery.verified`.

## Example session (PowerShell)

```powershell
$pw = (Select-String -Path .env -Pattern '^BOOTSTRAP_ADMIN_PASSWORD=').Line.Split('=',2)[1]
$tok = (Invoke-RestMethod -Method Post http://localhost:8000/api/v1/auth/login `
        -ContentType application/json -Body (@{username='admin'; password=$pw} | ConvertTo-Json)).access_token
$h = @{Authorization = "Bearer $tok"}
Invoke-RestMethod http://localhost:8000/api/v1/services -Headers $h | ConvertTo-Json -Depth 4
Invoke-RestMethod -Method Post http://localhost:8000/api/v1/fault-injection -Headers $h -ContentType application/json `
  -Body '{"target":"payment-service","type":"pool_exhaustion","duration_s":180}'
```
