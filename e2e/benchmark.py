"""Reproducible performance benchmark against the RUNNING stack. Writes e2e/results/benchmark.json.

    python e2e/benchmark.py [--api-requests 300] [--load-seconds 20] [--concurrency 16]

Measures (nothing is estimated):
  * API latency (p50/p95/p99/max) for representative authenticated endpoints, sequential requests.
  * API throughput under concurrency (cached /services).
  * Checkout throughput: POST /orders through order-service (5-service fan-out) under concurrency.
  * Telemetry ingestion latency: request -> trace queryable in Tempo; request -> log queryable in Loki.
  * Detection cycle and RCA durations from the worker's own Prometheus histograms.
  * CPU / memory per container (docker stats snapshot during the run).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import secrets
import statistics
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from client import API, admin

OUT = Path(__file__).resolve().parent / "results"
PROM = "http://localhost:9090"


def pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summary(ms: list[float]) -> dict:
    return {"n": len(ms), "p50_ms": round(pct(ms, 0.5), 2), "p95_ms": round(pct(ms, 0.95), 2), "p99_ms": round(pct(ms, 0.99), 2),
            "mean_ms": round(statistics.fmean(ms), 2), "max_ms": round(max(ms), 2)}


def api_latency(api, n: int) -> dict:  # noqa: ANN001
    inc = api.get("/incidents", params={"limit": 1}).json()["items"]
    endpoints = ["/health", "/services", "/metrics/overview", "/incidents", "/anomalies", "/dependencies",
                 "/logs?minutes=5&limit=50", "/traces?minutes=5&limit=20"]
    if inc:
        endpoints.append(f"/rca/{inc[0]['id']}")
    out = {}
    for ep in endpoints:
        lat = []
        for _ in range(n // len(endpoints)):
            t0 = time.perf_counter()
            r = api.get(ep)
            lat.append((time.perf_counter() - t0) * 1000)
            assert r.status_code in (200, 404), (ep, r.status_code)
        out[ep] = summary(lat)
    return out


async def load(url: str, method: str, body: dict | None, headers: dict, seconds: float, conc: int) -> dict:
    lat: list[float] = []
    codes: dict[str, int] = {}
    stop = time.perf_counter() + seconds

    async def worker(c: httpx.AsyncClient) -> None:
        while time.perf_counter() < stop:
            t0 = time.perf_counter()
            try:
                r = await c.request(method, url, json=body, headers=headers)
                k = str(r.status_code)
            except httpx.HTTPError as e:
                k = type(e).__name__
            lat.append((time.perf_counter() - t0) * 1000)
            codes[k] = codes.get(k, 0) + 1

    t_start = time.perf_counter()
    async with httpx.AsyncClient(timeout=30, limits=httpx.Limits(max_connections=conc)) as c:
        await asyncio.gather(*(worker(c) for _ in range(conc)))
    elapsed = time.perf_counter() - t_start
    ok = sum(v for k, v in codes.items() if k.startswith("2"))
    return {"concurrency": conc, "seconds": round(elapsed, 1), "requests": len(lat), "status_counts": codes,
            "throughput_rps": round(len(lat) / elapsed, 1), "successful_rps": round(ok / elapsed, 1), "latency": summary(lat)}


def ingestion_latency(samples: int) -> dict:
    trace_lat, log_lat = [], []
    for _ in range(samples):
        tid = secrets.token_hex(16)
        t0 = time.time()
        httpx.post("http://localhost:8100/orders", headers={"traceparent": f"00-{tid}-{secrets.token_hex(8)}-01"},
                   json={"user_id": 3, "items": [{"product_id": 2, "quantity": 1}]}, timeout=15)
        got_t = got_l = None
        while time.time() - t0 < 60 and (got_t is None or got_l is None):
            if got_t is None and httpx.get(f"http://localhost:3200/api/traces/{tid}").status_code == 200:
                got_t = time.time() - t0
            if got_l is None:
                end = time.time_ns()
                r = httpx.get("http://localhost:3100/loki/api/v1/query_range",
                              params={"query": f'{{service_name="payment-service"}} | trace_id="{tid}"', "start": end - 120 * 10**9, "end": end})
                if r.json()["data"]["result"]:
                    got_l = time.time() - t0
            time.sleep(0.25)
        if got_t:
            trace_lat.append(got_t * 1000)
        if got_l:
            log_lat.append(got_l * 1000)
    return {"note": "time from request completion to the record being queryable (includes SDK batch export ~1-5 s, "
                    "Collector batching 2 s, backend ingestion); polled every 250 ms",
            "trace_to_tempo": summary(trace_lat), "log_to_loki": summary(log_lat)}


def prom_hist(name: str) -> dict:
    out = {}
    for q, key in ((f"histogram_quantile(0.5, sum by (le) (rate({name}_bucket[30m])))", "p50_s"),
                   (f"histogram_quantile(0.95, sum by (le) (rate({name}_bucket[30m])))", "p95_s"),
                   (f"sum(increase({name}_count[30m]))", "count_30m"),
                   (f"sum(rate({name}_sum[30m])) / sum(rate({name}_count[30m]))", "mean_s")):
        r = httpx.get(f"{PROM}/api/v1/query", params={"query": q}).json()["data"]["result"]
        out[key] = round(float(r[0]["value"][1]), 4) if r and r[0]["value"][1] not in ("NaN",) else None
    return out


def docker_stats() -> list[dict]:
    cmd = ["docker", "stats", "--no-stream", "--format", "{{json .}}"]  # fixed argv, no user input
    raw = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout  # noqa: S603
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    return [{"container": r["Name"].replace("faultlens-", ""), "cpu": r["CPUPerc"], "mem": r["MemUsage"].split(" / ")[0]}
            for r in rows if r["Name"].startswith("faultlens-")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-requests", type=int, default=450)
    ap.add_argument("--load-seconds", type=float, default=20)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--ingestion-samples", type=int, default=10)
    a = ap.parse_args()
    api = admin()
    hdr = {"Authorization": f"Bearer {api.token}"}
    res: dict = {"generated_at": datetime.now(UTC).isoformat(),
                 "environment": {"os": platform.platform(), "cpu_count": os.cpu_count(), "python": platform.python_version(),
                                 "docker": subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],  # noqa: S603, S607
                                                          capture_output=True, text=True).stdout.strip(),
                                 "note": "All containers on one Docker Desktop (WSL2) VM on the same machine as the load generator."}}
    print("API latency…", flush=True)
    res["api_latency_sequential"] = api_latency(api, a.api_requests)
    print("API throughput…", flush=True)
    res["api_throughput_services"] = asyncio.run(load(f"{API}/services", "GET", None, hdr, a.load_seconds, a.concurrency))
    res["docker_stats_idle_traffic"] = docker_stats()
    print("Checkout throughput…", flush=True)
    body = {"user_id": 7, "items": [{"product_id": 9, "quantity": 1}]}
    checkout = asyncio.run(load("http://localhost:8100/orders", "POST", body, {}, a.load_seconds, a.concurrency))
    res["checkout_throughput"] = checkout | {"note": "each order = 1 order-service request + 4 downstream service calls + 5 DB transactions; "
                                                       "runs on top of the background traffic generator (~8 req/s)"}
    res["docker_stats_under_checkout_load_after"] = docker_stats()
    print("Ingestion latency…", flush=True)
    res["telemetry_ingestion"] = ingestion_latency(a.ingestion_samples)
    res["worker"] = {"detection_cycle": prom_hist("faultlens_detection_cycle_seconds"), "rca_investigation": prom_hist("faultlens_rca_duration_seconds"),
                     "event_bridge_lag": prom_hist("faultlens_event_bridge_lag_seconds")}
    OUT.mkdir(exist_ok=True)
    (OUT / "benchmark.json").write_text(json.dumps(res, indent=2))
    print(json.dumps({k: v for k, v in res.items() if not k.startswith("docker")}, indent=2))


if __name__ == "__main__":
    main()
