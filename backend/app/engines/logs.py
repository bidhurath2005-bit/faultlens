"""Log analysis: template mining + signature classification + baseline-relative excess counts."""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.telemetry.clients import LogRecord

_MASKS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b[0-9a-f]{16,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)?\b"), "<n>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
]

# Signatures map raw log evidence onto failure categories used by the RCA hypotheses.
SIGNATURES: dict[str, re.Pattern] = {
    "pool_exhausted": re.compile(r"pool exhausted|QueuePool limit|connection pool", re.I),
    "slow_query": re.compile(r"slow query", re.I),
    "timeout": re.compile(r"timed out|timeout", re.I),
    "unreachable": re.compile(r"unreachable|connection refused|ConnectError", re.I),
    "downstream_failed": re.compile(r"downstream call failed", re.I),
    "unhandled_exception": re.compile(r"unhandled exception|internal server error|Traceback", re.I),
    "cpu_starvation": re.compile(r"event loop lag|CPU starved", re.I),
    "memory_high": re.compile(r"memory usage high|MemoryError|out of memory", re.I),
}


def template_of(message: str) -> str:
    t = message
    for rx, rep in _MASKS:
        t = rx.sub(rep, t)
    return t[:200]


def classify(rec: LogRecord) -> list[str]:
    text = rec.message
    cats = [c for c, rx in SIGNATURES.items() if rx.search(text)]
    status = rec.attributes.get("status_code")
    if rec.message == "request completed" and status and status.startswith("5"):
        cats.append("request_5xx")
    return cats


@dataclass
class ServiceLogSummary:
    service: str
    levels: Counter = field(default_factory=Counter)
    categories: Counter = field(default_factory=Counter)
    excess: dict[str, float] = field(default_factory=dict)
    peer_categories: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    templates: Counter = field(default_factory=Counter)
    samples: dict[str, LogRecord] = field(default_factory=dict)

    def to_dict(self, max_templates: int = 8) -> dict:
        return {
            "service": self.service,
            "levels": dict(self.levels),
            "categories": dict(self.categories),
            "excess": {k: round(v, 1) for k, v in self.excess.items() if v > 0},
            "peer_categories": {p: dict(c) for p, c in self.peer_categories.items()},
            "top_templates": [
                {"template": t, "count": n, "sample": self.samples[t].as_dict()}
                for t, n in self.templates.most_common(max_templates)
            ],
        }


def summarise(records: Iterable[LogRecord]) -> dict[str, ServiceLogSummary]:
    out: dict[str, ServiceLogSummary] = {}
    for rec in records:
        s = out.setdefault(rec.service, ServiceLogSummary(rec.service))
        s.levels[rec.level] += 1
        cats = classify(rec)
        for c in cats:
            s.categories[c] += 1
            if peer := rec.attributes.get("peer_service"):
                s.peer_categories[peer][c] += 1
        if rec.level in ("ERROR", "WARNING", "CRITICAL"):
            t = template_of(rec.message)
            s.templates[t] += 1
            s.samples.setdefault(t, rec)
    return out


def with_excess(window: dict[str, ServiceLogSummary], baseline: dict[str, ServiceLogSummary],
                window_minutes: float, baseline_minutes: float) -> dict[str, ServiceLogSummary]:
    """Excess = window count - expected count from the baseline rate. Recurring background noise
    (e.g. an occasional slow query) therefore does not count as incident evidence."""
    scale = window_minutes / max(baseline_minutes, 1e-6)
    for svc, s in window.items():
        b = baseline.get(svc)
        for cat, n in s.categories.items():
            expected = (b.categories.get(cat, 0) if b else 0) * scale
            s.excess[cat] = max(0.0, n - expected)
    return window


def peer_mentions(summaries: dict[str, ServiceLogSummary], peer: str, categories: set[str]) -> int:
    """How many log lines in *other* services blame `peer` with one of `categories`."""
    return sum(n for s in summaries.values() for c, n in s.peer_categories.get(peer, {}).items() if c in categories)
