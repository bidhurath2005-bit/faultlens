"""RCA orchestration: incident -> evidence -> hypotheses -> ranked findings -> persisted investigation.

This module deliberately has no access to fault-injection records: diagnoses are derived from
telemetry only (enforced by tests/test_rca_isolation.py).
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Anomaly,
    Incident,
    Investigation,
    InvestigationStatus,
    Recommendation,
    RootCauseFinding,
)
from app.services.incidents import services_by_name, timeline

from .evidence import EvidenceBundle, EvidenceCollector, bundle_summary
from .hypotheses import INCONCLUSIVE_BELOW, Hypothesis, confidence_label, generate
from .recommendations import recommendations_for

log = logging.getLogger("faultlens.rca")
ENGINE_VERSION = "1.0.0"
MAX_FINDINGS = 5
MAX_WINDOW = timedelta(minutes=15)


def investigation_window(incident: Incident, first_anomaly_at: datetime | None, now: datetime) -> tuple[datetime, datetime]:
    onset = min(x for x in (incident.opened_at, first_anomaly_at) if x is not None)
    start = max(onset - timedelta(minutes=3), now - MAX_WINDOW)
    return start, now


def rank(hyps: list[Hypothesis]) -> tuple[list[Hypothesis], bool]:
    """Returns (top findings, conclusive). Deterministic ordering for equal scores."""
    top = hyps[:MAX_FINDINGS]
    return top, bool(top) and top[0].score >= INCONCLUSIVE_BELOW


def summarise_text(top: list[Hypothesis], conclusive: bool, ev: EvidenceBundle) -> str:
    missing = [k for k, v in ev.sources.items() if v != "ok"]
    gap = f" Evidence gaps: {', '.join(missing)} unavailable." if missing else ""
    if not top:
        return "Inconclusive: no hypothesis had sufficient primary evidence in the investigation window." + gap
    h = top[0]
    if not conclusive:
        return (f"Inconclusive: best candidate '{h.title}' scored {h.score:.2f}, below the {INCONCLUSIVE_BELOW} "
                f"evidence threshold." + gap)
    return f"Most likely root cause: {h.title} (score {h.score:.2f}, {confidence_label(h.score)} confidence)." + gap


def log_highlights(ev: EvidenceBundle, limit: int = 8) -> list[dict]:
    rows = []
    for svc, ls in ev.logs.items():
        for t, n in ls.templates.most_common(4):
            if t.startswith("request completed"):
                continue
            rows.append({"service": svc, "template": t, "count": n, "sample": ls.samples[t].as_dict()})
    return sorted(rows, key=lambda r: -r["count"])[:limit]


async def run_investigation(session: AsyncSession, collector: EvidenceCollector, incident: Incident,
                            trigger: str = "auto") -> Investigation:
    started = time.perf_counter()
    now = datetime.now(UTC)
    first = (await session.execute(select(Anomaly.detected_at).where(Anomaly.incident_id == incident.id)
                                   .order_by(Anomaly.detected_at).limit(1))).scalar_one_or_none()
    w_start, w_end = investigation_window(incident, first, now)
    inv = Investigation(incident_id=incident.id, status=InvestigationStatus.RUNNING, trigger=trigger,
                        window_start=w_start, window_end=w_end, engine_version=ENGINE_VERSION, findings=[])
    session.add(inv)
    await session.flush()
    try:
        onset = min(x for x in (incident.opened_at, first) if x is not None) - timedelta(seconds=60)
        ev = await collector.collect(w_start, w_end, onset=max(onset, w_start))
        hyps = generate(ev)
        top, conclusive = rank(hyps)
        svcs = await services_by_name(session)
        focus = sorted({h.service for h in top} | {s.name for s in incident.services})
        evidence = bundle_summary(ev, focus)
        evidence["log_highlights"] = log_highlights(ev)
        evidence["hypotheses_considered"] = len(hyps)
        for i, h in enumerate(top, start=1):
            f = RootCauseFinding(
                rank=i, service_id=svcs[h.service].id if h.service in svcs else None, cause_type=h.cause_type,
                title=h.title, score=h.score, confidence=confidence_label(h.score),
                signals=[s.to_dict() for s in sorted(h.signals, key=lambda s: -s.contribution)],
                evidence={"locality": h.locality, "locality_reason": h.locality_reason, "raw_score": round(h.raw_score, 4),
                          "related_services": h.related_services,
                          "upstream_affected": sorted(ev.graph.transitive_upstream(h.service))},
            )
            for r in recommendations_for(h, ev.graph.upstream(h.service)):
                f.recommendations.append(Recommendation(**r))
            inv.findings.append(f)
        inv.status = InvestigationStatus.COMPLETED if conclusive else InvestigationStatus.INCONCLUSIVE
        inv.summary = summarise_text(top, conclusive, ev)
        inv.evidence = evidence
        if conclusive and incident.source == "auto":
            incident.title = top[0].title
            if top[0].service in svcs:
                incident.primary_service_id = svcs[top[0].service].id
        timeline(session, incident, "investigation.completed", inv.summary,
                 data={"investigation_id": inv.id, "top": top[0].cause_type if top else None,
                       "score": top[0].score if top else None})
    except Exception as exc:  # noqa: BLE001
        log.exception("investigation failed", extra={"incident_id": incident.id})
        inv.status = InvestigationStatus.FAILED
        inv.summary = f"Investigation failed: {type(exc).__name__}"
    inv.completed_at = datetime.now(UTC)
    inv.duration_ms = round((time.perf_counter() - started) * 1000, 1)
    await session.flush()
    # Re-load with eager relationships (findings -> service, recommendations): async sessions cannot lazy-load.
    return (await session.execute(select(Investigation).where(Investigation.id == inv.id)
                                  .execution_options(populate_existing=True))).scalar_one()


def investigation_to_dict(inv: Investigation) -> dict:
    return {
        "id": inv.id, "incident_id": inv.incident_id, "status": inv.status.value, "trigger": inv.trigger,
        "started_at": inv.started_at, "completed_at": inv.completed_at, "window_start": inv.window_start,
        "window_end": inv.window_end, "duration_ms": inv.duration_ms, "summary": inv.summary,
        "engine_version": inv.engine_version, "evidence": inv.evidence,
        "findings": [
            {"id": f.id, "rank": f.rank, "service": f.service.name if f.service else None, "cause_type": f.cause_type,
             "title": f.title, "score": f.score, "confidence": f.confidence, "signals": f.signals, "evidence": f.evidence,
             "recommendations": [{"priority": r.priority, "action": r.action, "rationale": r.rationale, "risk": r.risk}
                                 for r in f.recommendations]}
            for f in inv.findings
        ],
    }
