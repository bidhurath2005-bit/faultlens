"""Read APIs over live telemetry: services, metrics, traces, logs, dependency graph."""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require
from app.core.config import Settings, get_settings
from app.core.errors import AppError, NotFound
from app.core.redis import cached, get_redis
from app.core.security import Permission
from app.db.models import Anomaly, Incident, IncidentStatus, Service, User
from app.db.session import get_session
from app.engines.dependencies import observed_graph
from app.engines.traces import analyse_trace
from app.telemetry.clients import get_telemetry
from app.telemetry.promql import STATUS_BREAKDOWN, feature_query, validate_service_name

router = APIRouter(tags=["observability"])
reader = require(Permission.READ)

OVERVIEW_FEATURES = ["rps", "error_rate", "latency_p50", "latency_p95", "latency_p99", "latency_avg", "cpu",
                     "memory_rss", "memory_util", "db_pool_util", "db_query_p95", "availability"]
RANGE_STEPS = {15: 15, 30: 15, 60: 30, 180: 60, 360: 120, 1440: 300}


def _svc(name: str) -> str:
    try:
        return validate_service_name(name)
    except ValueError:
        raise AppError("invalid service name", code="invalid_service", status_code=422) from None


def health_status(f: dict[str, float | None]) -> str:
    """Simple, documented health classification used across the UI."""
    avail = f.get("availability")
    err = f.get("error_rate") or 0.0
    p95 = f.get("latency_p95") or 0.0
    if avail is not None and avail < 0.5:
        return "down"
    if err >= 0.10 or p95 >= 2.0 or (avail is not None and avail < 0.9):
        return "critical"
    if err >= 0.02 or p95 >= 0.5 or (f.get("cpu") or 0) >= 0.85 or (f.get("memory_util") or 0) >= 0.85:
        return "degraded"
    if f.get("rps") is None:
        return "unknown"
    return "healthy"


async def _service_snapshot() -> dict[str, dict[str, float | None]]:
    async def produce() -> dict:
        prom = get_telemetry().prom
        vals = await asyncio.gather(*(prom.instant_by(feature_query(f, "1m")) for f in OVERVIEW_FEATURES))
        out: dict[str, dict] = {}
        for f, v in zip(OVERVIEW_FEATURES, vals, strict=False):
            for svc, x in v.items():
                out.setdefault(svc, {})[f] = x
        return out
    return await cached("snapshot:services", 5, produce)


@router.get("/health/live", include_in_schema=False)
async def live() -> dict:
    return {"status": "ok"}


@router.get("/health")
async def health(settings: Settings = Depends(get_settings), session: AsyncSession = Depends(get_session)) -> dict:
    """Dependency health for the platform itself (unauthenticated; exposes no data)."""
    tel = get_telemetry()
    checks: dict[str, bool] = {}
    try:
        await session.execute(select(1))
        checks["postgres"] = True
    except Exception:  # noqa: BLE001
        checks["postgres"] = False
    try:
        checks["redis"] = bool(await get_redis().ping())
    except Exception:  # noqa: BLE001
        checks["redis"] = False
    checks["prometheus"], checks["loki"], checks["tempo"] = await asyncio.gather(tel.prom.ready(), tel.loki.ready(), tel.tempo.ready())
    ok = all(checks.values())
    return {"status": "ok" if ok else "degraded", "version": settings.version, "environment": settings.environment,
            "checks": checks}


