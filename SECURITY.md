# Security

FaultLens is designed to run **locally** as a demonstration and learning platform. The controls below are real
and tested, but the project has **not** had an independent security review and should not be exposed to the
internet as-is.

## Reporting
For the public repository, please report vulnerabilities privately through GitHub Security Advisories
("Report a vulnerability") rather than public issues.

## Threat model (local demo)
- Assets: user credentials, incident data, the ability to inject faults or change detection settings.
- Trust boundary: the browser ↔ nginx/API. Internal services sit on a private Docker network; only
  `127.0.0.1` host ports are published.
- Out of scope: a malicious local administrator with Docker access (they control everything anyway).

## Controls

| Area | Control | Where / verified by |
|---|---|---|
| Secrets | No secrets in source. `scripts/setup.ps1` / `setup.sh` generate `.env` with random values; `.env` is git-ignored and excluded from the ZIP. Compose fails fast if a required secret is missing (`${VAR:?}`). | `.env.example`, `docker-compose.yml` |
| Passwords | argon2id with per-hash salt; policy ≥ 12 chars and mixed classes (or ≥ 16 chars); rehash on login if parameters change; unknown-user logins still hash (no timing oracle) | `core/security.py`, `test_security.py` |
| Tokens | 15-min HS256 JWT access tokens (memory only in the SPA); refresh tokens are random, stored hashed, `HttpOnly; SameSite=Strict`, path-scoped, rotated on use; reuse revokes the token family; logout, password change, role change and deactivation revoke sessions | `api/v1/auth.py`, `test_api_auth.py` |
| Authorisation | Every protected endpoint declares a permission; roles are re-checked against the database on every request; unknown roles denied | `api/deps.py`, `test_api_rbac.py` (role × endpoint matrix) |
| WebSocket | Token in first frame (not URL), 5 s auth deadline, Origin allow-list (403), expiry enforced mid-connection, payload sanitisation, bounded per-client queue | `events/ws.py`, `test_ws.py` |
| Input validation | Pydantic schemas with bounds on every body/query; service names restricted to `[a-z][a-z0-9-]{1,62}` before entering PromQL/LogQL/TraceQL; trace ids must be 32 hex; LogQL search text escaped; time ranges bounded | `api/schemas.py`, `telemetry/promql.py`, `test_api_observability.py` (injection cases) |
| Rate limiting | Login 10/min/IP, refresh 30/min, password change 5/5 min, fault injection 20/min (Redis, fail-open) | `api/deps.py::limit` |
| CORS | Explicit allow-list of local UI origins; credentials allowed only for those | `main.py`, `CORS_ORIGINS` |
| Fault injection | Disabled unless `ENVIRONMENT` is local/demo/test **and** `FAULT_INJECTION_ENABLED=true`; SRE/Admin only; service admin endpoints require a ≥ 16-char shared token compared in constant time; hard parameter limits; auto-expiry; audited | `api/v1/faults.py`, `faultlens_common/faults.py`, tests in both packages |
| Logging | JSON logs redact keys matching password/secret/token/authorization/cookie/api_key/card; query strings are not logged; payment data is tokenised (`tok_…`), never card numbers | `core/logging.py`, `faultlens_common/logging.py` |
| HTTP headers | API: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. UI (every nginx response, including errors): the same first three, plus `Permissions-Policy: camera=(), microphone=(), geolocation=()` and the CSP below (no inline scripts) | `main.py`, `frontend/nginx.conf` |
| Containers | App containers (API, worker, demo services, UI): non-root users (uid 10001; nginx-unprivileged for the UI), `read_only` root filesystems with tmpfs, `no-new-privileges`. `cap_drop: ALL` on the API, worker and demo services, but not the UI. Memory limits on the demo services (512 MB; traffic generator 256 MB), API (768 MB) and worker (1 GB); a 1-CPU limit on the demo services only; no limits on the UI. Third-party containers (datastores, Collector, Prometheus, Loki, Tempo, Grafana) use their images' defaults; see *Known limitations* | `docker-compose.yml`, Dockerfiles |
| Database | Separate least-privilege roles per database; `PUBLIC` connect revoked; all schema changes via Alembic | `deploy/postgres/init.sh` |
| AI layer | LLM optional and off by default; key only from environment and never returned by the API; output validated against evidence; no tool or command execution | `ai/explainer.py`, `test_explainer.py` |
| Supply chain | Exact version pins; `pip-audit` and `npm audit` in CI; Dependabot config | `.github/` |

UI Content-Security-Policy, exactly as `frontend/nginx.conf` sends it:

```
default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'
```

`font-src` allows `data:` only because the build embeds small font subsets in the CSS as `data:` URIs. This
applies only to font loading: `script-src` is still `'self'`, so neither `data:` URIs nor inline scripts can run code.

UI caching, as `frontend/nginx.conf` sets it (one `map` on the request path, applied at server level so the
security headers above are kept on every response):

| Path | `Cache-Control` from nginx |
|---|---|
| HTML and SPA routes (everything not listed below) | `no-store` |
| `/assets/` (content-hashed file names) | `public, max-age=31536000, immutable` |
| `/api/` (proxied) | none added by nginx; the API sends its own `Cache-Control: no-store` |

## Audit trail
Logins (success and failure), token-reuse detection, password changes, user changes, settings changes,
incident transitions, investigation runs and every fault-lab action are written to `audit_events`
(visible to Admins in Settings).

## Known limitations
- HTTP only by default; set `COOKIE_SECURE=true` and terminate TLS in front of nginx for anything beyond localhost.
- HS256 with a single shared secret; no key rotation tooling.
- No MFA and no per-account lockout (IP rate limiting only); fixed-window limits allow short bursts at window boundaries.
- The CSP allows inline *styles* (required by React Flow / Recharts) and `data:` fonts (embedded font subsets).
- `connect-src` allows `ws:` / `wss:` to any host, not only the UI's own origin.
- Grafana, Prometheus, Loki and Tempo are reachable on localhost; Prometheus/Loki/Tempo have no auth (local-only binding).
- Prometheus runs with `--web.enable-lifecycle`, so its unauthenticated `/-/reload` and `/-/quit` endpoints are on.
  Port 9090 is published on `127.0.0.1` only, but any local process (and any container on the private network) can
  make Prometheus reload its configuration or shut down.
- The UI container (nginx-unprivileged) does not set `cap_drop: ALL`. It runs as a non-root user with
  `no-new-privileges` and a read-only root filesystem, but keeps Docker's default capability set.
- Tempo runs as root inside its container (`user: "0"` in `docker-compose.yml`). It is not privileged and its
  port is published on `127.0.0.1` only. The other third-party containers also run without `read_only` or
  `cap_drop`.
- Kafka and Redis have no TLS; Redis is password-protected, Kafka is unauthenticated on the private network.
- Single-node Kafka/Postgres/Redis; no backups configured.
