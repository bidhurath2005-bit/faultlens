"""RCA hypothesis scoring on constructed evidence bundles (one per supported failure class).

These are deterministic unit tests of the scoring model; live accuracy against injected faults is measured
separately by e2e/rca_eval.py.
"""
from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.engines.dependencies import DependencyGraph, Edge
from app.engines.logs import ServiceLogSummary
from app.engines.rca.engine import rank
from app.engines.rca.evidence import EvidenceBundle, MetricEvidence, robust_level
from app.engines.rca.hypotheses import INCONCLUSIVE_BELOW, confidence_label, generate, ramp
from app.engines.traces import TraceSummary

SERVICES = ["order-service", "user-service", "payment-service", "inventory-service", "notification-service"]
MB = 2**20
NORMAL = {"rps": (5, 5), "error_rate": (0, 0), "latency_p95": (0.05, 0.05), "cpu": (0.1, 0.1), "memory_rss": (110 * MB, 110 * MB),
          "memory_util": (0.2, 0.2), "db_pool_util": (0.2, 0.2), "db_pool_wait_p95": (0.005, 0.005), "db_pool_timeouts": (0, 0),
          "db_query_p95": (0.005, 0.005), "availability": (1, 1)}


def graph() -> DependencyGraph:
    edges = [Edge("traffic-generator", "order-service", 5, 0, 0.05)] + \
        [Edge("order-service", s, 5, 0, 0.02) for s in SERVICES if s != "order-service"]
    return DependencyGraph(edges)


def bundle(changes: dict[str, dict[str, float]], logs: dict | None = None, traces: TraceSummary | None = None,
           peer_err: dict | None = None) -> EvidenceBundle:
    now = datetime.now(UTC)
    ev = EvidenceBundle(now - timedelta(minutes=3), now, now - timedelta(minutes=24), now - timedelta(minutes=4), SERVICES)
    for s in SERVICES:
        ev.metrics[s] = {f: MetricEvidence(cur, base) for f, (cur, base) in NORMAL.items()}
        for f, cur in changes.get(s, {}).items():
            ev.metrics[s][f] = MetricEvidence(cur, NORMAL[f][1])
    for s, cats in (logs or {}).items():
        ls = ServiceLogSummary(s)
        for c, n in cats.items():
            if isinstance(n, dict):  # peer attribution {"peer:x": {...}}
                for peer, pc in n.items():
                    ls.peer_categories[peer].update(pc)
            else:
                ls.categories[c] = n
                ls.excess[c] = n
        ev.logs[s] = ls
    ev.traces = traces or TraceSummary()
    ev.peer_error_ratio = peer_err or {}
    ev.graph = graph()
    ev.sources = {"metrics": "ok", "logs": "ok", "traces": "ok", "dependencies": "ok"}
    return ev


def traces(lat: dict | None = None, origins: dict | None = None, reasons: dict | None = None, timeouts: dict | None = None) -> TraceSummary:
    t = TraceSummary(analysed=10, slow_analysed=8, error_analysed=6)
    t.latency_breakdown = lat or {}
    t.latency_share = {s: sum(v.values()) for s, v in (lat or {}).items()}
    t.error_origin_share = origins or {}
    t.origin_reasons = reasons or {}
    t.peer_timeouts = timeouts or {}
    return t


