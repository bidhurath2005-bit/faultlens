"""Root-cause hypotheses and the transparent scoring model.

score(h) = locality(h) * sum_i( weight_i * strength_i )

  * strength_i in [0, 1] is a documented, piecewise-linear function (`ramp`) of one observable.
  * weights of a hypothesis sum to 1.0, so the raw score is in [0, 1].
  * A hypothesis is only generated if at least one of its *primary* signals reaches 0.3
    (no evidence => no hypothesis; nothing is guessed).
  * locality in [0.4, 1] down-weights symptom-type causes (latency, 5xx) in a service whose
    downstream dependencies are themselves anomalous, unless traces attribute the time/errors
    to the service itself. Resource causes (CPU, memory, pool, DB) need evidence on the service
    itself, so they are not discounted.
  * If the best score is below INCONCLUSIVE_BELOW the investigation is reported as inconclusive.

Every signal is returned with value, strength, weight and contribution so the UI can show
exactly why a hypothesis ranks where it does.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.engines.logs import peer_mentions

from .evidence import EvidenceBundle

INCONCLUSIVE_BELOW = 0.35
PRIMARY_GATE = 0.3
MB = 2**20


def ramp(x: float | None, lo: float, hi: float) -> float:
    """0 at/below lo, 1 at/above hi, linear in between. None => 0 (missing evidence is not evidence)."""
    if x is None:
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def confidence_label(score: float) -> str:
    if score >= 0.7:
        return "high"
    if score >= 0.5:
        return "medium"
    if score >= INCONCLUSIVE_BELOW:
        return "low"
    return "insufficient"


@dataclass
class Signal:
    name: str
    description: str
    weight: float
    strength: float
    value: str
    source: str            # metrics | logs | traces | dependencies | probe
    primary: bool = False

    @property
    def contribution(self) -> float:
        return self.weight * self.strength

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "weight": self.weight,
                "strength": round(self.strength, 3), "contribution": round(self.contribution, 4),
                "value": self.value, "source": self.source, "primary": self.primary}


@dataclass
class Hypothesis:
    cause_type: str
    service: str
    title: str
    signals: list[Signal]
    locality: float = 1.0
    locality_reason: str = "no anomalous downstream dependency"
    related_services: list[str] = field(default_factory=list)

    @property
    def raw_score(self) -> float:
        return sum(s.contribution for s in self.signals)

    @property
    def score(self) -> float:
        return round(min(1.0, self.raw_score * self.locality), 4)

    @property
    def gated(self) -> bool:
        return any(s.primary and s.strength >= PRIMARY_GATE for s in self.signals)


CAUSE_TITLES = {
    "db_pool_exhaustion": "Database connection pool exhaustion",
    "db_query_latency": "Increased database query latency",
    "service_timeout": "Service slowdown causing timeouts",
    "high_cpu": "High CPU utilisation",
    "memory_pressure": "Memory pressure",
    "downstream_failure": "Service unavailable (downstream failure)",
    "traffic_overload": "Traffic overload",
    "http_500_spike": "Application errors (HTTP 5xx spike)",
}


# ---------------------------------------------------------------- observable strengths
class Obs:
    """Strength functions over the evidence bundle (one place, documented thresholds)."""

    def __init__(self, ev: EvidenceBundle) -> None:
        self.ev = ev

    def lat_up(self, s: str) -> float:
        m = self.ev.m(s, "latency_p95")
        return min(ramp(m.delta, 0.1, 0.8), ramp(m.ratio, 1.3, 3.0))

    def err_up(self, s: str) -> float:
        m = self.ev.m(s, "error_rate")
        return ramp(m.delta, 0.02, 0.25)

    def cpu_up(self, s: str) -> float:
        return ramp(self.ev.m(s, "cpu").delta, 0.2, 0.6)

    def mem_up(self, s: str) -> float:
        return ramp(self.ev.m(s, "memory_rss").delta, 48 * MB, 200 * MB)

    def mem_util(self, s: str) -> float:
        return ramp(self.ev.m(s, "memory_util").current, 0.5, 0.85)

    def pool_util(self, s: str) -> float:
        m = self.ev.m(s, "db_pool_util")
        return min(ramp(m.current, 0.7, 0.98), ramp(m.delta, 0.2, 0.5))

    def pool_wait(self, s: str) -> float:
        return ramp(self.ev.m(s, "db_pool_wait_p95").current, 0.1, 1.5)

    def pool_timeouts(self, s: str) -> float:
        return ramp(self.ev.m(s, "db_pool_timeouts").current, 0.0, 0.5)

    def query_up(self, s: str) -> float:
        m = self.ev.m(s, "db_query_p95")
        return min(ramp(m.delta, 0.05, 0.4), ramp(m.ratio, 2.0, 6.0))

    def rps_up(self, s: str) -> float:
        m = self.ev.m(s, "rps")
        return min(ramp(m.ratio, 1.5, 3.5), ramp(m.delta, 3.0, 15.0))

    def avail_down(self, s: str) -> float:
        m = self.ev.m(s, "availability")
        if m.current is None:
            return 0.0
        return ramp((m.baseline if m.baseline is not None else 1.0) - m.current, 0.1, 0.6)

    def log_excess(self, s: str, cat: str, full: float) -> float:
        ls = self.ev.logs.get(s)
        return ramp(ls.excess.get(cat, 0.0) if ls else 0.0, 0.0, full)

    def peer_logs(self, s: str, cats: set[str], full: float) -> float:
        return ramp(peer_mentions(self.ev.logs, s, cats), 0.0, full)

    def trace_lat(self, s: str) -> float:
        """Share of slow-trace self time spent in the service's *own application code* (excludes DB, pool
        waits and network), i.e. evidence that the service itself is slow at processing."""
        if self.ev.traces.slow_analysed < 3:
            return 0.0
        return ramp(self.ev.traces.latency_breakdown.get(s, {}).get("app", 0.0), 0.2, 0.6)

    def trace_err(self, s: str) -> float:
        if sum(1 for _ in self.ev.traces.error_origin_share) == 0:
            return 0.0
        return ramp(self.ev.traces.error_origin_share.get(s, 0.0), 0.2, 0.7)

    def trace_db(self, s: str) -> float:
        return ramp(self.ev.traces.latency_breakdown.get(s, {}).get("db", 0.0), 0.1, 0.4)

    def trace_pool(self, s: str) -> float:
        share = self.ev.traces.latency_breakdown.get(s, {}).get("db_pool", 0.0)
        origins = self.ev.traces.origin_reasons.get(s, {}).get("db_pool", 0)
        return max(ramp(share, 0.1, 0.4), ramp(origins, 0, 3))

    def client_err_to(self, s: str) -> float:
        return ramp(self.ev.peer_error_ratio.get(s, 0.0), 0.05, 0.5)

    def symptom(self, s: str) -> float:
        return max(self.lat_up(s), self.err_up(s), self.avail_down(s))


def _fmt(v: float | None, unit: str = "") -> str:
    if v is None:
        return "n/a"
    if unit == "s":
        return f"{v * 1000:.0f} ms"
    if unit == "%":
        return f"{v * 100:.1f}%"
    if unit == "MB":
        return f"{v / MB:.0f} MB"
    return f"{v:.3g}{unit}"


def _cmp(ev: EvidenceBundle, s: str, f: str, unit: str) -> str:
    m = ev.m(s, f)
    return f"{_fmt(m.current, unit)} (baseline {_fmt(m.baseline, unit)})"


# ---------------------------------------------------------------- hypothesis builders
def _pool(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    return Hypothesis("db_pool_exhaustion", s, f"{CAUSE_TITLES['db_pool_exhaustion']} in {s}", [
        Signal("pool_saturation", "Connections in use vs pool size", 0.25, o.pool_util(s), _cmp(ev, s, "db_pool_util", "%"), "metrics", True),
        Signal("pool_wait", "P95 time waiting for a pooled connection", 0.20, o.pool_wait(s), _fmt(ev.m(s, "db_pool_wait_p95").current, "s"), "metrics", True),
        Signal("pool_timeout_logs", "Excess 'pool exhausted' log lines", 0.20, o.log_excess(s, "pool_exhausted", 5), str(round(ev.logs[s].excess.get("pool_exhausted", 0)) if s in ev.logs else 0), "logs", True),
        Signal("pool_spans", "Trace time/errors in db.pool.acquire spans", 0.15, o.trace_pool(s), str(ev.traces.origin_reasons.get(s, {}).get("db_pool", 0)) + " error origins", "traces"),
        Signal("queries_not_slow", "Query execution itself is not slower (discriminates from slow DB)", 0.20, 1.0 - o.query_up(s), _cmp(ev, s, "db_query_p95", "s"), "metrics"),
    ])


def _dbslow(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    return Hypothesis("db_query_latency", s, f"{CAUSE_TITLES['db_query_latency']} in {s}", [
        Signal("query_latency", "DB query P95 vs baseline", 0.40, o.query_up(s), _cmp(ev, s, "db_query_p95", "s"), "metrics", True),
        Signal("slow_query_logs", "Excess 'slow query' log lines", 0.25, o.log_excess(s, "slow_query", 10), str(round(ev.logs[s].excess.get("slow_query", 0)) if s in ev.logs else 0), "logs", True),
        Signal("db_span_time", "Share of slow-trace self time spent in DB spans", 0.15, o.trace_db(s), f"{ev.traces.latency_breakdown.get(s, {}).get('db', 0):.0%}", "traces"),
        Signal("service_latency", "Service P95 latency increase", 0.10, o.lat_up(s), _cmp(ev, s, "latency_p95", "s"), "metrics"),
        Signal("no_pool_timeouts", "Pool not timing out (discriminates from leaked connections)", 0.10, 1.0 - o.pool_timeouts(s), _fmt(ev.m(s, "db_pool_timeouts").current, "/s"), "metrics"),
    ])


def _timeout(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    resource = max(o.cpu_up(s), o.query_up(s), o.pool_util(s), o.pool_wait(s), o.pool_timeouts(s), o.mem_up(s))
    callers_timeouts = max(o.peer_logs(s, {"timeout"}, 10), ramp(ev.traces.peer_timeouts.get(s, 0), 0, 3))
    return Hypothesis("service_timeout", s, f"{CAUSE_TITLES['service_timeout']} in {s}", [
        Signal("latency", "Service P95 latency vs baseline", 0.30, o.lat_up(s), _cmp(ev, s, "latency_p95", "s"), "metrics", True),
        Signal("self_time", "Share of slow-trace self time spent in this service's own code", 0.35, o.trace_lat(s), f"{ev.traces.latency_breakdown.get(s, {}).get('app', 0):.0%}", "traces", True),
        Signal("caller_timeouts", "Callers report timeouts calling this service", 0.15, callers_timeouts, str(ev.traces.peer_timeouts.get(s, 0)) + " timed-out client spans", "logs"),
        Signal("no_resource_cause", "No CPU/DB/memory explanation on this service", 0.20, 1.0 - resource, f"max resource signal {resource:.2f}", "metrics"),
    ])


def _cpu(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    return Hypothesis("high_cpu", s, f"{CAUSE_TITLES['high_cpu']} in {s}", [
        Signal("cpu", "Process CPU (cores) vs baseline", 0.45, o.cpu_up(s), _cmp(ev, s, "cpu", ""), "metrics", True),
        Signal("loop_lag_logs", "Excess event-loop-lag / CPU starvation logs", 0.25, o.log_excess(s, "cpu_starvation", 5), str(round(ev.logs[s].excess.get("cpu_starvation", 0)) if s in ev.logs else 0), "logs", True),
        Signal("latency", "Latency increase consistent with CPU saturation", 0.15, o.lat_up(s), _cmp(ev, s, "latency_p95", "s"), "metrics"),
        Signal("traffic_not_up", "Request rate not elevated (CPU not explained by load)", 0.15, 1.0 - o.rps_up(s), _cmp(ev, s, "rps", "/s"), "metrics"),
    ])


def _mem(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    return Hypothesis("memory_pressure", s, f"{CAUSE_TITLES['memory_pressure']} in {s}", [
        Signal("rss_growth", "Resident memory growth vs baseline", 0.45, o.mem_up(s), _cmp(ev, s, "memory_rss", "MB"), "metrics", True),
        Signal("limit_proximity", "RSS as a fraction of the container limit", 0.25, o.mem_util(s), _fmt(ev.m(s, "memory_util").current, "%"), "metrics", True),
        Signal("memory_logs", "Excess 'memory usage high' logs", 0.20, o.log_excess(s, "memory_high", 3), str(round(ev.logs[s].excess.get("memory_high", 0)) if s in ev.logs else 0), "logs"),
        Signal("traffic_not_up", "Request rate not elevated", 0.10, 1.0 - o.rps_up(s), _cmp(ev, s, "rps", "/s"), "metrics"),
    ])


def _down(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    return Hypothesis("downstream_failure", s, f"{CAUSE_TITLES['downstream_failure']}: {s}", [
        Signal("probe_availability", "Synthetic health-probe availability drop", 0.35, o.avail_down(s), _cmp(ev, s, "availability", "%"), "probe", True),
        Signal("caller_errors", "Fraction of calls to this service failing (caller side)", 0.25, o.client_err_to(s), f"{ev.peer_error_ratio.get(s, 0):.0%}", "metrics", True),
        Signal("caller_logs", "Callers log failures naming this service", 0.15, o.peer_logs(s, {"downstream_failed", "unreachable"}, 10), "", "logs"),
        Signal("error_origin", "Share of trace error origins located in this service", 0.25, o.trace_err(s), f"{ev.traces.error_origin_share.get(s, 0):.0%}", "traces"),
    ])


def _http500(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    return Hypothesis("http_500_spike", s, f"{CAUSE_TITLES['http_500_spike']} in {s}", [
        Signal("error_rate", "Server 5xx ratio vs baseline", 0.35, o.err_up(s), _cmp(ev, s, "error_rate", "%"), "metrics", True),
        Signal("error_origin", "Share of trace error origins located in this service", 0.25, o.trace_err(s), f"{ev.traces.error_origin_share.get(s, 0):.0%}", "traces", True),
        Signal("exception_logs", "Excess unhandled-exception logs in this service", 0.25, o.log_excess(s, "unhandled_exception", 10), str(round(ev.logs[s].excess.get("unhandled_exception", 0)) if s in ev.logs else 0), "logs"),
        Signal("still_available", "Health probe still passing (process up, requests failing)", 0.15, 1.0 - o.avail_down(s), _cmp(ev, s, "availability", "%"), "probe"),
    ])


def _traffic(ev: EvidenceBundle, o: Obs, s: str) -> Hypothesis:
    others = [x for x in ev.services if x != s]
    spread = sum(1 for x in others if o.rps_up(x) >= 0.3) / max(1, len(others))
    fault_specific = max(max((o.pool_util(x), o.query_up(x), o.avail_down(x), o.trace_err(x), o.mem_up(x))) for x in ev.services)
    return Hypothesis("traffic_overload", s, f"{CAUSE_TITLES['traffic_overload']} entering via {s}", [
        Signal("rps", "Inbound request rate vs baseline", 0.40, o.rps_up(s), _cmp(ev, s, "rps", "/s"), "metrics", True),
        Signal("load_spread", "Fraction of other services whose request rate also rose", 0.15, spread, f"{spread:.0%}", "metrics"),
        Signal("saturation", "Latency or CPU rising with load", 0.20, max(max(o.lat_up(x), o.cpu_up(x)) for x in ev.services), "", "metrics"),
        Signal("no_fault_specific", "No fault-specific signature (pool, DB, outage, error origin, memory)", 0.25, 1.0 - fault_specific, f"max fault signal {fault_specific:.2f}", "metrics"),
    ])


BUILDERS: list[Callable[[EvidenceBundle, Obs, str], Hypothesis]] = [_pool, _dbslow, _timeout, _cpu, _mem, _down, _http500]


def _apply_locality(h: Hypothesis, ev: EvidenceBundle, o: Obs) -> None:
    if h.cause_type not in {"service_timeout", "http_500_spike"}:
        return
    bad_down = sorted(d for d in ev.graph.downstream(h.service) if o.symptom(d) >= 0.4)
    if not bad_down:
        return
    h.related_services = bad_down
    if ev.traces.analysed >= 3:
        own = max(ev.traces.latency_share.get(h.service, 0.0), ev.traces.error_origin_share.get(h.service, 0.0))
        h.locality = round(0.4 + 0.6 * own, 3)
        h.locality_reason = (f"downstream {', '.join(bad_down)} also anomalous; traces attribute "
                             f"{own:.0%} of slow time/error origins to {h.service}")
    else:
        h.locality = 0.6
        h.locality_reason = f"downstream {', '.join(bad_down)} also anomalous; no trace evidence to localise"


def generate(ev: EvidenceBundle) -> list[Hypothesis]:
    o = Obs(ev)
    hyps: list[Hypothesis] = []
    for s in ev.services:
        for b in BUILDERS:
            h = b(ev, o, s)
            if h.gated:
                _apply_locality(h, ev, o)
                hyps.append(h)
    entries = ev.graph.entry_services() & set(ev.services) or set(ev.services)
    if entries:
        entry = max(entries, key=lambda x: (o.rps_up(x), ev.m(x, "rps").delta))
        h = _traffic(ev, o, entry)
        if h.gated:
            hyps.append(h)
    hyps.sort(key=lambda h: (-h.score, h.cause_type, h.service))
    return hyps
