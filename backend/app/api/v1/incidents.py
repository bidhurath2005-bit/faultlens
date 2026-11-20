"""Incidents, anomalies, RCA investigations and the explanation assistant."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.explainer import Explainer, facts_from_investigation
from app.api.deps import require
from app.api.schemas import AskIn, IncidentCreate, IncidentOut, Page, ResolveIn, TransitionIn
from app.api.v1.observability import _anomaly_dict
from app.core.config import DEMO_SERVICES, Settings, get_settings
from app.core.errors import NotFound
from app.core.security import Permission
from app.db.models import (
    Anomaly,
    Incident,
    IncidentEvent,
    IncidentStatus,
    Investigation,
    RootCauseFinding,
    Service,
    Severity,
    User,
)
from app.db.session import get_session
from app.engines.rca.engine import investigation_to_dict, run_investigation
from app.engines.rca.evidence import EvidenceCollector
from app.events.bus import TOPIC_INCIDENTS, emit
from app.services import incidents as svc
from app.telemetry.clients import get_telemetry

router = APIRouter(tags=["incidents"])
reader = require(Permission.READ)
writer = require(Permission.INCIDENT_WRITE)


async def _publish(inc: Incident, event_type: str, actor: User | None = None, extra: dict | None = None) -> None:
    data = {"incident": svc.incident_to_dict(inc) | {"opened_at": inc.opened_at.isoformat()}, **(extra or {})}
    if actor:
        data["actor"] = actor.username
    await emit(TOPIC_INCIDENTS, event_type, data, source="api", key=str(inc.id))


@router.get("/incidents", response_model=Page[IncidentOut])
async def list_incidents(status: IncidentStatus | None = None, severity: Severity | None = None,
                         service: str | None = Query(None, max_length=64), since: datetime | None = None,
                         limit: int = Query(25, ge=1, le=200), offset: int = Query(0, ge=0),
                         user: User = Depends(reader), session: AsyncSession = Depends(get_session)) -> Page[IncidentOut]:
    stmt = select(Incident)
    if status:
        stmt = stmt.where(Incident.status == status)
    if severity:
        stmt = stmt.where(Incident.severity == severity)
    if since:
        stmt = stmt.where(Incident.opened_at >= since)
    if service:
        stmt = stmt.where(Incident.services.any(Service.name == service))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Incident.opened_at.desc()).limit(limit).offset(offset))).unique().scalars().all()
    return Page(items=[IncidentOut(**svc.incident_to_dict(i)) for i in rows], total=total, limit=limit, offset=offset)


@router.post("/incidents", response_model=IncidentOut, status_code=201)
async def create_incident(body: IncidentCreate, user: User = Depends(writer), session: AsyncSession = Depends(get_session)) -> IncidentOut:
    inc = await svc.create_manual(session, title=body.title, severity=body.severity, service_names=body.services,
                                  actor=user, description=body.description)
    await session.commit()
    await _publish(inc, "incident.created", user)
    return IncidentOut(**svc.incident_to_dict(inc))


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: int, user: User = Depends(reader), session: AsyncSession = Depends(get_session)) -> dict:
    inc = await svc.get_incident(session, incident_id)
    events = (await session.execute(select(IncidentEvent).where(IncidentEvent.incident_id == incident_id)
                                    .order_by(IncidentEvent.occurred_at))).unique().scalars().all()
    anomalies = (await session.execute(select(Anomaly).where(Anomaly.incident_id == incident_id)
                                       .order_by(Anomaly.detected_at))).scalars().all()
    invs = (await session.execute(select(Investigation.id, Investigation.status, Investigation.started_at, Investigation.summary)
                                  .where(Investigation.incident_id == incident_id).order_by(Investigation.started_at.desc()))).all()
    return {
        **svc.incident_to_dict(inc),
        "timeline": [{"id": e.id, "occurred_at": e.occurred_at, "kind": e.kind, "message": e.message,
                      "actor": e.actor.username if e.actor else None, "data": e.data} for e in events],
        "anomalies": [_anomaly_dict(a) for a in anomalies],
        "investigations": [{"id": i.id, "status": i.status.value, "started_at": i.started_at, "summary": i.summary} for i in invs],
    }


async def _transition(incident_id: int, dst: IncidentStatus, user: User, session: AsyncSession, note: str | None) -> IncidentOut:
    inc = await svc.transition(session, incident_id, dst, user, note)
    await session.commit()
    await _publish(inc, f"incident.{dst.value}", user)
    return IncidentOut(**svc.incident_to_dict(inc))


@router.post("/incidents/{incident_id}/acknowledge", response_model=IncidentOut)
async def acknowledge(incident_id: int, body: TransitionIn | None = None, user: User = Depends(writer),
                      session: AsyncSession = Depends(get_session)) -> IncidentOut:
    return await _transition(incident_id, IncidentStatus.ACKNOWLEDGED, user, session, body.note if body else None)


@router.post("/incidents/{incident_id}/investigate", response_model=IncidentOut)
async def start_investigating(incident_id: int, body: TransitionIn | None = None, user: User = Depends(writer),
                              session: AsyncSession = Depends(get_session)) -> IncidentOut:
    return await _transition(incident_id, IncidentStatus.INVESTIGATING, user, session, body.note if body else None)


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentOut)
async def resolve(incident_id: int, body: ResolveIn, user: User = Depends(writer),
                  session: AsyncSession = Depends(get_session)) -> IncidentOut:
    return await _transition(incident_id, IncidentStatus.RESOLVED, user, session, body.note)


# ------------------------------------------------------------------ anomalies
@router.get("/anomalies")
async def list_anomalies(service: str | None = Query(None, max_length=64), method: str | None = Query(None, max_length=32),
                         severity: Severity | None = None, since: datetime | None = None, incident_id: int | None = None,
                         limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                         user: User = Depends(reader), session: AsyncSession = Depends(get_session)) -> dict:
    stmt = select(Anomaly)
    if service:
        stmt = stmt.join(Service).where(Service.name == service)
    if method:
        stmt = stmt.where(Anomaly.method == method)
    if severity:
        stmt = stmt.where(Anomaly.severity == severity)
    if since:
        stmt = stmt.where(Anomaly.detected_at >= since)
    if incident_id:
        stmt = stmt.where(Anomaly.incident_id == incident_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await session.execute(stmt.order_by(Anomaly.detected_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [_anomaly_dict(a) for a in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/anomalies/stats")
async def anomaly_stats(hours: int = Query(24, ge=1, le=168), user: User = Depends(reader),
                        session: AsyncSession = Depends(get_session)) -> dict:
    from datetime import timedelta
    since = datetime.now(UTC) - timedelta(hours=hours)
    bucket = func.date_trunc("minute", Anomaly.detected_at)
    by_method = dict((await session.execute(select(Anomaly.method, func.count()).where(Anomaly.detected_at >= since)
                                            .group_by(Anomaly.method))).all())
    by_metric = dict((await session.execute(select(Anomaly.metric, func.count()).where(Anomaly.detected_at >= since)
                                            .group_by(Anomaly.metric))).all())
    trend = (await session.execute(select(bucket, func.count()).where(Anomaly.detected_at >= since)
                                   .group_by(bucket).order_by(bucket))).all()
    return {"by_method": by_method, "by_metric": by_metric, "trend": [[t, n] for t, n in trend]}


# ------------------------------------------------------------------ RCA
@router.get("/rca/investigations/{investigation_id}")
async def get_investigation(investigation_id: int, user: User = Depends(reader), session: AsyncSession = Depends(get_session)) -> dict:
    inv = await session.get(Investigation, investigation_id)
    if inv is None:
        raise NotFound("investigation not found")
    return investigation_to_dict(inv)


@router.get("/rca/{incident_id}")
async def get_rca(incident_id: int, user: User = Depends(reader), session: AsyncSession = Depends(get_session)) -> dict:
    await svc.get_incident(session, incident_id)
    inv = (await session.execute(select(Investigation).where(Investigation.incident_id == incident_id)
                                 .order_by(Investigation.started_at.desc()).limit(1))).scalar_one_or_none()
    if inv is None:
        raise NotFound("no investigation has run for this incident yet")
    return investigation_to_dict(inv)


@router.post("/rca/{incident_id}/run")
async def rerun_rca(incident_id: int, user: User = Depends(require(Permission.INVESTIGATION_RUN)),
                    session: AsyncSession = Depends(get_session)) -> dict:
    inc = await svc.get_incident(session, incident_id)
    inv = await run_investigation(session, EvidenceCollector(get_telemetry(), DEMO_SERVICES), inc, trigger=f"manual:{user.username}")
    await svc.audit(session, actor=user, action="investigation.run", target_type="incident", target_id=incident_id)
    await session.commit()
    out = investigation_to_dict(inv)
    await emit(TOPIC_INCIDENTS, "investigation.completed",
               {"incident_id": incident_id, "investigation_id": inv.id, "status": inv.status.value, "summary": inv.summary},
               source="api", key=str(incident_id))
    return out


@router.get("/rca")
async def list_investigations(limit: int = Query(20, ge=1, le=100), user: User = Depends(reader),
                              session: AsyncSession = Depends(get_session)) -> dict:
    rows = (await session.execute(select(Investigation).order_by(Investigation.started_at.desc()).limit(limit))).scalars().all()
    inc_ids = {i.incident_id for i in rows}
    incs = {i.id: i for i in (await session.execute(select(Incident).where(Incident.id.in_(inc_ids)))).unique().scalars()} if inc_ids else {}
    out = []
    for i in rows:
        top = i.findings[0] if i.findings else None
        inc = incs.get(i.incident_id)
        out.append({"id": i.id, "incident_id": i.incident_id, "incident_title": inc.title if inc else None,
                    "incident_status": inc.status.value if inc else None, "status": i.status.value,
                    "started_at": i.started_at, "duration_ms": i.duration_ms, "summary": i.summary,
                    "top_cause": top.cause_type if top else None, "top_service": top.service.name if top and top.service else None,
                    "top_score": top.score if top else None})
    return {"items": out}


async def _history(session: AsyncSession, inc: Incident, cause: str | None, service_id: int | None) -> list[dict]:
    if not cause:
        return []
    q = (select(Incident).join(Investigation, Investigation.incident_id == Incident.id)
         .join(RootCauseFinding, RootCauseFinding.investigation_id == Investigation.id)
         .where(Incident.id != inc.id, Incident.status == IncidentStatus.RESOLVED, RootCauseFinding.rank == 1,
                RootCauseFinding.cause_type == cause, RootCauseFinding.service_id == service_id)
         .order_by(Incident.resolved_at.desc()).limit(5))
    return [{"id": i.id, "title": i.title, "resolved_at": i.resolved_at.isoformat() if i.resolved_at else None,
             "resolution_note": i.resolution_note} for i in (await session.execute(q)).unique().scalars()]


@router.post("/assistant/incidents/{incident_id}")
async def ask(incident_id: int, body: AskIn | None = None, user: User = Depends(reader),
              session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> dict:
    inc = await svc.get_incident(session, incident_id)
    inv = (await session.execute(select(Investigation).where(Investigation.incident_id == incident_id)
                                 .order_by(Investigation.started_at.desc()).limit(1))).scalar_one_or_none()
    if inv is None:
        raise NotFound("no investigation to explain yet")
    inv_d = investigation_to_dict(inv)
    inv_d["log_highlights"] = [{"service": h["service"], "template": h["template"], "count": h["count"]}
                               for h in (inv.evidence or {}).get("log_highlights", [])]
    top = inv.findings[0] if inv.findings else None
    history = await _history(session, inc, top.cause_type if top else None, top.service_id if top else None)
    facts = facts_from_investigation(inv_d, svc.incident_to_dict(inc) | {"opened_at": inc.opened_at.isoformat()}, history)
    exp = await Explainer(settings).explain(facts, body.question if body else None)
    return {"incident_id": incident_id, "investigation_id": inv.id, "question": body.question if body else None,
            **exp.to_dict(), "facts": facts}
