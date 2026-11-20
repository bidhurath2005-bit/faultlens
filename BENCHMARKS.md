# Benchmarks & Evaluation

Every number below was measured on the machine described in *Environment*. Raw outputs are saved in
`e2e/results/` and `backend/app/ml/artifacts/`, and every number can be reproduced with the commands shown.
These are **single-machine, single-run measurements of a demo system**, not capacity or accuracy guarantees.

## Environment
| | |
|---|---|
| Machine | Windows 11 Home (10.0.26200), 16 logical CPUs, 15.7 GB RAM |
| Containers | Docker Desktop 29.9.2 (WSL2 VM, memory capped at 6 GB in `.wslconfig`); all 17 containers (including the traffic generator) and the benchmark/evaluation scripts on the same host |
| Python / Node | 3.12.10 / 26.13 |
| Demo service limits | 1 CPU and 512 MB per service container; DB pool size 5, pool timeout 2 s, downstream timeout 3 s |
| Background traffic | traffic generator at 8 req/s (Poisson, seed 1234): 60% orders, 15% order lists, 15% product reads, 10% user reads |
| Detection cadence | worker cycle 10 s; Prometheus scrape 5 s; OTel metric export 5 s |

## 1. Anomaly detection (labelled SYNTHETIC dataset)

`cd backend; python -m app.ml.evaluate` (≈5 min). Output: `backend/app/ml/artifacts/anomaly_evaluation.json`.

**Dataset (synthetic, clearly not production data):** 30 scenarios × 480 steps (15 s/step = 2 h each), seeds
11/22/33. There are 8 anomaly types, each a labelled 60-step (15-min) segment starting at step 300, plus 2
benign variation types (gradual load growth, heavier latency noise) with no anomalies. That gives 10,560
evaluated points, 1,440 of them anomalous. Replay is causal: at step *t* each detector sees only the baseline
window [t−120, t−8) and the current point, with the production 2-cycle confirmation rule.

| Detector | Precision | Recall | F1 | False-positive rate | TP | FP | FN |
|---|---|---|---|---|---|---|---|
| Static threshold | 1.000 | 0.706 | 0.827 | 0.00% | 1016 | 0 | 424 |
| Robust z-score | 1.000 | 0.967 | 0.983 | 0.00% | 1392 | 0 | 48 |
| EWMA moving average | 0.863 | 0.188 | 0.309 | 0.47% | 271 | 43 | 1169 |
| Isolation Forest | 1.000 | 0.531 | 0.693 | 0.00% | 764 | 0 | 676 |
| **Ensemble (any detector, as deployed)** | **0.970** | **0.967** | **0.968** | **0.47%** | 1392 | 43 | 48 |

Per-type recall (ensemble detection delay in 15 s steps, three seeds):

| Anomaly type | threshold | z-score | EWMA | IForest | ensemble | ensemble FP | first detection |
|---|---|---|---|---|---|---|---|
| latency spike | 0.96 | 0.98 | 0.21 | 0.48 | 0.98 | 0 | 1, 1, 1 |
| error spike | 0.98 | 0.98 | 0.21 | 0.39 | 0.98 | 0 | 1, 1, 1 |
| traffic spike | 0.00 | 0.98 | 0.20 | 0.57 | 0.98 | 43 | 1, 1, 1 |
| CPU saturation | 0.97 | 0.98 | 0.20 | 0.49 | 0.98 | 0 | 1, 1, 1 |
| memory leak (ramp) | 0.00 | 0.85 | 0.00 | 0.52 | 0.85 | 0 | 9, 9, 9 |
| pool saturation | 0.98 | 0.98 | 0.23 | 0.62 | 0.98 | 0 | 1, 1, 1 |
| DB slowdown | 0.76 | 0.98 | 0.23 | 0.57 | 0.98 | 0 | 1, 1, 1 |
| outage / traffic drop | 0.98 | 0.98 | 0.23 | 0.61 | 0.98 | 0 | 1, 1, 1 |
| benign: gradual load growth | n/a | n/a | n/a | n/a | n/a | **0** | n/a |
| benign: noisy latency | n/a | n/a | n/a | n/a | n/a | **0** | n/a |