@router.get("/services")
async def list_services(user: User = Depends(reader), session: AsyncSession = Depends(get_session)) -> dict:
    snap = await _service_snapshot()
    rows = (await session.execute(select(Service).order_by(Service.name))).scalars().all()
    graph = await cached("graph:5m", 10, lambda: _graph_dict("5m"))
    since = datetime.now(UTC) - timedelta(hours=1)
    recent = dict((await session.execute(select(Anomaly.service_id, func.count(Anomaly.id))
                                         .where(Anomaly.detected_at >= since).group_by(Anomaly.service_id))).all())
    items = []
    for s in rows:
        f = snap.get(s.name, {})
        items.append({
            "name": s.name, "display_name": s.display_name, "description": s.description, "owner_team": s.owner_team,
            "tier": s.tier, "health": health_status(f), "metrics": f,
            "dependencies": sorted({e["target"] for e in graph["edges"] if e["source"] == s.name}),
            "dependents": sorted({e["source"] for e in graph["edges"] if e["target"] == s.name}),
            "anomalies_1h": recent.get(s.id, 0),
        })
    return {"items": items}


@router.get("/services/{service_name}")
async def get_service(service_name: str, minutes: int = Query(30, ge=5, le=1440), user: User = Depends(reader),
                      session: AsyncSession = Depends(get_session)) -> dict:
    name = _svc(service_name)
    svc = (await session.execute(select(Service).where(Service.name == name))).scalar_one_or_none()
    if svc is None:
        raise NotFound("service not found")
    snap = (await _service_snapshot()).get(name, {})
    series = await _timeseries(["rps", "error_rate", "latency_p50", "latency_p95", "latency_p99", "cpu", "memory_rss",
                                "db_pool_util", "db_query_p95"], minutes, name)
    prom = get_telemetry().prom
    status = await prom.instant(STATUS_BREAKDOWN.format(w="5m", svc=f'service_name="{name}"'))
    anomalies = (await session.execute(select(Anomaly).where(Anomaly.service_id == svc.id)
                                       .order_by(Anomaly.detected_at.desc()).limit(10))).scalars().all()
    return {"name": name, "display_name": svc.display_name, "description": svc.description, "owner_team": svc.owner_team,
            "health": health_status(snap), "metrics": snap, "series": series,
            "status_breakdown": {r["metric"].get("status_class", "?"): float(r["value"][1]) for r in status},
            "recent_anomalies": [_anomaly_dict(a) for a in anomalies]}


def _anomaly_dict(a: Anomaly) -> dict:
    return {"id": a.id, "service": a.service.name, "incident_id": a.incident_id, "detected_at": a.detected_at,
            "metric": a.metric, "method": a.method, "score": a.score, "severity": a.severity.value,
            "observed_value": a.observed_value, "baseline_value": a.baseline_value, "details": a.details}


async def _timeseries(features: list[str], minutes: int, service: str | None = None) -> dict:
    step = next((v for k, v in sorted(RANGE_STEPS.items()) if minutes <= k), 300)
    end = datetime.now(UTC)
    start = end - timedelta(minutes=minutes)
    prom = get_telemetry().prom
    res = await asyncio.gather(*(prom.range_by(feature_query(f, "1m", service), start, end, step) for f in features))
    return {f: {svc: [[round(t), v] for t, v in pts] for svc, pts in r.items()} for f, r in zip(features, res, strict=False)}


