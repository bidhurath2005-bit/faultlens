# Development Status

_Last updated: 2026-11-20 (after the release verification tasks I–L.1)._

## Remaining work
1. Create the GitHub repository and let the three workflows run remotely. **The project has not been published
   to GitHub yet**, and no GitHub Actions run has happened.

## Session log

### 2026-11-20: release packaging and verification (tasks I–L.1)
- **Packaging done.** `FaultLens.zip` is built by `scripts/package.py`, which verifies the archive after building
  it. The release is identified by the SHA-256 of the final ZIP, recorded when it is built. Excluded by design:
  `.env`, virtualenvs, `node_modules`, caches, build output (including `services/common/build/`), `*.log` and
  `*.partial.json`. So `e2e/results/benchmark.log` and `e2e/results/rca_evaluation.partial.json` are not in the ZIP.
  Files under `e2e/results/` are stored byte for byte, so the SHA-256 values in `rca_evaluation.json`'s
  `merged_from` field still match the packaged files.
- **Clean-extraction audit (I):** the ZIP extracts cleanly; no `.env`, no unsafe paths, required files present.
- **Fresh-install checks (J.1–J.3):** fresh-install audit, installation validation and cleanup/safety audit.
- **nginx security headers (J.4–J.6.1):** an `add_header` inside a `location` block had stopped nginx inheriting
  the server-level security headers on HTML and `/assets/` responses. Fixed by choosing Cache-Control through a
  `map` instead, documenting the CSP (including `font-src 'self' data:`), and verifying the headers on a live,
  isolated stack.
- **Final ZIP refresh (J.7):** rebuilt and integrity-checked.
- **Isolated runtime test from the extracted ZIP (K / K.1): passed.** Images were built from the extracted
  release under separate image tags, a separate Compose project, alternate host ports and new volumes, with a
  freshly generated `.env`, while the development stack kept running untouched. Results: 17/17 containers
  running, 10/10 health checks healthy, no restarts. API health went from `degraded` (Loki/Tempo starting) to
  `ok` by the next check, about 90 s after startup. The UI, its assets and its security headers loaded. Login succeeded, and wrong
  credentials and missing or invalid tokens returned 401. Seed data (200 users, 50 products) and the Kafka
  topics were present. One test order was created, read back and found in PostgreSQL. Prometheus, Loki, Tempo,
  Grafana, Redis and Kafka responded. The test stack and its volumes were then removed. No faults were injected.
- **Final release audit (L): no blocking issues.** It recommended documentation corrections, which were made
  in L.1 (this file, a `benchmark.log` caveat in BENCHMARKS.md, container-hardening and caching notes in
  SECURITY.md, a `CORS_ORIGINS` note in README.md), and the ZIP was rebuilt. No runtime or source code changed.

### 2026-11-19 ~18:05 UTC: paused for host memory
- RCA eval (`e2e/rca_eval.py`) had stopped after injecting `notification-memory` at 18:01:27 UTC (no result recorded).
  Completed: payment-pool, inventory-db-slow, payment-timeout, inventory-cpu (all detected, auto rank 1, T+90 rank 1).
  Backed up to `e2e/results/rca_evaluation.first4.json`, because a `--scenarios` run overwrites `rca_evaluation.partial.json`.
- After that interrupted attempt, notification-service stayed `degraded` at ~96 % of its 512 MiB limit even though
  the fault had expired, and recovered only after `docker restart faultlens-notification-service-1` (→ 117 MiB).
- Host memory was ~430–520 MB available (WSL VM 5.2 GB, an unrelated `fraudstream` stack running, no `.wslconfig`),
  so work stopped before any further load.

### 2026-11-20: resumed and completed validation
A `.wslconfig` was added (`memory=6GB`, `autoMemoryReclaim=gradual`) and the unrelated stacks stayed stopped. Completed:
- **RCA evaluation, all 8 scenarios.** The remaining four (`inventory-down`, `payment-500`, `traffic-spike`,
  `notification-memory`) ran one per invocation (05:19–05:46 UTC) and were merged with the first four into
  `e2e/results/rca_evaluation.json`. Result: 8/8 detected, expected cause ranked #1 at both checkpoints, one run
  per scenario. This time `notification-memory` recovered without a container restart. Details: BENCHMARKS.md §2.