CASES = {
    "db_pool_exhaustion": bundle(
        {"payment-service": {"db_pool_util": 1.0, "db_pool_wait_p95": 2.0, "db_pool_timeouts": 3, "latency_p95": 2.0, "error_rate": 0.9},
         "order-service": {"latency_p95": 2.1, "error_rate": 0.6}},
        logs={"payment-service": {"pool_exhausted": 120}, "order-service": {"x": {"payment-service": {"downstream_failed": 50}}}},
        traces=traces({"payment-service": {"db_pool": 0.9}}, {"payment-service": 1.0}, {"payment-service": {"db_pool": 12}}),
        peer_err={"payment-service": 0.9}),
    "db_query_latency": bundle(
        {"inventory-service": {"db_query_p95": 0.65, "latency_p95": 0.8}, "order-service": {"latency_p95": 0.9}},
        logs={"inventory-service": {"slow_query": 80}},
        traces=traces({"inventory-service": {"db": 0.7, "app": 0.05}, "order-service": {"app": 0.05}})),
    "service_timeout": bundle(
        {"payment-service": {"latency_p95": 3.6}, "order-service": {"latency_p95": 3.1, "error_rate": 0.5}},
        logs={"order-service": {"x": {"payment-service": {"timeout": 40}}}},
        traces=traces({"payment-service": {"app": 0.85}, "order-service": {"app": 0.05}}, timeouts={"payment-service": 9})),
    "high_cpu": bundle(
        {"inventory-service": {"cpu": 1.0, "latency_p95": 0.9}, "order-service": {"latency_p95": 1.0}},
        logs={"inventory-service": {"cpu_starvation": 30}},
        traces=traces({"inventory-service": {"app": 0.8}})),
    "memory_pressure": bundle(
        {"notification-service": {"memory_rss": 480 * MB, "memory_util": 0.93}},
        logs={"notification-service": {"memory_high": 12}}),
    "downstream_failure": bundle(
        {"inventory-service": {"availability": 0.0, "error_rate": 1.0}, "order-service": {"error_rate": 0.6}},
        logs={"order-service": {"x": {"inventory-service": {"downstream_failed": 60}}}},
        traces=traces(origins={"inventory-service": 1.0}, reasons={"inventory-service": {"app": 10}}),
        peer_err={"inventory-service": 1.0}),
    "traffic_overload": bundle(
        {s: {"rps": 30, "cpu": 0.5, "latency_p95": 0.3} for s in SERVICES}),
    "http_500_spike": bundle(
        {"payment-service": {"error_rate": 0.6}, "order-service": {"error_rate": 0.55}},
        logs={"payment-service": {"unhandled_exception": 90}},
        traces=traces(origins={"payment-service": 1.0}, reasons={"payment-service": {"app": 20}}),
        peer_err={"payment-service": 0.6}),
}
EXPECTED_SERVICE = {"db_pool_exhaustion": "payment-service", "db_query_latency": "inventory-service", "service_timeout": "payment-service",
                    "high_cpu": "inventory-service", "memory_pressure": "notification-service", "downstream_failure": "inventory-service",
                    "traffic_overload": "order-service", "http_500_spike": "payment-service"}


@pytest.mark.parametrize("cause", list(CASES))
def test_top_hypothesis_matches_failure_class(cause):
    top, conclusive = rank(generate(CASES[cause]))
    assert conclusive
    assert (top[0].cause_type, top[0].service) == (cause, EXPECTED_SERVICE[cause]), [(h.cause_type, h.service, h.score) for h in top]


def test_scores_are_transparent_weighted_sums():
    for cause, ev in CASES.items():
        for h in generate(ev):
            assert abs(sum(s.weight for s in h.signals) - 1.0) < 1e-9, (cause, h.cause_type)
            assert h.score == pytest.approx(min(1.0, sum(s.weight * s.strength for s in h.signals) * h.locality), abs=1e-4)
            assert 0 <= h.score <= 1


def test_symptomatic_upstream_service_is_down_weighted_by_locality():
    hyps = generate(CASES["db_pool_exhaustion"])
    upstream = [h for h in hyps if h.service == "order-service"]
    assert all(h.locality < 1 for h in upstream if h.cause_type in {"http_500_spike", "service_timeout"})
    assert all(h.score < hyps[0].score for h in upstream)


def test_no_evidence_means_no_hypotheses_and_inconclusive():
    top, conclusive = rank(generate(bundle({})))
    assert top == [] and not conclusive


def test_weak_evidence_is_inconclusive():
    ev = bundle({"payment-service": {"latency_p95": 0.25}})  # mild bump, nothing else
    top, conclusive = rank(generate(ev))
    assert not conclusive or top[0].score >= INCONCLUSIVE_BELOW
    assert all(h.score < 0.5 for h in top)


def test_missing_evidence_sources_lower_scores_rather_than_guessing():
    full = rank(generate(CASES["db_pool_exhaustion"]))[0][0].score
    ev = CASES["db_pool_exhaustion"]
    degraded = bundle({"payment-service": {"db_pool_util": 1.0, "db_pool_wait_p95": 2.0, "latency_p95": 2.0, "error_rate": 0.9}})
    degraded.sources["logs"] = degraded.sources["traces"] = "unavailable: down"
    assert rank(generate(degraded))[0][0].score < full
    assert ev.sources["logs"] == "ok"


def test_helpers():
    assert ramp(None, 0, 1) == 0 and ramp(5, 0, 1) == 1 and ramp(0.5, 0, 1) == 0.5
    assert confidence_label(0.8) == "high" and confidence_label(0.1) == "insufficient"
    assert robust_level([0, 0, 0, 10, 10, 10], baseline=0) == 10
    assert robust_level([], 1) is None


def test_rca_engine_never_reads_fault_injection_records():
    """Diagnoses must come from telemetry only: the RCA package may not import the fault-injection model."""
    rca_dir = Path(__file__).resolve().parents[1] / "app" / "engines"
    for py in rca_dir.rglob("*.py"):
        tree = ast.parse(py.read_text())
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        assert "FaultInjection" not in names, py
        assert "fault_injections" not in py.read_text(), py