**How to read this honestly**
- The injected anomalies are large and clean, so near-perfect z-score recall mostly shows the detector logic
  and confirmation rule work as intended. It is *not* evidence of production accuracy.
- EWMA's low point-level recall is by design: it adapts to a sustained shift within a few steps, so it acts as an
  onset detector. All 43 ensemble false positives come from EWMA *after* traffic spikes, once the baseline window
  is more than half contaminated by the spike itself (a documented limitation).
- Isolation Forest recall improved from 0.21 to 0.53 during development after measuring that single-feature
  deviations were not isolatable among 7 features. Aggregate deviation features (max |z|, mean |z|) were added.
  It remains the weakest single detector on single-metric anomalies and the most useful for multi-metric ones
  (pool saturation, DB slowdown, outage).
- Memory leaks are detected later (9 steps ≈ 2¼ min) because the ramp must exceed the 64 MB minimum meaningful change.

## 2. Root-cause analysis: controlled fault-injection evaluation

`python e2e/rca_eval.py [--scenarios <id,...>]` (defaults: 150 s cooldown, 180 s fault). Output:
`e2e/results/rca_evaluation.json`.

**Method.** For each scenario the script resets all faults, resolves open incidents, waits 150 s, injects one
fault through the Fault Lab API and then waits up to 150 s for the automatically opened incident. It scores
two checkpoints:

- **Automatic**: the first investigation the worker runs on its own.
- **T+90 s**: an investigation re-run about 90 s after injection, when more evidence is available.

It then resets the fault and waits up to 300 s for verified recovery. The RCA engine sees only telemetry; the
expected cause is known only to the evaluation script. A scenario counts as correct at rank *k* when a finding
with the expected cause type (and the expected service, where one is defined) appears at rank *k*.

**Runs.** The four scenarios from `payment-pool` to `inventory-cpu` ran in one invocation on 2026-11-19 (17:35–17:54 UTC). The
remaining four ran one scenario per invocation on 2026-11-20 (05:19–05:46 UTC) after a host restart. The eight
results were merged into `rca_evaluation.json`; the `merged_from` field lists each source file with its SHA256.
**Each scenario was run once.**

| Scenario (injected fault) | Expected cause / service | Incident | Detection | Detectors fired | Auto: rank (score) | T+90 s: rank (score) | Recovery verified after reset |
|---|---|---|---|---|---|---|---|
| `payment-pool` (pool exhaustion) | db_pool_exhaustion / payment-service | #19 | 19.2 s | threshold | 1 (1.00) | 1 (1.00) | 147.6 s |
| `inventory-db-slow` (600 ms DB delay) | db_query_latency / inventory-service | #20 | 29.0 s | threshold, z-score, EWMA, IForest | 1 (1.00) | 1 (1.00) | 102.4 s |
| `payment-timeout` (3.5 s latency) | service_timeout / payment-service | #21 | 23.8 s | threshold, z-score, EWMA | 1 (1.00) | 1 (1.00) | 151.5 s |
| `inventory-cpu` (2 CPU-burn threads) | high_cpu / inventory-service | #22 | 29.8 s | z-score, EWMA, IForest | 1 (0.74) | 1 (0.75) | 161.6 s |
| `inventory-down` (service unavailable) | downstream_failure / inventory-service | #58 | 23.7 s | threshold, z-score, EWMA, IForest | 1 (0.83) | 1 (0.91) | 124.6 s |
| `payment-500` (60% HTTP 500) | http_500_spike / payment-service | #59 | 29.2 s | threshold, z-score, EWMA, IForest | 1 (1.00) | 1 (1.00) | 114.7 s |
| `traffic-spike` (6× traffic) | traffic_overload / system-wide | #60 | 23.0 s | z-score, EWMA, IForest | 1 (0.80) | 1 (0.80) | 95.4 s |
| `notification-memory` (384 MB allocation) | memory_pressure / notification-service | #62 | 37.9 s | z-score, EWMA, IForest | 1 (0.95) | 1 (1.00) | 176.8 s |

