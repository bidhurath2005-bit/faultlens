"""Evidence model and collector: metrics (Prometheus), logs (Loki), traces (Tempo), dependencies."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from app.engines.dependencies import DependencyGraph, observed_graph
from app.engines.logs import ServiceLogSummary, summarise, with_excess
from app.engines.traces import TraceSummary, analyse_trace, summarise_traces
from app.telemetry.clients import Telemetry
from app.telemetry.promql import CLIENT_CALLS_BY_PEER, CLIENT_ERRORS_BY_PEER, feature_query

log = logging.getLogger("faultlens.rca.evidence")

RCA_FEATURES = ["rps", "error_rate", "latency_p95", "cpu", "memory_rss", "memory_util", "db_pool_util",
                "db_pool_wait_p95", "db_pool_timeouts", "db_query_p95", "availability"]


@dataclass
class MetricEvidence:
    current: float | None     # robust "incident level": mean of the upper half of window samples
    baseline: float | None    # median of baseline samples
    series: list[tuple[float, float]] = field(default_factory=list)

    @property
    def delta(self) -> float:
        if self.current is None or self.baseline is None:
            return 0.0
        return self.current - self.baseline

    @property
    def ratio(self) -> float:
        if self.current is None or not self.baseline:
            return 1.0
        return self.current / max(abs(self.baseline), 1e-9)

    def to_dict(self) -> dict:
        return {"current": self.current, "baseline": self.baseline, "delta": self.delta, "ratio": round(self.ratio, 3),
                "series": [(round(t), round(v, 6)) for t, v in self.series]}


@dataclass
class EvidenceBundle:
    window_start: datetime
    window_end: datetime
    baseline_start: datetime
    baseline_end: datetime
    services: list[str]
    metrics: dict[str, dict[str, MetricEvidence]] = field(default_factory=dict)
    peer_error_ratio: dict[str, float] = field(default_factory=dict)
    logs: dict[str, ServiceLogSummary] = field(default_factory=dict)
    traces: TraceSummary = field(default_factory=TraceSummary)
    graph: DependencyGraph = field(default_factory=DependencyGraph)
    sources: dict[str, str] = field(default_factory=dict)  # source -> ok | unavailable: reason
    log_samples: list[dict] = field(default_factory=list)
    onset: datetime | None = None

    def m(self, svc: str, feature: str) -> MetricEvidence:
        return self.metrics.get(svc, {}).get(feature) or MetricEvidence(None, None)

    @property
    def window_minutes(self) -> float:
        return (self.window_end - self.window_start).total_seconds() / 60


def robust_level(values: list[float], baseline: float | None = None) -> float | None:
    """Incident level = mean of the half of window samples that deviates most from the baseline.

    The incident window spans onset and possibly recovery, so a plain mean dilutes the signal while a
    max is noise-prone. Taking the most-deviating half captures sustained deviations in *either*
    direction (e.g. availability dropping) while ignoring single-sample spikes."""
    if not values:
        return None
    arr = np.array(values, dtype=float)
    ref = baseline if baseline is not None else float(np.median(arr))
    order = np.argsort(-np.abs(arr - ref))
    top = arr[order[: max(1, len(arr) // 2)]]
    return float(np.mean(top))


class EvidenceCollector:
    def __init__(self, tel: Telemetry, known_services: list[str]) -> None:
        self.tel = tel
        self.known = known_services

    async def collect(self, window_start: datetime, window_end: datetime, baseline_minutes: int = 20,
                      onset: datetime | None = None) -> EvidenceBundle:
        b_end = window_start - timedelta(minutes=1)
        b_start = b_end - timedelta(minutes=baseline_minutes)
        ev = EvidenceBundle(window_start, window_end, b_start, b_end, list(self.known), onset=onset)
        await asyncio.gather(self._metrics(ev), self._logs(ev), self._traces(ev), self._graph(ev))
        return ev

    async def _metrics(self, ev: EvidenceBundle) -> None:
        try:
            # 30 s windows during the incident (responsive to a young incident); 1 m for the smoother baseline.
            win = await asyncio.gather(*(self.tel.prom.range_by(feature_query(f, "30s"), ev.window_start, ev.window_end, 10)
                                         for f in RCA_FEATURES))
            base = await asyncio.gather(*(self.tel.prom.range_by(feature_query(f, "1m"), ev.baseline_start, ev.baseline_end, 30)
                                          for f in RCA_FEATURES))
            for f, w, b in zip(RCA_FEATURES, win, base, strict=False):
                for svc in set(w) | set(b):
                    bvals = [v for _, v in b.get(svc, [])]
                    bmed = float(np.median(bvals)) if bvals else None
                    if f in ("error_rate", "db_pool_timeouts") and bmed is None and svc in w:
                        bmed = 0.0
                    pts = w.get(svc, [])
                    # Incident level is measured from the onset onwards; the pre-onset part of the window is
                    # kept for display only (otherwise a young incident is diluted by normal samples).
                    cut = ev.onset.timestamp() if ev.onset else None
                    active = [v for t, v in pts if cut is None or t >= cut]
                    if len(active) < 2:
                        active = [v for _, v in pts]
                    ev.metrics.setdefault(svc, {})[f] = MetricEvidence(robust_level(active, bmed), bmed, pts[-40:])
            win_minutes = max(1, int(ev.window_minutes))
            errs, calls = await asyncio.gather(
                self.tel.prom.instant(CLIENT_ERRORS_BY_PEER.format(w=f"{win_minutes}m"), ev.window_end),
                self.tel.prom.instant(CLIENT_CALLS_BY_PEER.format(w=f"{win_minutes}m"), ev.window_end))
            e_by, c_by = {}, {}
            for r in errs:
                p = r["metric"].get("peer_service")
                e_by[p] = e_by.get(p, 0.0) + float(r["value"][1])
            for r in calls:
                p = r["metric"].get("peer_service")
                c_by[p] = c_by.get(p, 0.0) + float(r["value"][1])
            ev.peer_error_ratio = {p: (e_by.get(p, 0.0) / c) for p, c in c_by.items() if p and c > 0}
            ev.sources["metrics"] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.warning("metric evidence unavailable", extra={"error": str(exc)})
            ev.sources["metrics"] = f"unavailable: {exc}"

    async def _logs(self, ev: EvidenceBundle) -> None:
        q = '{service_namespace="faultlens-demo"} | severity_text=~"ERROR|WARNING|WARN|CRITICAL"'
        try:
            window, baseline = await asyncio.gather(
                self.tel.loki.query_range(q, ev.window_start, ev.window_end, limit=2000),
                self.tel.loki.query_range(q, ev.baseline_start, ev.baseline_end, limit=2000))
            wsum = summarise(window)
            bsum = summarise(baseline)
            ev.logs = with_excess(wsum, bsum, ev.window_minutes,
                                  (ev.baseline_end - ev.baseline_start).total_seconds() / 60)
            ev.log_samples = [r.as_dict() for r in window[:40]]
            ev.sources["logs"] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.warning("log evidence unavailable", extra={"error": str(exc)})
            ev.sources["logs"] = f"unavailable: {exc}"

    async def _traces(self, ev: EvidenceBundle) -> None:
        known = set(self.known)
        try:
            slow_hits, err_hits = await asyncio.gather(
                self.tel.tempo.search('{ kind = server && duration > 400ms }', ev.window_start, ev.window_end, limit=25),
                self.tel.tempo.search('{ status = error }', ev.window_start, ev.window_end, limit=25))
            ids_slow = [h["trace_id"] for h in slow_hits if h["trace_id"]][:15]
            ids_err = [h["trace_id"] for h in err_hits if h["trace_id"]][:15]
            uniq = list(dict.fromkeys(ids_slow + ids_err))
            fetched = await asyncio.gather(*(self.tel.tempo.get_trace(t) for t in uniq), return_exceptions=True)
            analysed = {}
            for tid, spans in zip(uniq, fetched, strict=False):
                if isinstance(spans, Exception) or not spans:
                    continue
                if (a := analyse_trace(spans, known)) is not None:
                    analysed[tid] = a
            ev.traces = summarise_traces([analysed[t] for t in ids_slow if t in analysed],
                                         [analysed[t] for t in ids_err if t in analysed])
            ev.sources["traces"] = "ok"
        except Exception as exc:  # noqa: BLE001
            log.warning("trace evidence unavailable", extra={"error": str(exc)})
            ev.sources["traces"] = f"unavailable: {exc}"

    async def _graph(self, ev: EvidenceBundle) -> None:
        try:
            minutes = max(2, int(ev.window_minutes) + 1)
            ev.graph = await observed_graph(self.tel.prom, f"{minutes}m", ev.window_end)
            ev.sources["dependencies"] = "ok"
        except Exception as exc:  # noqa: BLE001
            ev.sources["dependencies"] = f"unavailable: {exc}"


def bundle_summary(ev: EvidenceBundle, focus: list[str]) -> dict[str, Any]:
    """Compact, JSON-serialisable evidence for persistence and the UI."""
    return {
        "window": {"start": ev.window_start.isoformat(), "end": ev.window_end.isoformat()},
        "baseline": {"start": ev.baseline_start.isoformat(), "end": ev.baseline_end.isoformat()},
        "sources": ev.sources,
        "metrics": {s: {f: m.to_dict() for f, m in ev.metrics.get(s, {}).items()} for s in focus},
        "peer_error_ratio": {k: round(v, 4) for k, v in ev.peer_error_ratio.items()},
        "logs": {s: ls.to_dict() for s, ls in ev.logs.items()},
        "log_samples": ev.log_samples,
        "traces": ev.traces.to_dict(),
        "dependencies": ev.graph.to_dict(),
    }
