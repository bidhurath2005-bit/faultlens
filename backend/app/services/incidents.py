"""Incident lifecycle domain logic: state machine, timeline, audit and correlation."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import Conflict, NotFound
from app.db.models import (
    SEVERITY_RANK,
    Anomaly,
    AuditEvent,
    Incident,
    IncidentEvent,
    IncidentService,
    IncidentStatus,
    Service,
    Severity,
    User,
)

TRANSITIONS: dict[IncidentStatus, set[IncidentStatus]] = {
    IncidentStatus.OPEN: {IncidentStatus.ACKNOWLEDGED},
    IncidentStatus.ACKNOWLEDGED: {IncidentStatus.INVESTIGATING, IncidentStatus.RESOLVED},
    IncidentStatus.INVESTIGATING: {IncidentStatus.RESOLVED},
    IncidentStatus.RESOLVED: set(),
}

CORRELATION_WINDOW = timedelta(minutes=10)


def can_transition(src: IncidentStatus, dst: IncidentStatus) -> bool:
    return dst in TRANSITIONS[src]


async def audit(session: AsyncSession, *, actor: User | None, action: str, target_type: str | None = None,
                target_id: Any = None, outcome: str = "success", client_ip: str | None = None,
                details: dict | None = None) -> None:
    session.add(AuditEvent(actor_id=actor.id if actor else None, actor_username=actor.username if actor else "system",
                           action=action, target_type=target_type, target_id=str(target_id) if target_id is not None else None,
                           outcome=outcome, client_ip=client_ip, details=details or {}))


def timeline(session: AsyncSession, incident: Incident, kind: str, message: str, actor: User | None = None,
             data: dict | None = None) -> None:
    session.add(IncidentEvent(incident_id=incident.id, kind=kind, message=message,
                              actor_id=actor.id if actor else None, data=data or {}))


async def get_incident(session: AsyncSession, incident_id: int, *, lock: bool = False) -> Incident:
    stmt = select(Incident).where(Incident.id == incident_id)
    if lock:
        stmt = stmt.with_for_update(of=Incident)
    inc = (await session.execute(stmt)).unique().scalar_one_or_none()
    if inc is None:
        raise NotFound(f"incident {incident_id} not found")
    return inc


async def transition(session: AsyncSession, incident_id: int, dst: IncidentStatus, actor: User,
                     note: str | None = None) -> Incident:
    inc = await get_incident(session, incident_id, lock=True)  # row lock => no concurrent double transitions
    if not can_transition(inc.status, dst):
        raise Conflict(f"cannot move incident from {inc.status.value} to {dst.value}", code="invalid_transition")
    now = datetime.now(UTC)
    prev = inc.status
    inc.status = dst
    inc.last_activity_at = now
    if dst == IncidentStatus.ACKNOWLEDGED:
        inc.acknowledged_at, inc.acknowledged_by_id = now, actor.id
    elif dst == IncidentStatus.RESOLVED:
        inc.resolved_at, inc.resolved_by_id, inc.resolution_note = now, actor.id, note
    timeline(session, inc, f"status.{dst.value}", f"{actor.username} moved incident from {prev.value} to {dst.value}"
             + (f": {note}" if note else ""), actor, {"from": prev.value, "to": dst.value})
    await audit(session, actor=actor, action=f"incident.{dst.value}", target_type="incident", target_id=inc.id,
                details={"from": prev.value, "note": note})
    await session.flush()
    return inc


async def services_by_name(session: AsyncSession) -> dict[str, Service]:
    return {s.name: s for s in (await session.execute(select(Service))).scalars()}


async def correlate_anomaly(session: AsyncSession, anomaly: Anomaly, related: callable) -> tuple[Incident, bool]:
    """Attach an anomaly to an active, related incident, or open a new one.

    Correlation rule: an unresolved incident with activity in the last 10 minutes whose affected
    services include the anomaly's service or a service related to it in the observed dependency
    graph (`related(a, b)`). Returns (incident, created)."""
    now = datetime.now(UTC)
    svc = anomaly.service
    candidates = (await session.execute(
        select(Incident).where(Incident.status != IncidentStatus.RESOLVED,
                               Incident.last_activity_at >= now - CORRELATION_WINDOW)
        .order_by(Incident.last_activity_at.desc()).with_for_update(of=Incident)
    )).unique().scalars().all()
    for inc in candidates:
        names = {s.name for s in inc.services}
        if svc.name in names or any(related(svc.name, n) for n in names):
            if svc.name not in names:
                session.add(IncidentService(incident_id=inc.id, service_id=svc.id))
            anomaly.incident_id = inc.id
            inc.last_activity_at = now
            if SEVERITY_RANK[anomaly.severity] > SEVERITY_RANK[inc.severity]:
                timeline(session, inc, "severity.raised", f"severity raised {inc.severity.value} -> {anomaly.severity.value}")
                inc.severity = anomaly.severity
            timeline(session, inc, "anomaly.attached",
                     f"{anomaly.method} detected {anomaly.metric} anomaly on {svc.name}",
                     data={"anomaly_id": anomaly.id, "score": anomaly.score})
            await session.flush()
            await session.refresh(inc, attribute_names=["services"])
            return inc, False

    inc = Incident(title=f"{anomaly.metric.replace('_', ' ')} anomaly on {svc.name}", severity=anomaly.severity,
                   status=IncidentStatus.OPEN, source="auto", primary_service_id=svc.id, opened_at=now,
                   last_activity_at=now)
    session.add(inc)
    await session.flush()
    session.add(IncidentService(incident_id=inc.id, service_id=svc.id))
    anomaly.incident_id = inc.id
    timeline(session, inc, "incident.opened", f"opened automatically from {anomaly.method} anomaly on {svc.name} "
             f"({anomaly.metric}={anomaly.observed_value:.4g})", data={"anomaly_id": anomaly.id})
    await audit(session, actor=None, action="incident.created", target_type="incident", target_id=inc.id,
                details={"source": "auto", "anomaly_id": anomaly.id})
    await session.flush()
    await session.refresh(inc, attribute_names=["services"])
    return inc, True


async def create_manual(session: AsyncSession, *, title: str, severity: Severity, service_names: list[str],
                        actor: User, description: str | None) -> Incident:
    svcs = await services_by_name(session)
    unknown = [n for n in service_names if n not in svcs]
    if unknown:
        raise NotFound(f"unknown services: {', '.join(unknown)}")
    now = datetime.now(UTC)
    inc = Incident(title=title, severity=severity, status=IncidentStatus.OPEN, source="manual",
                   primary_service_id=svcs[service_names[0]].id if service_names else None,
                   opened_at=now, last_activity_at=now)
    session.add(inc)
    await session.flush()
    for n in service_names:
        session.add(IncidentService(incident_id=inc.id, service_id=svcs[n].id))
    timeline(session, inc, "incident.opened", f"opened manually by {actor.username}" + (f": {description}" if description else ""), actor)
    await audit(session, actor=actor, action="incident.created", target_type="incident", target_id=inc.id,
                details={"source": "manual"})
    await session.flush()
    await session.refresh(inc, attribute_names=["services"])
    return inc


def incident_to_dict(inc: Incident) -> dict[str, Any]:
    return {
        "id": inc.id, "title": inc.title, "status": inc.status.value, "severity": inc.severity.value,
        "source": inc.source, "primary_service": inc.primary_service.name if inc.primary_service else None,
        "services": sorted(s.name for s in inc.services), "opened_at": inc.opened_at,
        "last_activity_at": inc.last_activity_at, "acknowledged_at": inc.acknowledged_at,
        "resolved_at": inc.resolved_at, "resolution_note": inc.resolution_note,
        "recovery_verified_at": inc.recovery_verified_at,
    }
