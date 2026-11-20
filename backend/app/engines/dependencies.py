"""Service dependency graph built from *observed* caller->callee edges (trace-derived servicegraph metrics)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from app.telemetry.clients import PrometheusClient
from app.telemetry.promql import EDGE_FAILED, EDGE_P95, EDGE_RPS

EXTERNAL_CLIENTS = {"traffic-generator", "user", "unknown"}


@dataclass
class Edge:
    source: str
    target: str
    rps: float
    error_rate: float
    p95_s: float | None

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target, "rps": round(self.rps, 3),
                "error_rate": round(self.error_rate, 4), "p95_ms": round(self.p95_s * 1000, 1) if self.p95_s is not None else None}


@dataclass
class DependencyGraph:
    edges: list[Edge] = field(default_factory=list)

    @property
    def nodes(self) -> set[str]:
        return {e.source for e in self.edges} | {e.target for e in self.edges}

    def downstream(self, svc: str) -> set[str]:
        return {e.target for e in self.edges if e.source == svc and e.target not in EXTERNAL_CLIENTS}

    def upstream(self, svc: str) -> set[str]:
        return {e.source for e in self.edges if e.target == svc and e.source not in EXTERNAL_CLIENTS}

    def transitive_upstream(self, svc: str) -> set[str]:
        seen, stack = set(), [svc]
        while stack:
            for u in self.upstream(stack.pop()):
                if u not in seen:
                    seen.add(u)
                    stack.append(u)
        return seen

    def entry_services(self) -> set[str]:
        """Services called by external clients (the traffic generator / end users)."""
        return {e.target for e in self.edges if e.source in EXTERNAL_CLIENTS and e.target not in EXTERNAL_CLIENTS}

    def related(self, a: str, b: str) -> bool:
        return a == b or b in self.downstream(a) or a in self.downstream(b) or \
            b in self.transitive_upstream(a) or a in self.transitive_upstream(b)

    def to_dict(self) -> dict:
        return {"nodes": sorted(self.nodes), "edges": [e.to_dict() for e in self.edges]}


async def observed_graph(prom: PrometheusClient, window: str = "5m", at: datetime | None = None) -> DependencyGraph:
    rps, failed, p95 = await asyncio.gather(
        prom.instant(EDGE_RPS.format(w=window), at), prom.instant(EDGE_FAILED.format(w=window), at),
        prom.instant(EDGE_P95.format(w=window), at))

    def _key(m: dict) -> tuple[str, str]:
        return m["metric"].get("client", "unknown"), m["metric"].get("server", "unknown")

    def _val(r: dict) -> float | None:
        try:
            v = float(r["value"][1])
            return None if v != v else v
        except (KeyError, ValueError):
            return None

    f_map = {_key(r): _val(r) or 0.0 for r in failed}
    p_map = {_key(r): _val(r) for r in p95}
    edges = []
    for r in rps:
        k = _key(r)
        v = _val(r) or 0.0
        if v <= 0 or k[1] == "unknown" or k[0] == "unknown":
            continue
        edges.append(Edge(k[0], k[1], v, min(1.0, f_map.get(k, 0.0) / v) if v else 0.0, p_map.get(k)))
    edges.sort(key=lambda e: (e.source, e.target))
    return DependencyGraph(edges)