*Detection* is the time from injection to the first anomaly attached to the incident. All findings were
rated high confidence.

| Summary | Automatic | T+90 s |
|---|---|---|
| Detected | 8 / 8 | n/a |
| Top-1 accuracy | 8 / 8 (100%) | 8 / 8 (100%) |
| Top-3 accuracy | 8 / 8 (100%) | 8 / 8 (100%) |

- Detection latency ranged from 19.2 s to 37.9 s. The evaluator reports a median of **29.0 s**, which is the
  upper of the two middle values; the mean of the two middle values (23.8 s and 29.0 s) is 26.4 s.
- The first automatic investigation completed 39.5–58.4 s after injection, except `payment-500` (134.7 s).
- Verified recovery took 95.4–176.8 s after reset. This includes the 90 s healthy-and-quiet window that
  recovery verification requires.

**How to read this honestly**
- **One run per scenario, not repeated trials.** There is no variance estimate, and 8/8 cannot distinguish a
  method that is always right from one that is right most of the time.
- **Controlled faults in the system the engine was designed for.** Each injected fault maps to one of the 8
  cause classes the hand-designed RCA rules model, and the faults are large and clean. This shows the engine
  handles its modelled classes end to end; it is not evidence of accuracy on production incidents.
- **`traffic-spike` service attribution.** The expected cause is system-wide, so the evaluator checks only the
  cause type. The engine correctly diagnosed `traffic_overload` but attributed it to `inventory-service` (as the
  entry point), not to the whole system. A second incident (#61, "Traffic overload entering via
  inventory-service") opened during the same scenario.
- **Narrowest margin:** `inventory-cpu` scored 0.74–0.75, with `service_timeout` in inventory-service second at 0.47.
- **`notification-memory` recovery.** After the fault was reset, notification-service held about 475 MiB
  (93% of its 512 MiB limit) for about 95 s. It was released about 190 s after injection, close to the end of
  the fault's 180 s duration, without a container restart. An earlier, interrupted attempt needed a restart to recover (see
  DEVELOPMENT_STATUS.md).

## 3. Performance

```powershell
.\.venv\Scripts\python.exe e2e\benchmark.py --api-requests 270 --load-seconds 15 --concurrency 8 --ingestion-samples 5
```

Run on 2026-11-20 from 05:58:52 to 06:00:49 UTC (1 min 57 s). The settings are deliberately conservative (the
script defaults are 450 / 20 s / 16 / 10) to stay inside the host's memory headroom. The background traffic
generator kept running at 8 req/s throughout. Output: `e2e/results/benchmark.json`; host RAM samples:
`e2e/results/benchmark.ram`. The raw console log, `e2e/results/benchmark.log`, was kept on the development machine
but is excluded from the distributable ZIP (the packaging script leaves out `*.log` files). The figures in this
section come from `benchmark.json`, apart from the host RAM samples, which come from `benchmark.ram`.

### 3.1 API throughput (`GET /api/v1/services`, cached)
8 concurrent workers sending back-to-back authenticated requests for 15 s.

| Requests | Throughput | Errors | p50 | p95 | p99 | Mean | Max |
|---|---|---|---|---|---|---|---|
| 6,600 | 439.6 req/s | 0 (all 200) | 18.71 ms | 29.72 ms | 37.71 ms | 18.02 ms | 378.88 ms |

### 3.2 Checkout under load (`POST /orders` on order-service)
Each order is one order-service request plus 4 downstream service calls and 5 database transactions.
8 concurrent workers for 15 s, on top of the background traffic.

| Orders | Throughput | Errors | p50 | p95 | p99 | Mean | Max |
|---|---|---|---|---|---|---|---|
| 1,216 | 80.8 orders/s | 0 (all 201) | 90.58 ms | 145.38 ms | 255.08 ms | 98.14 ms | 403.56 ms |

