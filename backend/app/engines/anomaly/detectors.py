"""Anomaly detectors: static thresholds, moving average (EWMA), robust z-score, Isolation Forest.

All detectors are pure functions over a baseline history and a current observation so they can
be unit-tested and evaluated offline on labelled synthetic data (app/ml/evaluate.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

import numpy as np

Direction = Literal["up", "down", "both"]


@dataclass(frozen=True)
class MetricSpec:
    name: str
    label: str
    unit: str
    direction: Direction
    min_delta: float          # minimum absolute change to be operationally meaningful
    noise_floor: float        # minimum robust scale (avoids divide-by-~0 on flat series)
    threshold_key: str | None = None


METRIC_SPECS: dict[str, MetricSpec] = {
    s.name: s for s in [
        MetricSpec("latency_p95", "P95 latency", "s", "up", 0.15, 0.02, "latency_p95_s"),
        MetricSpec("error_rate", "Error rate", "ratio", "up", 0.03, 0.005, "error_rate"),
        MetricSpec("rps", "Request rate", "req/s", "both", 3.0, 0.5, None),
        MetricSpec("cpu", "CPU utilisation", "cores", "up", 0.25, 0.03, "cpu"),
        MetricSpec("memory_rss", "Memory (RSS)", "bytes", "up", 64 * 2**20, 4 * 2**20, None),
        MetricSpec("memory_util", "Memory utilisation", "ratio", "up", 0.15, 0.01, "memory_util"),
        MetricSpec("db_pool_util", "DB pool utilisation", "ratio", "up", 0.4, 0.05, "db_pool_util"),
        MetricSpec("db_query_p95", "DB query P95", "s", "up", 0.1, 0.005, "db_query_p95_s"),
        MetricSpec("availability", "Availability (probe)", "ratio", "down", 0.2, 0.01, "availability_min"),
    ]
}

DEFAULT_CONFIG: dict[str, Any] = {
    "thresholds": {"latency_p95_s": 1.0, "error_rate": 0.05, "cpu": 0.85, "memory_util": 0.85,
                   "db_pool_util": 0.95, "db_query_p95_s": 0.5, "availability_min": 0.9},
    "zscore_threshold": 4.0,
    "ewma_alpha": 0.1,
    "ewma_ratio_threshold": 2.0,
    "iforest_threshold": -0.09,
    "min_baseline_points": 20,
    "confirm_cycles": 2,
    "cooldown_s": 60,
    "min_rps": 0.2,
    "enabled_methods": ["threshold", "zscore", "moving_average", "isolation_forest"],
}


@dataclass
class Detection:
    metric: str
    method: str
    score: float           # method-specific, documented below; higher = more anomalous
    observed: float
    baseline: float | None
    details: dict[str, Any] = field(default_factory=dict)


def robust_stats(history: np.ndarray, noise_floor: float) -> tuple[float, float]:
    """Median and MAD-based robust scale (1.4826*MAD ~= sigma for normal data), floored."""
    med = float(np.median(history))
    mad = float(np.median(np.abs(history - med))) * 1.4826
    return med, max(mad, noise_floor, 0.05 * abs(med))


def _direction_ok(delta: float, spec: MetricSpec) -> bool:
    if spec.direction == "up":
        return delta >= spec.min_delta
    if spec.direction == "down":
        return -delta >= spec.min_delta
    return abs(delta) >= spec.min_delta


def threshold_detect(metric: str, current: float, thresholds: dict[str, float]) -> Detection | None:
    """Score = observed / threshold (>= 1 means breached). For floors (availability) inverted."""
    spec = METRIC_SPECS[metric]
    if spec.threshold_key is None or spec.threshold_key not in thresholds:
        return None
    thr = thresholds[spec.threshold_key]
    if spec.direction == "down":
        if current < thr:
            return Detection(metric, "threshold", round(thr / max(current, 1e-3), 3), current, thr, {"threshold": thr})
        return None
    if current >= thr:
        return Detection(metric, "threshold", round(current / thr, 3), current, thr, {"threshold": thr})
    return None


def zscore_detect(metric: str, history: np.ndarray, current: float, z_thr: float) -> Detection | None:
    """Robust z-score against the baseline window. Score = |z|."""
    spec = METRIC_SPECS[metric]
    if len(history) < 5:
        return None
    med, scale = robust_stats(history, spec.noise_floor)
    delta = current - med
    z = delta / scale
    if not _direction_ok(delta, spec):
        return None
    if (spec.direction == "up" and z >= z_thr) or (spec.direction == "down" and z <= -z_thr) or \
            (spec.direction == "both" and abs(z) >= z_thr):
        return Detection(metric, "zscore", round(abs(z), 3), current, med,
                         {"z": round(z, 3), "median": med, "robust_scale": scale, "n": int(len(history))})
    return None


def ewma(history: np.ndarray, alpha: float) -> float:
    m = float(history[0])
    for x in history[1:]:
        m = alpha * float(x) + (1 - alpha) * m
    return m


def moving_average_detect(metric: str, history: np.ndarray, current: float, alpha: float,
                          ratio_thr: float) -> Detection | None:
    """EWMA deviation. Score = current / EWMA (or EWMA / current for downward metrics)."""
    spec = METRIC_SPECS[metric]
    if len(history) < 5:
        return None
    m = ewma(history, alpha)
    delta = current - m
    if not _direction_ok(delta, spec):
        return None
    # Guard against flagging a *return to normal* (e.g. traffic dropping back after a spike): the value must
    # also deviate, in the same direction, from the robust long-term median of the baseline window.
    med = float(np.median(history))
    if (current - med) * delta <= 0 or abs(current - med) < spec.min_delta:
        return None
    base = max(abs(m), spec.noise_floor)
    ratio = (current / base) if delta > 0 else (base / max(abs(current), spec.noise_floor))
    if ratio >= ratio_thr:
        return Detection(metric, "moving_average", round(ratio, 3), current, m, {"ewma": m, "alpha": alpha})
    return None


def severity_for(det: Detection) -> str:
    m, v = det.metric, det.observed
    if m == "availability" and v < 0.5:
        return "critical"
    if m == "error_rate":
        return "critical" if v >= 0.25 else "high" if v >= 0.1 else "medium"
    if m in {"latency_p95", "db_query_p95"}:
        return "critical" if v >= 3 else "high" if v >= 1 else "medium"
    if m in {"db_pool_util"} and v >= 0.95:
        return "high"
    if m in {"cpu", "memory_util"}:
        return "high" if v >= 0.9 else "medium"
    if det.method == "zscore" and det.score >= 10:
        return "high"
    if m == "rps":
        return "medium"
    return "medium" if det.score >= 3 else "low"