@router.get("/metrics/overview")
async def metrics_overview(minutes: int = Query(30, ge=5, le=1440), user: User = Depends(reader),
                           session: AsyncSession = Depends(get_session)) -> dict:
    snap = await _service_snapshot()

    async def produce() -> dict:
        prom = get_telemetry().prom
        q = {
            "rps": "sum(rate(faultlens_http_server_requests_total[1m]))",
            "error_rate": '(sum(rate(faultlens_http_server_requests_total{status_class="5xx"}[1m])) or vector(0)) / clamp_min(sum(rate(faultlens_http_server_requests_total[1m])),1e-9)',
            "latency_p50": "histogram_quantile(0.5, sum by (le) (rate(faultlens_http_server_duration_seconds_bucket[1m])))",
            "latency_p95": "histogram_quantile(0.95, sum by (le) (rate(faultlens_http_server_duration_seconds_bucket[1m])))",
            "latency_p99": "histogram_quantile(0.99, sum by (le) (rate(faultlens_http_server_duration_seconds_bucket[1m])))",
            "latency_avg": "sum(rate(faultlens_http_server_duration_seconds_sum[1m])) / clamp_min(sum(rate(faultlens_http_server_duration_seconds_count[1m])),1e-9)",
        }
        step = next((v for k, v in sorted(RANGE_STEPS.items()) if minutes <= k), 300)
        end = datetime.now(UTC)
        start = end - timedelta(minutes=minutes)
        cur, rng = await asyncio.gather(
            asyncio.gather(*(prom.instant(x) for x in q.values())),
            asyncio.gather(*(prom.range(x, start, end, step) for x in q.values())))
        current = {k: (float(r[0]["value"][1]) if r and r[0]["value"][1] not in ("NaN", "+Inf") else None)
                   for k, r in zip(q, cur, strict=False)}
        series = {k: ([[round(float(t)), float(v)] for t, v in r[0]["values"] if v not in ("NaN", "+Inf")] if r else [])
                  for k, r in zip(q, rng, strict=False)}
        return {"current": current, "series": series}

    agg = await cached(f"overview:{minutes}", 5, produce)
    healths = {s: health_status(f) for s, f in snap.items() if s in _demo_names()}
    active = (await session.execute(select(func.count(Incident.id)).where(Incident.status != IncidentStatus.RESOLVED))).scalar_one()
    by_sev = dict((await session.execute(select(Incident.severity, func.count(Incident.id))
                                         .where(Incident.status != IncidentStatus.RESOLVED).group_by(Incident.severity))).all())
    recent = (await session.execute(select(Anomaly).order_by(Anomaly.detected_at.desc()).limit(8))).scalars().all()
    order = ["down", "critical", "degraded", "unknown", "healthy"]
    overall = min(healths.values(), key=order.index) if healths else "unknown"
    return {
        "overall_health": overall,
        "services_total": len(_demo_names()),
        "services_reporting": sum(1 for s in healths if snap.get(s, {}).get("rps") is not None),
        "service_health": healths,
        "active_incidents": active,
        "active_incidents_by_severity": {k.value: v for k, v in by_sev.items()},
        **agg,
        "recent_anomalies": [_anomaly_dict(a) for a in recent],
        "generated_at": datetime.now(UTC),
    }


def _demo_names() -> list[str]:
    from app.core.config import DEMO_SERVICES
    return DEMO_SERVICES


@router.get("/metrics/services")
async def metrics_services(minutes: int = Query(30, ge=5, le=1440),
                           features: str = Query("rps,error_rate,latency_p95", max_length=200),
                           user: User = Depends(reader)) -> dict:
    fs = [f.strip() for f in features.split(",") if f.strip()]
    bad = [f for f in fs if f not in OVERVIEW_FEATURES]
    if bad:
        raise AppError(f"unknown features: {bad}", code="invalid_feature", status_code=422)
    return {"current": await _service_snapshot(),
            "series": await cached(f"ts:{minutes}:{','.join(fs)}", 10, lambda: _timeseries(fs, minutes))}


async def _graph_dict(window: str) -> dict:
    return (await observed_graph(get_telemetry().prom, window)).to_dict()


@router.get("/dependencies")
async def dependency_graph(window: Literal["1m", "5m", "15m", "1h"] = "5m", user: User = Depends(reader)) -> dict:
    graph = await cached(f"graph:{window}", 10, lambda: _graph_dict(window))
    snap = await _service_snapshot()
    nodes = []
    from app.core.config import DEMO_SERVICES
    for n in graph["nodes"]:
        f = snap.get(n, {})
        # The servicegraph connector also reports virtual nodes: "user" = an uninstrumented caller, and database
        # peers (e.g. "shop", from db.name on client DB spans). They are real observations, labelled as such.
        if n in DEMO_SERVICES:
            kind = "service"
        elif n in ("traffic-generator", "user"):
            kind = "client"
        else:
            kind = "database"
        nodes.append({"id": n, "kind": kind, "health": health_status(f) if kind == "service" else "external", "metrics": f})
    return {"window": window, "nodes": nodes, "edges": graph["edges"], "source": "traces_service_graph (OTel Collector servicegraph connector)",
            "generated_at": datetime.now(UTC)}


