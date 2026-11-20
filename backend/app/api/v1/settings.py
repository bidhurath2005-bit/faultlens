"""Settings (Admin write, everyone read) and model-evaluation records."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.explainer import Explainer
from app.api.deps import require
from app.api.schemas import SettingsIn
from app.core.config import Settings, get_settings
from app.core.redis import get_redis
from app.core.security import Permission
from app.db.models import ModelEvaluation, User
from app.db.session import get_session
from app.services.incidents import audit
from app.services.settings_store import get_setting, put_setting

router = APIRouter(tags=["settings"])


@router.get("/settings")
async def read_settings(user: User = Depends(require(Permission.READ)), session: AsyncSession = Depends(get_session),
                        settings: Settings = Depends(get_settings)) -> dict:
    return {
        "detection": await get_setting(session, "detection"),
        "preferences": await get_setting(session, "preferences"),
        "telemetry": {"prometheus_url": settings.prometheus_url, "loki_url": settings.loki_url, "tempo_url": settings.tempo_url,
                      "detect_interval_s": settings.detect_interval_s, "probe_interval_s": settings.probe_interval_s},
        "services": sorted(settings.service_urls),
        "ai": {"provider": Explainer(settings).provider, "configured_provider": settings.llm_provider,
               "model": settings.llm_model or None, "api_key_configured": bool(settings.llm_api_key)},
        "fault_injection": {"enabled": settings.fault_injection_allowed, "environment": settings.environment},
        "auth": {"access_token_ttl_minutes": settings.access_token_ttl_minutes,
                 "refresh_token_ttl_hours": settings.refresh_token_ttl_hours},
    }


@router.put("/settings")
async def write_settings(body: SettingsIn, user: User = Depends(require(Permission.SETTINGS_WRITE)),
                         session: AsyncSession = Depends(get_session)) -> dict:
    out = {}
    if body.detection is not None:
        out["detection"] = await put_setting(session, "detection", body.detection, user.id)
    if body.preferences is not None:
        out["preferences"] = await put_setting(session, "preferences", body.preferences, user.id)
    await audit(session, actor=user, action="settings.updated", details={"keys": sorted(out)})
    await session.commit()
    try:
        await get_redis().publish("faultlens:settings", "changed")  # worker reloads detection config
    except Exception as exc:  # noqa: BLE001 - settings are persisted; the worker also reloads on restart
        logging.getLogger("faultlens.api").warning("settings change notification failed", extra={"error": str(exc)})
    return out


@router.get("/evaluations")
async def evaluations(kind: str | None = Query(None, pattern="^(anomaly|rca)$"), user: User = Depends(require(Permission.READ)),
                      session: AsyncSession = Depends(get_session)) -> dict:
    stmt = select(ModelEvaluation).order_by(ModelEvaluation.created_at.desc()).limit(100)
    if kind:
        stmt = stmt.where(ModelEvaluation.kind == kind)
    rows = (await session.execute(stmt)).scalars().all()
    return {"items": [{"id": r.id, "created_at": r.created_at, "kind": r.kind, "detector": r.detector, "dataset": r.dataset,
                       "dataset_description": r.dataset_description, "metrics": r.metrics, "params": r.params} for r in rows]}