- **Benchmark**, conservative settings (05:58:52–06:00:49 UTC): `e2e/results/benchmark.json`, BENCHMARKS.md §3.
- **Dependency-graph fix deployed and verified live.** The API now classifies graph nodes as `service`, `client`
  (traffic generator, servicegraph's virtual `user` node) or `database` (`shop`). After the final backend deploy,
  the Playwright dependency-graph test passed (1/1).
- **Screenshots recaptured** from the final build with no faults active (`frontend/e2e/screenshots.spec.ts`,
  `CAPTURE=1`): the 9 images README.md embeds. `docs/screenshots/service-detail.png` and `settings.png` were not
  recaptured (2026-11-19) and are not referenced by README.md.
- **README.md and BENCHMARKS.md** updated with the measured results (no placeholders remain).
- **Integration suite** (`e2e/test_integration.py`): 8/8 passed in 67.26 s.
- **Playwright UI suite** (`frontend/e2e/ui.spec.ts`): 4/4 passed in 5.2 s (screenshot-capture spec skipped by design).
- Not re-run on 2026-11-20: backend, services and frontend unit suites, the E2E failure-lifecycle test, and the
  detector evaluation. Their latest results are the earlier ones recorded in TESTING.md.

## Phase summary

| Phase | Status |
|---|---|
| 0 Environment inspection | ✅ Done (Windows 11, 16 CPUs, 15.7 GB RAM, Python 3.12.10, Node 26.13, Docker 29.9 / Compose 5.6; port 8080 already in use on the dev machine, so the UI uses 3080) |
| 1 Architecture | ✅ Done: ARCHITECTURE.md, 9 ADRs |
| 2 Core implementation | ✅ Done: services, backend, DB + migrations, frontend |
| 3 Observability | ✅ Done and verified with real telemetry (integration tests) |
| 4 Intelligence | ✅ Done: detectors (evaluated), RCA (evaluated live), AI layer |
| 5 Frontend integration | ✅ Done: every page on real APIs, no mock data in the app |
| 6 Full integration | ✅ Done: Compose stack; integration 8/8 and Playwright 4/4 (2026-11-20); E2E failure lifecycle passed (2026-11-19) |
| 7 Hardening | ✅ Done: security controls, dependency audits (0 known vulns after upgrades), benchmarks, docs |
| 8 Packaging | ✅ Done: `FaultLens.zip` built by `scripts/package.py`; clean extraction, isolated fresh install/runtime test and final audit passed (2026-11-20). Not yet published to GitHub |

## Feature checklist (PART 25 + PART 27)

| Item | Status | Evidence |
|---|---|---|
| Five independently running microservices + inter-service communication | ✅ | `services/`, compose; integration test sees all 5 in one trace |
| OpenTelemetry instrumentation; real metrics, logs, traces | ✅ | `test_integration.py` (Prometheus, Loki, Tempo) |
| Service dependency graph (observed) | ✅ | servicegraph connector; `/dependencies`; Playwright graph test |
| Statistical + ML anomaly detection, evaluated | ✅ | `app/engines/anomaly`, `app/ml/evaluate.py`, BENCHMARKS.md |
| Evidence-based RCA with ranking; inconclusive outcome | ✅ | `app/engines/rca`, `test_rca_scoring.py`, live eval |
| AI investigation explanations (provider-independent, validated) | ✅ | `app/ai/explainer.py`, `test_explainer.py` |
| Incident management (lifecycle, timeline, audit, recovery) | ✅ | `test_api_incidents.py`, E2E |
| Interactive fault injection (8 types, bounded, local-only) | ✅ | Fault Lab page, `test_common.py`, E2E |
| Professional React dashboard (10 pages + login) | ✅ | screenshots in `docs/screenshots` |
| PostgreSQL + Alembic, Redis, Kafka (real workflows) | ✅ | ADR-002/003/004; `alembic check` |
| Docker Compose deployment with health checks | ✅ | `docker-compose.yml` |
| Backend, frontend, integration, E2E tests | ✅ | TESTING.md |
| Performance benchmarks | ✅ | BENCHMARKS.md, `e2e/results/benchmark.json` |
| Security review and controls | ✅ (self-review, not an independent audit) | SECURITY.md |
| Auth (login/logout, argon2id, JWT + rotating refresh) + RBAC | ✅ | `test_api_auth.py`, `test_api_rbac.py` |
| WebSocket real-time updates + auth + reconnection | ✅ | `test_ws.py`, `live.test.ts`, Playwright live-incident test |
| trace_id/span_id in logs; propagation; Loki↔Tempo correlation used by RCA | ✅ | `test_integration.py`, Grafana provisioning |
| GitHub Actions workflows | ✅ present, **validated locally only** (never run on GitHub) | `.github/workflows/` |
| ADRs linked from ARCHITECTURE.md and README | ✅ | `docs/adr/` |

## Known issues / limitations
- One backend test failed once under heavy CPU contention and did not reproduce in 5 reruns (name not captured).
- RCA evaluated on 8 controlled faults injected into this demo system, one run each; in `traffic-spike` the
  system-wide cause was attributed to the entry service. See BENCHMARKS.md §2.
- `memory_pressure` recovery is not always clean: in the interrupted 2026-11-19 attempt notification-service
  needed a container restart; in the recorded 2026-11-20 run it recovered on its own (BENCHMARKS.md §2).
- Benchmarks are single-run measurements on one machine with conservative settings, not capacity figures.
- `.venv` was recreated during the session; console-script launchers (e.g. `ruff.exe`) may point to the old
  path, so always use `python -m <tool>`. Recreate with `scripts\faultlens.ps1 test` (it builds `.venv` if missing).
- The servicegraph connector also reports a database node (`shop`) and a virtual `user` node (uninstrumented
  callers); both are shown and labelled as such in the UI.
- See README *Limitations* and SECURITY.md *Known limitations*.

## Next steps (suggested, after the remaining work above)
1. Add a transactional outbox for Kafka publishing.
2. Collect RCA accuracy over more repetitions per scenario (the current eval is 1 run per scenario).

## Resume commands

```powershell
cd FaultLens
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1   # only if .env is missing
docker compose up -d --build
scripts\faultlens.ps1 status
scripts\faultlens.ps1 test        # unit/API/frontend tests
scripts\faultlens.ps1 test-live   # integration + Playwright
```
