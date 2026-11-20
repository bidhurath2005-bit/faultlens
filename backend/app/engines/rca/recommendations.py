"""Remediation knowledge base. Recommendations are advisory only: FaultLens never executes them."""
from __future__ import annotations

from .hypotheses import Hypothesis

_KB: dict[str, list[tuple[str, str, str]]] = {
    "db_pool_exhaustion": [
        ("Look for code paths that check out DB connections without returning them (leaks, long transactions); "
         "restart or drain {svc} to reclaim held connections.", "Pool is saturated while query execution time is normal, "
         "which points at connections being held rather than slow SQL.", "medium"),
        ("Temporarily raise {svc}'s pool size / max_overflow within database max_connections headroom.",
         "Buys capacity while the leak is fixed; does not fix the cause.", "low"),
        ("Add pool-checkout timeouts with fast-fail and a circuit breaker in callers ({callers}).",
         "Prevents callers from stacking up behind an exhausted pool.", "low"),
    ],
    "db_query_latency": [
        ("Inspect pg_stat_activity / pg_stat_statements for slow or blocked statements from {svc}; check locks and I/O.",
         "Query P95 rose sharply relative to its baseline.", "low"),
        ("Set statement_timeout for {svc}'s queries and review recently changed queries or missing indexes.",
         "Bounds the blast radius of slow queries.", "low"),
        ("Reduce concurrent load on the database (rate limit batch jobs) until latency recovers.", "Lower contention.", "medium"),
    ],
    "service_timeout": [
        ("Profile {svc}: the slow time is spent inside the service itself (not in DB or downstream calls).",
         "Trace self-time attribution localises the latency to {svc}.", "low"),
        ("Review recent deploys/config changes for {svc} and roll back if correlated with the onset.", "Common trigger.", "medium"),
        ("Tune caller timeouts and add retries with backoff/jitter in {callers} to avoid timeout cascades.",
         "Callers are timing out on {svc}.", "low"),
    ],
    "high_cpu": [
        ("Capture a CPU profile of {svc} (py-spy) to identify the hot code path.", "CPU rose without a matching rise in load.", "low"),
        ("Scale {svc} horizontally or raise its CPU limit temporarily.", "Restores headroom while the hot path is fixed.", "medium"),
    ],
    "memory_pressure": [
        ("Take a heap snapshot of {svc} (tracemalloc) and look for unbounded caches/buffers.",
         "RSS grew well beyond baseline and towards the container limit.", "low"),
        ("Restart {svc} in a rolling fashion before it is OOM-killed; raise the memory limit if growth is legitimate.",
         "Prevents an abrupt OOM kill.", "medium"),
    ],
    "downstream_failure": [
        ("Check {svc}'s process/container health and recent restarts; restore the instance or fail over.",
         "Health probes and callers both report {svc} unavailable.", "medium"),
        ("Enable graceful degradation in {callers} (circuit breaker, cached/fallback responses).",
         "Limits user impact during the outage.", "low"),
    ],
    "http_500_spike": [
        ("Inspect the exception logs and error spans of {svc} for the failing code path; roll back the latest change if correlated.",
         "Errors originate in {svc} while it remains reachable.", "medium"),
        ("Add alerting on {svc}'s 5xx ratio and protect callers ({callers}) with retries for idempotent calls.", "", "low"),
    ],
    "traffic_overload": [
        ("Apply rate limiting / load shedding at the entry point ({svc}).", "Request rate is far above baseline across services.", "low"),
        ("Scale out the saturated services and verify autoscaling thresholds.", "Load-driven saturation.", "medium"),
        ("Identify the traffic source (client, bot, retry storm) before scaling further.", "Avoid amplifying retry storms.", "low"),
    ],
}


def recommendations_for(h: Hypothesis, callers: set[str]) -> list[dict]:
    caller_s = ", ".join(sorted(callers)) or "callers"
    out = []
    for i, (action, why, risk) in enumerate(_KB.get(h.cause_type, []), start=1):
        out.append({"priority": i, "action": action.format(svc=h.service, callers=caller_s),
                    "rationale": why.format(svc=h.service, callers=caller_s), "risk": risk})
    return out
