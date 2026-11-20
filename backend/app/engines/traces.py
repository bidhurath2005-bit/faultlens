"""Trace analysis: span trees, exclusive (self) time attribution and error-origin detection."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.telemetry.clients import Span


def span_category(s: Span) -> str:
    first = s.name.split(" ", 1)[0].upper()
    is_db = "db.system" in s.attributes or first in {"SELECT", "INSERT", "UPDATE", "DELETE", "BEGIN", "COMMIT", "CONNECT"}
    # Pool checkout: our own db.pool.acquire span, and the instrumentation's DB "connect" span beneath it
    # (which is where a QueuePool checkout actually waits / times out).
    if s.name == "db.pool.acquire" or (is_db and first == "CONNECT"):
        return "db_pool"
    if is_db:
        return "db"
    if s.kind == "client":
        return "client"
    return "app"


def peer_of(s: Span, known_services: set[str]) -> str | None:
    for key in ("server.address", "net.peer.name", "http.host"):
        host = s.attributes.get(key)
        if isinstance(host, str):
            host = host.split(":")[0]
            if host in known_services:
                return host
    url = s.attributes.get("url.full") or s.attributes.get("http.url")
    if isinstance(url, str):
        for svc in known_services:
            if f"//{svc}" in url:
                return svc
    return None


def _union_ns(intervals: list[tuple[int, int]]) -> int:
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


@dataclass
class TraceAnalysis:
    trace_id: str
    duration_ms: float
    root_service: str
    services: list[str]
    has_error: bool
    self_time_ms: dict[str, dict[str, float]]          # service -> category -> ms
    error_origins: list[dict]                          # [{service, span, reason}]
    peer_timeouts: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        return {"trace_id": self.trace_id, "duration_ms": round(self.duration_ms, 1), "root_service": self.root_service,
                "services": self.services, "has_error": self.has_error,
                "self_time_ms": {s: {c: round(v, 1) for c, v in cats.items()} for s, cats in self.self_time_ms.items()},
                "error_origins": self.error_origins, "peer_timeouts": dict(self.peer_timeouts)}


def analyse_trace(spans: list[Span], known_services: set[str]) -> TraceAnalysis | None:
    if not spans:
        return None
    by_id = {s.span_id: s for s in spans}
    children: dict[str, list[Span]] = defaultdict(list)
    for s in spans:
        if s.parent_span_id and s.parent_span_id in by_id:
            children[s.parent_span_id].append(s)
    roots = [s for s in spans if not s.parent_span_id or s.parent_span_id not in by_id]
    root = min(roots, key=lambda s: s.start_ns)

    self_time: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for s in spans:
        kids = [(max(c.start_ns, s.start_ns), min(c.end_ns, s.end_ns)) for c in children[s.span_id]]
        kids = [(a, b) for a, b in kids if b > a]
        excl = max(0, (s.end_ns - s.start_ns) - _union_ns(kids)) / 1e6
        cat = span_category(s)
        if cat == "client":
            # Time a client waits with no server-side child is attributed to the peer (unreachable/queued).
            has_server_child = any(c.kind == "server" for c in children[s.span_id])
            peer = peer_of(s, known_services)
            if not has_server_child and peer:
                self_time[peer]["no_response"] += excl
                continue
            self_time[s.service]["network"] += excl
        else:
            self_time[s.service][cat] += excl

    def _error_desc(sp: Span) -> bool:
        stack = list(children[sp.span_id])
        while stack:
            c = stack.pop()
            if c.status == "error":
                return True
            stack.extend(children[c.span_id])
        return False

    origins, timeouts = [], Counter()
    for s in spans:
        if s.status != "error" or _error_desc(s):
            continue
        cat = span_category(s)
        if cat in ("db", "db_pool"):
            # A failing database call is an error originating in the calling service itself.
            origins.append({"service": s.service, "span": s.name, "reason": cat, "message": s.status_message})
            continue
        if s.kind == "client":
            server_kids = [c for c in children[s.span_id] if c.kind == "server"]
            peer = peer_of(s, known_services)
            msg = (s.status_message or "").lower()
            if "timeout" in msg or "timed out" in msg:
                if peer:
                    timeouts[peer] += 1
            if server_kids:
                continue  # peer answered without error (e.g. 4xx business rule): not a fault origin
            if peer:
                origins.append({"service": peer, "span": s.name, "reason": "no_response", "observed_by": s.service})
            continue
        if s.service == "traffic-generator":
            continue
        origins.append({"service": s.service, "span": s.name, "reason": cat, "message": s.status_message})

    return TraceAnalysis(
        trace_id=root.trace_id, duration_ms=(max(s.end_ns for s in spans) - root.start_ns) / 1e6,
        root_service=root.service, services=sorted({s.service for s in spans}),
        has_error=any(s.status == "error" for s in spans),
        self_time_ms={k: dict(v) for k, v in self_time.items()}, error_origins=origins, peer_timeouts=timeouts,
    )


@dataclass
class TraceSummary:
    analysed: int = 0
    slow_analysed: int = 0
    error_analysed: int = 0
    latency_share: dict[str, float] = field(default_factory=dict)      # service -> share of self time in slow traces
    latency_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)
    error_origin_share: dict[str, float] = field(default_factory=dict)  # service -> share of error origins
    origin_reasons: dict[str, dict[str, int]] = field(default_factory=dict)
    peer_timeouts: dict[str, int] = field(default_factory=dict)
    examples: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"analysed": self.analysed, "slow_analysed": self.slow_analysed, "error_analysed": self.error_analysed,
                "latency_share": {k: round(v, 3) for k, v in self.latency_share.items()},
                "latency_breakdown": {s: {c: round(v, 3) for c, v in d.items()} for s, d in self.latency_breakdown.items()},
                "error_origin_share": {k: round(v, 3) for k, v in self.error_origin_share.items()},
                "origin_reasons": self.origin_reasons, "peer_timeouts": self.peer_timeouts, "examples": self.examples}


def summarise_traces(slow: Iterable[TraceAnalysis], errors: Iterable[TraceAnalysis]) -> TraceSummary:
    slow, errors = list(slow), list(errors)
    ts = TraceSummary(analysed=len({t.trace_id for t in slow + errors}), slow_analysed=len(slow), error_analysed=len(errors))
    tot: Counter = Counter()
    breakdown: dict[str, Counter] = defaultdict(Counter)
    for t in slow:
        for svc, cats in t.self_time_ms.items():
            if svc == "traffic-generator":
                continue
            for c, v in cats.items():
                tot[svc] += v
                breakdown[svc][c] += v
    grand = sum(tot.values())
    if grand > 0:
        ts.latency_share = {s: v / grand for s, v in tot.items()}
        ts.latency_breakdown = {s: {c: v / grand for c, v in cats.items()} for s, cats in breakdown.items()}
    origins: Counter = Counter()
    reasons: dict[str, Counter] = defaultdict(Counter)
    timeouts: Counter = Counter()
    for t in {t.trace_id: t for t in errors + slow}.values():
        for o in t.error_origins:
            origins[o["service"]] += 1
            reasons[o["service"]][o["reason"]] += 1
        timeouts.update(t.peer_timeouts)
    n = sum(origins.values())
    if n:
        ts.error_origin_share = {s: c / n for s, c in origins.items()}
    ts.origin_reasons = {s: dict(c) for s, c in reasons.items()}
    ts.peer_timeouts = dict(timeouts)
    ts.examples = [t.to_dict() for t in sorted(errors + slow, key=lambda t: -t.duration_ms)[:6]]
    return ts