# ------------------------------------------------------------------ traces
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


@router.get("/traces")
async def search_traces(service: str | None = None, status: Literal["any", "error", "ok"] = "any",
                        min_duration_ms: int = Query(0, ge=0, le=600_000), minutes: int = Query(15, ge=1, le=1440),
                        limit: int = Query(20, ge=1, le=100), user: User = Depends(reader)) -> dict:
    conds = []
    if service:
        conds.append(f'resource.service.name = "{_svc(service)}"')
    if status == "error":
        conds.append("status = error")
    elif status == "ok":
        conds.append("status != error")
    if min_duration_ms:
        conds.append(f"duration > {min_duration_ms}ms")
    q = "{ " + " && ".join(conds) + " }" if conds else "{}"
    end = datetime.now(UTC)
    items = await get_telemetry().tempo.search(q, end - timedelta(minutes=minutes), end, limit)
    return {"query": q, "items": items}


@router.get("/traces/{trace_id}")
async def get_trace(trace_id: str, user: User = Depends(reader)) -> dict:
    if not _TRACE_ID.match(trace_id):
        raise AppError("trace id must be 32 lowercase hex chars", code="invalid_trace_id", status_code=422)
    spans = await get_telemetry().tempo.get_trace(trace_id)
    if not spans:
        raise NotFound("trace not found (it may not be ingested yet or may have expired)")
    from app.core.config import DEMO_SERVICES
    analysis = analyse_trace(spans, set(DEMO_SERVICES))
    by_service: dict[str, dict] = {}
    for s in spans:
        b = by_service.setdefault(s.service, {"spans": 0, "errors": 0})
        b["spans"] += 1
        b["errors"] += s.status == "error"
    return {"trace_id": trace_id, "spans": [s.as_dict() for s in spans], "service_breakdown": by_service,
            "analysis": analysis.to_dict() if analysis else None}


# ------------------------------------------------------------------ logs
_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _escape_logql(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


@router.get("/logs")
async def search_logs(service: str | None = None, level: str | None = Query(None, max_length=40),
                      q: str | None = Query(None, max_length=200), trace_id: str | None = None,
                      minutes: int = Query(15, ge=1, le=1440), start: datetime | None = None, end: datetime | None = None,
                      limit: int = Query(100, ge=1, le=1000), user: User = Depends(reader)) -> dict:
    selector = f'{{service_name="{_svc(service)}"}}' if service else '{service_namespace="faultlens-demo"}'
    pipeline = []
    if level:
        lv = [x.strip().upper() for x in level.split(",") if x.strip()]
        if not set(lv) <= _LEVELS:
            raise AppError("invalid level", code="invalid_level", status_code=422)
        alts = sorted(set(lv) | ({"WARN"} if "WARNING" in lv else set()))
        pipeline.append(f'severity_text=~"{"|".join(alts)}"')
    if trace_id:
        if not _TRACE_ID.match(trace_id):
            raise AppError("trace id must be 32 lowercase hex chars", code="invalid_trace_id", status_code=422)
        pipeline.append(f'trace_id="{trace_id}"')
    logql = selector + "".join(f" | {p}" for p in pipeline)
    if q:
        logql += f' |= "{_escape_logql(q)}"'
    end = end or datetime.now(UTC)
    start = start or end - timedelta(minutes=minutes)
    if start >= end or end - start > timedelta(days=1):
        raise AppError("time range must be positive and at most 24h", code="invalid_range", status_code=422)
    recs = await get_telemetry().loki.query_range(logql, start, end, limit=limit)
    return {"query": logql, "items": [r.as_dict() for r in recs], "start": start, "end": end}
