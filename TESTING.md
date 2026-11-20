# Testing

All numbers in this document come from runs executed on the development machine (see *Environment* in
BENCHMARKS.md). "Validated locally" means the same commands CI runs were executed locally. **No GitHub
Actions run has happened yet**, because the repository has not been created.

## Test pyramid

| Layer | Tool | Location | Needs | Count |
|---|---|---|---|---|
| Backend unit (security, detectors, log/trace analysis, RCA scoring, AI validator) | pytest | `backend/tests/test_{security,detectors,logs_traces,rca_scoring,explainer}.py` | nothing | 60 |
| Backend API + DB (auth lifecycle, RBAC matrix, incidents, observability, settings, faults) | pytest + httpx ASGI | `backend/tests/test_api_*.py` | PostgreSQL, Redis | 49 |
| WebSocket (real uvicorn + real WS client) | pytest + websockets | `backend/tests/test_ws.py` | Redis | 9 |
| Demo-service runtime (logging/trace context, faults, middleware, admin auth) | pytest | `services/tests/test_common.py` | nothing | 12 |
| Frontend unit/component (live client, formatters, graph layout, waterfall, pages, auth, RBAC UI, states) | Vitest + Testing Library | `frontend/src/test/*` | nothing | 17 |
| Integration (real telemetry: propagation, Loki/Tempo correlation, Prometheus, Kafka→WS) | pytest | `e2e/test_integration.py` | running stack | 8 |
| End-to-end failure lifecycle (SRE + Viewer, WS, fault, RCA, resolve, recovery) | pytest | `e2e/test_e2e_flow.py` | running stack | 1 (long) |
| Browser E2E (real UI, Edge/Chromium) | Playwright | `frontend/e2e/ui.spec.ts` | running stack | 4 |
| Detector evaluation (labelled synthetic) | script | `backend/app/ml/evaluate.py` | nothing | 10,560 points |
| Live RCA accuracy (8 injected failure classes) | script | `e2e/rca_eval.py` | running stack | 8 scenarios |

Backend total: 118 collected tests (60 unit + 49 API/DB + 9 WebSocket), counting parametrised cases.

## How to run

```powershell
# Unit + API + WS (backend needs postgres + redis from compose)
docker compose up -d postgres redis
cd backend;  ..\.venv\Scripts\python -m pytest -q;  cd ..
cd services; $env:OTEL_ENABLED="false"; ..\.venv\Scripts\python -m pytest -q; cd ..
cd frontend; npx vitest run; cd ..

# Against the full running stack (docker compose up -d; wait ~5 min for baselines)
cd e2e; ..\.venv\Scripts\python -m pytest test_integration.py -v; cd ..
.\.venv\Scripts\python -m pytest e2e\test_e2e_flow.py -v -s -c e2e\pytest.ini --rootdir e2e
cd frontend; npx playwright test; cd ..          # uses system Microsoft Edge (PW_CHANNEL=chromium elsewhere)
.\.venv\Scripts\python -u e2e\rca_eval.py        # ~55 min
cd backend; ..\.venv\Scripts\python -m app.ml.evaluate; cd ..
```

Or: `scripts\faultlens.ps1 test` (unit/API) and `scripts\faultlens.ps1 test-live` (integration + browser).

## What the important tests prove

- **RBAC is enforced by the backend**: `test_api_rbac.py::test_matrix` hits 15 protected operations as
  anonymous / viewer / SRE / admin and asserts 401 / 403 / success exactly per the permission table, plus
  explicit viewer-cannot-write and SRE-cannot-administer tests.
- **Token lifecycle**: refresh rotation, theft detection on reuse (family revoked), logout revocation,
  immediate effect of deactivation and role change, rate-limited login, password policy.
- **WebSocket**: authorised delivery through Redis Pub/Sub, sensitive-key stripping, rejection of missing,
  garbage and expired tokens (4401), foreign Origin rejected at the handshake (403), disconnect cleanup,
  reconnect then resumed delivery, fan-out to multiple clients. The frontend `LiveClient` tests cover backoff,
  token refresh after 4401, pong and stop().
