"""Fault Injection Lab API: authorised, audited, bounded, local-demo-only proxies to the services' admin endpoints."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import client_ip, limit, require
from app.api.schemas import FaultIn
from app.core.config import DEMO_SERVICES, Settings, get_settings
from app.core.errors import AppError, Forbidden, ServiceUnavailable
from app.core.security import Permission
from app.db.models import FaultInjection, User
from app.db.session import get_session
from app.events.bus import TOPIC_FAULTS, emit
from app.services.incidents import audit

router = APIRouter(prefix="/fault-injection", tags=["fault-injection"])
injector = require(Permission.FAULT_INJECT)

SCENARIOS = [
    {"id": "payment-pool", "name": "Payment DB connection pool exhaustion", "target": "payment-service", "type": "pool_exhaustion"},
    {"id": "inventory-db-slow", "name": "Inventory slow database queries", "target": "inventory-service", "type": "db_delay", "db_delay_ms": 600},
    {"id": "payment-timeout", "name": "Payment slowdown causing timeouts", "target": "payment-service", "type": "latency", "latency_ms": 3500},
    {"id": "inventory-cpu", "name": "Inventory CPU saturation", "target": "inventory-service", "type": "cpu_burn", "cpu_threads": 2},
    {"id": "notification-memory", "name": "Notification memory pressure", "target": "notification-service", "type": "memory_pressure", "memory_mb": 384},
    {"id": "inventory-down", "name": "Inventory service outage", "target": "inventory-service", "type": "unavailable"},
    {"id": "traffic-spike", "name": "Traffic overload (6x)", "target": "traffic-generator", "type": "traffic_spike", "multiplier": 6},
    {"id": "payment-500", "name": "Payment HTTP 500 spike", "target": "payment-service", "type": "error_500", "error_rate": 0.6},
]


def _guard(settings: Settings) -> None:
    if not settings.fault_injection_allowed:
        raise Forbidden("fault injection is disabled (requires ENVIRONMENT=local|demo and FAULT_INJECTION_ENABLED=true)",
                        code="fault_injection_disabled")


async def _call(settings: Settings, method: str, url: str, json: dict | None = None) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.request(method, url, json=json, headers={"x-fault-token": settings.fault_admin_token})
    except httpx.HTTPError as exc:
        raise ServiceUnavailable(f"target unreachable: {type(exc).__name__}") from exc
    if r.status_code >= 400 and r.status_code != 404:
        raise AppError(f"target rejected request: {r.text[:200]}", code="fault_rejected", status_code=502)
    return r.json() if r.content else {}


@router.get("/scenarios")
async def scenarios(user: User = Depends(require(Permission.READ)), settings: Settings = Depends(get_settings)) -> dict:
    return {"enabled": settings.fault_injection_allowed, "environment": settings.environment, "scenarios": SCENARIOS,
            "targets": DEMO_SERVICES + ["traffic-generator"],
            "limits": {"duration_s": [10, 900], "latency_ms": 10000, "db_delay_ms": 5000, "memory_mb": 384, "cpu_threads": 2,
                       "traffic_multiplier": 20}}


@router.get("")
async def active_faults(user: User = Depends(require(Permission.READ)), settings: Settings = Depends(get_settings)) -> dict:
    if not settings.fault_injection_allowed:
        return {"enabled": False, "services": {}, "traffic": None}
    out: dict[str, list] = {}
    for name in DEMO_SERVICES:
        url = settings.service_urls.get(name)
        if not url:
            continue
        try:
            out[name] = (await _call(settings, "GET", f"{url}/admin/faults")).get("faults", [])
        except AppError:
            out[name] = []
    traffic = None
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            traffic = (await c.get(f"{settings.traffic_generator_url}/config")).json()
    except httpx.HTTPError:
        pass
    return {"enabled": True, "services": out, "traffic": traffic}


@router.post("", status_code=201, dependencies=[Depends(limit("fault", 20, 60))])
async def inject(body: FaultIn, request: Request, user: User = Depends(injector), settings: Settings = Depends(get_settings),
                 session: AsyncSession = Depends(get_session)) -> dict:
    _guard(settings)
    params = body.model_dump(exclude_none=True, exclude={"target", "type"})
    if body.type == "traffic_spike":
        if body.target != "traffic-generator":
            raise AppError("traffic_spike targets the traffic-generator", code="invalid_target", status_code=422)
        result = await _call(settings, "PUT", f"{settings.traffic_generator_url}/config",
                             {"profile": "spike", "spike_multiplier": body.multiplier or 6, "duration_s": body.duration_s})
    else:
        if body.target not in DEMO_SERVICES or not settings.service_urls.get(body.target):
            raise AppError("unknown target service", code="invalid_target", status_code=422)
        payload = {"type": body.type, **{k: v for k, v in params.items() if k != "multiplier"}}
        result = await _call(settings, "POST", f"{settings.service_urls[body.target]}/admin/faults", payload)
    now = datetime.now(UTC)
    rec = FaultInjection(target=body.target, fault_type=body.type, params=params, started_at=now,
                         expires_at=now + timedelta(seconds=body.duration_s), actor_id=user.id)
    session.add(rec)
    await audit(session, actor=user, action="fault.injected", target_type="service", target_id=body.target,
                client_ip=client_ip(request), details={"type": body.type, **params})
    await session.commit()
    await emit(TOPIC_FAULTS, "fault.injected", {"target": body.target, "type": body.type, "params": params,
                                                "expires_at": rec.expires_at.isoformat(), "actor": user.username},
               source="api", key=body.target)
    return {"id": rec.id, "target": body.target, "type": body.type, "expires_at": rec.expires_at, "result": result}


@router.post("/reset")
async def reset(request: Request, user: User = Depends(injector), settings: Settings = Depends(get_settings),
                session: AsyncSession = Depends(get_session)) -> dict:
    """Restore normal behaviour everywhere: clear all service faults and reset the traffic profile."""
    _guard(settings)
    removed: dict[str, int] = {}
    for name in DEMO_SERVICES:
        url = settings.service_urls.get(name)
        if url:
            try:
                removed[name] = (await _call(settings, "DELETE", f"{url}/admin/faults")).get("removed", 0)
            except AppError:
                removed[name] = -1
    try:
        await _call(settings, "POST", f"{settings.traffic_generator_url}/config/reset")
        removed["traffic-generator"] = 1
    except AppError:
        removed["traffic-generator"] = -1
    now = datetime.now(UTC)
    await session.execute(update(FaultInjection).where(FaultInjection.ended_at.is_(None)).values(ended_at=now))
    await audit(session, actor=user, action="fault.reset", client_ip=client_ip(request), details=removed)
    await session.commit()
    await emit(TOPIC_FAULTS, "fault.cleared", {"removed": removed, "actor": user.username}, source="api")
    return {"removed": removed}


@router.get("/history")
async def history(user: User = Depends(require(Permission.READ)), session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(FaultInjection).order_by(FaultInjection.started_at.desc()).limit(50))).scalars().all()
    return {"items": [{"id": r.id, "target": r.target, "type": r.fault_type, "params": r.params, "started_at": r.started_at,
                       "expires_at": r.expires_at, "ended_at": r.ended_at} for r in rows]}
