"""SYNTHETIC labelled telemetry for detector training/evaluation.

THIS DATA IS SYNTHETIC. It is generated from simple statistical models of the demo services'
observed normal behaviour (latency ~ log-normal around 40 ms, ~5 rps, low error rate, ~0.1 core,
~110 MB RSS, small pool usage) with injected, labelled anomaly segments. It is used to measure
detector behaviour reproducibly; it does not prove performance on real production traffic.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

FEATURES = ["latency_p95", "error_rate", "rps", "cpu", "memory_rss", "db_pool_util", "db_query_p95"]
MB = 2**20

ANOMALY_TYPES = ["latency_spike", "error_spike", "traffic_spike", "cpu_saturation", "memory_leak", "pool_saturation",
                 "db_slowdown", "outage_traffic_drop"]
BENIGN_TYPES = ["gradual_traffic_growth", "noisy_latency"]


@dataclass
class Scenario:
    name: str
    kind: str              # anomaly type or benign type
    data: np.ndarray       # (T, F)
    labels: np.ndarray     # (T,) 1 = anomalous
    onset: int | None
    seed: int


def _normal(rng: np.random.Generator, t: int) -> np.ndarray:
    steps = np.arange(t)
    drift = 1 + 0.08 * np.sin(2 * np.pi * steps / 240)            # slow periodic load variation
    rps = np.clip(rng.normal(5.0 * drift, 0.5), 0.5, None)
    lat = 0.04 * np.exp(rng.normal(0, 0.18, t)) * (1 + 0.15 * (rps / 5 - 1))
    err = np.clip(rng.normal(0.004, 0.003, t), 0, None)
    cpu = np.clip(rng.normal(0.10 * drift, 0.02), 0.01, None)
    mem = 110 * MB + np.cumsum(rng.normal(0, 0.05 * MB, t)) + rng.normal(0, 0.5 * MB, t)
    pool = np.clip(rng.normal(0.25, 0.08, t), 0, 1)
    q = 0.004 * np.exp(rng.normal(0, 0.25, t))
    return np.column_stack([lat, err, rps, cpu, mem, pool, q])


def make_scenario(kind: str, seed: int, t: int = 480, onset: int = 300, length: int = 60) -> Scenario:
    rng = np.random.default_rng(seed)
    x = _normal(rng, t)
    y = np.zeros(t, dtype=int)
    seg = slice(onset, onset + length)
    n = length
    j = {f: i for i, f in enumerate(FEATURES)}
    if kind == "latency_spike":
        x[seg, j["latency_p95"]] += rng.normal(1.5, 0.2, n)
    elif kind == "error_spike":
        x[seg, j["error_rate"]] = np.clip(rng.normal(0.35, 0.05, n), 0, 1)
    elif kind == "traffic_spike":
        x[seg, j["rps"]] *= rng.normal(5.5, 0.4, n)
        x[seg, j["cpu"]] *= 2.5
        x[seg, j["latency_p95"]] *= 1.6
    elif kind == "cpu_saturation":
        x[seg, j["cpu"]] = np.clip(rng.normal(1.0, 0.05, n), 0, None)
        x[seg, j["latency_p95"]] *= 3.0
    elif kind == "memory_leak":
        x[seg, j["memory_rss"]] += np.linspace(16, 384, n) * MB
    elif kind == "pool_saturation":
        x[seg, j["db_pool_util"]] = 1.0
        x[seg, j["latency_p95"]] = rng.normal(2.0, 0.05, n)
        x[seg, j["error_rate"]] = np.clip(rng.normal(0.9, 0.05, n), 0, 1)
    elif kind == "db_slowdown":
        x[seg, j["db_query_p95"]] = rng.normal(0.6, 0.08, n)
        x[seg, j["latency_p95"]] += 0.6
        x[seg, j["db_pool_util"]] = np.clip(x[seg, j["db_pool_util"]] + 0.4, 0, 1)
    elif kind == "outage_traffic_drop":
        x[seg, j["rps"]] = np.clip(rng.normal(0.3, 0.1, n), 0, None)
        x[seg, j["error_rate"]] = 1.0
    elif kind == "gradual_traffic_growth":   # benign: +60% load over 3 minutes => not an incident
        x[onset:, j["rps"]] *= np.linspace(1.0, 1.6, t - onset)
        x[onset:, j["cpu"]] *= np.linspace(1.0, 1.4, t - onset)
        return Scenario(f"{kind}-{seed}", kind, x, y, None, seed)
    elif kind == "noisy_latency":             # benign: heavier latency noise, no shift
        x[:, j["latency_p95"]] *= np.exp(rng.normal(0, 0.3, t))
        return Scenario(f"{kind}-{seed}", kind, x, y, None, seed)
    else:
        raise ValueError(kind)
    y[seg] = 1
    return Scenario(f"{kind}-{seed}", kind, x, y, onset, seed)


def dataset(seeds: tuple[int, ...] = (11, 22, 33)) -> list[Scenario]:
    return [make_scenario(k, s) for k in ANOMALY_TYPES + BENIGN_TYPES for s in seeds]


DATASET_DESCRIPTION = (
    "SYNTHETIC: {n} scenarios x 480 steps (15 s each = 2 h), seeds {seeds}. 8 anomaly types (latency, errors, "
    "traffic, CPU, memory leak, pool saturation, DB slowdown, outage) each a labelled 60-step (15 min) segment "
    "starting at step 300, plus 2 benign variation types (gradual load growth, noisy latency) with no labels. "
    "Point-level labels."
)