### 3.3 Sequential API latency
30 sequential authenticated requests per endpoint (270 in total). The script accepts 200 or 404 responses
and stops on anything else; all requests passed that check (it does not record which of the two codes each returned).

| Endpoint | p50 | p95 | p99 | Max |
|---|---|---|---|---|
| `/health` | 4.70 ms | 7.65 ms | 15.37 ms | 18.25 ms |
| `/services` | 4.44 ms | 4.98 ms | 18.12 ms | 23.45 ms |
| `/metrics/overview` | 5.37 ms | 7.07 ms | 32.62 ms | 42.98 ms |
| `/incidents` | 5.98 ms | 6.57 ms | 6.91 ms | 7.00 ms |
| `/anomalies` | 4.93 ms | 6.25 ms | 6.59 ms | 6.63 ms |
| `/dependencies` | 2.86 ms | 3.91 ms | 4.18 ms | 4.25 ms |
| `/logs?minutes=5&limit=50` (Loki query) | 55.52 ms | 60.20 ms | 62.41 ms | 62.94 ms |
| `/traces?minutes=5&limit=20` (Tempo query) | 14.41 ms | 16.59 ms | 17.28 ms | 17.44 ms |
| `/rca/62` | 7.50 ms | 8.98 ms | 9.69 ms | 9.87 ms |

### 3.4 Telemetry queryability
Time from an order request completing to its trace being returned by Tempo, and its log line by Loki, polled
every 250 ms. This includes SDK batch export (about 1–5 s), Collector batching (2 s) and backend ingestion.

| Signal | Samples | p50 | Mean | Max |
|---|---|---|---|---|
| Trace in Tempo | 5 | 9.89 s | 8.77 s | 10.10 s |
| Log line in Loki | 5 | 7.13 s | 7.06 s | 7.39 s |

### 3.5 Worker metrics (Prometheus histograms, 30-minute window)

| Metric | p50 | p95 | Mean | Count in window |
|---|---|---|---|---|
| Detection cycle | 0.036 s | 0.184 s | 0.053 s | ≈181 |
| RCA investigation | 0.359 s | 0.486 s | 0.344 s | ≈9 |
| Event-bridge lag (Kafka event age when bridged to WebSocket) | 0.0050 s | 0.0095 s | 0.0023 s | ≈178 |

The window ended at the benchmark and therefore includes the `notification-memory` evaluation scenario and the
incident the benchmark load itself opened. Counts are Prometheus `increase()` estimates, which is why they are
not whole numbers. Percentiles come from `histogram_quantile`, which interpolates within fixed buckets (for
example, the smallest buckets are 50 ms for the detection cycle and 10 ms for event lag), so they are approximate.

### 3.6 Resource observations
- **Containers** (two `docker stats` snapshots, not peaks):
  - Just after the `/services` load phase, the five demo services were at 21.7–24.9% CPU each.
  - Just after the checkout phase, faultlens-api was at 19.0% and the traffic generator at 22.8%.
  - Tempo memory rose from 275 to 311 MiB and Prometheus from 121 to 134 MiB. The largest container was Kafka at about 460 MiB.
- **Host:** free Windows RAM stayed between 2.94 and 3.05 GB during the run (19 samples about 6 s apart), and the
  WSL2 VM between 4.08 and 4.10 GB. No container restarted or became unhealthy.

### 3.7 Limitations
- The application stack, the load generator and the measurement all ran on the same machine, so they compete for
  CPU and the numbers are not independent capacity figures.
- Each phase ran once. p99 from 30 sequential samples is close to the maximum, and the ingestion figures come
  from only 5 samples. The Tempo mean is below its median because one of the five traces arrived much sooner.
- CPU and memory figures are point-in-time snapshots, not peaks.
- The worker metrics window also covers other test activity (see 3.5).
- The background traffic generator (8 req/s) was running throughout.
- The checkout phase created real demo orders (1,216, plus 5 for the ingestion samples). The load also triggered
  an automatic "Traffic overload" incident (#63), which was later resolved through the normal incident workflow.