- **Trace↔log correlation** (`test_integration.py`): an order sent with a known `traceparent` produces a Tempo
  trace containing all five services, with each downstream server span parented by an order-service client
  span; every Loki line for that trace id carries a `span_id` that exists in the trace; the FaultLens API
  returns the same correlation. Malformed or missing `traceparent` starts a fresh trace. Log records without
  span context return `trace_id: null` (`test_api_observability.py`).
- **RCA does not cheat**: `test_rca_engine_never_reads_fault_injection_records` statically checks that no RCA
  module imports the fault-injection model or table.
- **RCA scoring**: one constructed evidence bundle per failure class must rank the right (cause, service)
  first; weights sum to 1; score = locality × Σ weight·strength; no evidence ⇒ no hypotheses ⇒ inconclusive;
  missing evidence sources lower scores instead of being guessed.
- **AI layer**: the validator rejects invented services, numbers and causes; an invalid or unavailable LLM
  falls back to the deterministic narrator.

## Results (latest local runs)

| Suite | Result |
|---|---|
| Backend pytest (`backend/`) | **118 passed** (≈65 s); 5 consecutive clean runs. One earlier run under heavy CPU load (concurrent `docker build`) had 1 failure that did not reproduce in 5 reruns; the failing test name was not captured. Likely a timing-sensitive WS/rate-limit test; recorded as a known flaky candidate |
| Services pytest | **12 passed** |
| Frontend Vitest | **17 passed** |
| Ruff (backend, services, e2e) | clean |
| ESLint (`--max-warnings 0`) + `tsc --noEmit` | clean |
| Frontend production build | success |
| `docker compose config --quiet` | valid |
| Alembic upgrade → downgrade → upgrade, `alembic check` | pass, no drift |
| Integration (`e2e/test_integration.py`) | **8 passed**, 0 failed, 0 skipped (67.26 s), 2026-11-20 |
| Browser E2E (`frontend/e2e/ui.spec.ts`, Microsoft Edge) | **4 passed**, 0 failed (5.2 s), 2026-11-20: login/navigation/logout, dependency graph, trace waterfall → correlated logs, live incident delivery with Viewer read-only. `npx playwright test` also collects `e2e/screenshots.spec.ts`, which is **skipped** by design unless `CAPTURE=1`, so the run reports "4 passed, 1 skipped" |
| End-to-end failure lifecycle (`e2e/test_e2e_flow.py`) | **passed**, 2026-11-19 (timings in `e2e/results/e2e_timings.json`); not re-run since |
| Live RCA evaluation (`e2e/rca_eval.py`) | 8/8 scenarios detected and ranked #1, one run each (2026-11-19/20); see BENCHMARKS.md §2 |
| Detector evaluation (`app.ml.evaluate`) | see BENCHMARKS.md §1; not re-run on 2026-11-20 |

The integration and Playwright suites were run once each on 2026-11-20 against the running stack (no faults
active, traffic generator at 8 req/s). The backend, services and frontend suites, the E2E failure lifecycle and
the detector evaluation were **not** re-run then; the results above are from their latest earlier runs. There is
no single run in which every suite passed together.

## Bugs found by testing (and fixed)
Recorded because they show what the tests are for:
1. **RCA picked "service slowdown" over "pool exhaustion"** in the first live run: trace analysis counted
   pool-wait time as application time, and young incidents were diluted by pre-onset samples. Fixed with
   `db_pool` span categorisation, onset-based incident levels, and app-only self time.
2. **Anomaly events published before commit**: the correlator sometimes read the row before it existed and
   dropped the event, delaying incidents by minutes (found when the memory scenario "missed"). Fixed: commit,
   then publish, with a bounded retry in the consumer.
3. **Keep-alive race** in the eval client (uvicorn 5 s keep-alive vs 5 s polling): idempotent GET retry, and the
   server keep-alive raised to 30 s.
4. **Isolation Forest recall 0.21 → 0.53** after measuring that single-feature deviations were not isolatable;
   aggregate-deviation features added.
5. **EWMA false positives on return-to-normal** reduced by requiring deviation from the long-term median too.
6. **Traffic generator semaphore closure bug** (B023): a finished task could release a different semaphore after a config change.
7. **Evidence validator compared number strings** ("0.60" ≠ "0.6"); it now compares numerically.
8. **Mermaid diagram** with `;` in a message would not render on GitHub; validated all diagrams with the Mermaid parser.
