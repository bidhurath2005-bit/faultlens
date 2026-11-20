"""Detection cycle: Prometheus features -> detectors -> confirmation/de-dup -> anomaly candidates."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from app.core.redis import cached
from app.telemetry.clients import PrometheusClient
from app.telemetry.promql import feature_query

from .detectors import (
    DEFAULT_CONFIG,
    Detection,
    moving_average_detect,
    severity_for,
    threshold_detect,
    zscore_detect,
)
from .iforest import IF_FEATURES, IsolationForestDetector

log = logging.getLogger("faultlens.detection")

DETECT_METRICS = ["latency_p95", "error_rate", "rps", "cpu", "memory_rss", "memory_util", "db_pool_util",
                  "db_query_p95", "availability"]
STATISTICAL_METRICS = ["latency_p95", "error_rate", "rps", "cpu", "memory_rss", "db_query_p95"]
BASELINE_STEP_S = 15


@dataclass
class ServiceFeatures:
    service: str
    current: dict[str, float]
    history: dict[str, np.ndarray]
    history_ts: list[float] = field(default_factory=list)

    def vector(self) -> np.ndarray:
        return np.array([self.current.get(f, 0.0) for f in IF_FEATURES], dtype=float)

    def baseline_matrix(self) -> np.ndarray:
        n = min((len(self.history.get(f, [])) for f in IF_FEATURES), default=0)
        if n == 0:
            return np.empty((0, len(IF_FEATURES)))
        return np.column_stack([self.history[f][-n:] for f in IF_FEATURES])


def detect_service(sf: ServiceFeatures, config: dict[str, Any], iforest: IsolationForestDetector | None) -> list[Detection]:
    """Run every enabled detector on one service's features. Pure apart from the IF model cache."""
    methods = set(config.get("enabled_methods", DEFAULT_CONFIG["enabled_methods"]))
    out: list[Detection] = []
    min_pts = config.get("min_baseline_points", 20)
    if "threshold" in methods:
        for m in DETECT_METRICS:
            if m in sf.current and (d := threshold_detect(m, sf.current[m], config["thresholds"])):
                out.append(d)
    for m in STATISTICAL_METRICS:
        hist = sf.history.get(m)
        if m not in sf.current or hist is None or len(hist) < min_pts:
            continue
        if m == "rps" and sf.current[m] < config.get("min_rps", 0.2) and float(np.median(hist)) < config.get("min_rps", 0.2):
            continue
        if "zscore" in methods and (d := zscore_detect(m, hist, sf.current[m], config["zscore_threshold"])):
            out.append(d)
        if "moving_average" in methods and (d := moving_average_detect(m, hist, sf.current[m], config["ewma_alpha"],
                                                                        config["ewma_ratio_threshold"])):
            out.append(d)
    if "isolation_forest" in methods and iforest is not None:
        base = sf.baseline_matrix()
        if len(base) >= min_pts and all(f in sf.current for f in IF_FEATURES):
            if d := iforest.detect(sf.service, base, sf.vector()):
                out.append(d)
    return out


class FeatureFetcher:
    """Fetches current values (instant) and baseline history (range) for all services at once.
    The baseline range is cached in Redis for 60 s: it changes slowly and is the expensive query."""

    def __init__(self, prom: PrometheusClient) -> None:
        self.prom = prom

    async def fetch(self, now: datetime | None = None, baseline_minutes: int = 30, gap_minutes: int = 2) -> dict[str, ServiceFeatures]:
        now = now or datetime.now(UTC)
        cur_q = {m: feature_query(m, "1m") for m in DETECT_METRICS}
        currents = await asyncio.gather(*(self.prom.instant_by(q, at=now) for q in cur_q.values()))
        b_end = now - timedelta(minutes=gap_minutes)
        b_start = b_end - timedelta(minutes=baseline_minutes)

        async def _baseline() -> dict[str, dict[str, list]]:
            res = await asyncio.gather(*(self.prom.range_by(feature_query(m, "1m"), b_start, b_end, BASELINE_STEP_S)
                                         for m in DETECT_METRICS))
            return {m: {svc: pts for svc, pts in r.items()} for m, r in zip(DETECT_METRICS, res, strict=False)}

        bucket = int(now.timestamp() // 60)
        history_raw = await cached(f"baseline:v1:{bucket}:{baseline_minutes}", 90, _baseline)

        services: dict[str, ServiceFeatures] = {}
        for m, values in zip(DETECT_METRICS, currents, strict=False):
            for svc, v in values.items():
                services.setdefault(svc, ServiceFeatures(svc, {}, {})).current[m] = v
        for m, per_svc in history_raw.items():
            for svc, pts in per_svc.items():
                sf = services.setdefault(svc, ServiceFeatures(svc, {}, {}))
                sf.history[m] = np.array([p[1] for p in pts], dtype=float)
        # Services that have no 5xx simply produce no error series: treat as 0 rather than missing.
        for sf in services.values():
            if "rps" in sf.current:
                sf.current.setdefault("error_rate", 0.0)
        return services


@dataclass
class Candidate:
    service: str
    detection: Detection
    severity: str


class Confirmer:
    """Requires N consecutive positive cycles per (service, metric) before emitting, then a cooldown.
    State lives in Redis so it survives worker restarts and is shared by replicas."""

    def __init__(self, redis, confirm_cycles: int, cooldown_s: int, cycle_s: float) -> None:  # noqa: ANN001
        self.r, self.n, self.cooldown, self.cycle_s = redis, confirm_cycles, cooldown_s, cycle_s

    async def filter(self, service: str, dets: list[Detection]) -> list[Detection]:
        emitted = []
        for metric in {d.metric for d in dets}:
            group = [d for d in dets if d.metric == metric]
            key = f"anom:streak:{service}:{metric}"
            streak = await self.r.incr(key)
            await self.r.expire(key, int(self.cycle_s * 2.5))
            severe = any(d.method == "threshold" and severity_for(d) == "critical" for d in group)
            if streak < self.n and not severe:
                continue
            for d in group:
                cd_key = f"anom:cooldown:{service}:{metric}:{d.method}"
                if await self.r.set(cd_key, str(time.time()), nx=True, ex=self.cooldown):
                    emitted.append(d)
        return emitted
