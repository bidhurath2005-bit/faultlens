"""Notification Service: records customer notifications (order confirmations)."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, FastAPI, Query, Request
from faultlens_common.app import create_service_app
from faultlens_common.config import get_settings
from pydantic import BaseModel, Field
from sqlalchemy import text

router = APIRouter(tags=["notifications"])

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS notifications;
CREATE TABLE IF NOT EXISTS notifications.notifications (
  id UUID PRIMARY KEY,
  user_id INTEGER NOT NULL,
  channel TEXT NOT NULL,
  template TEXT NOT NULL,
  order_ref TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_notifications_user ON notifications.notifications(user_id);
"""


class NotificationIn(BaseModel):
    user_id: int = Field(gt=0)
    channel: str = Field(default="email", pattern="^(email|sms|push)$")
    template: str = Field(default="order_confirmation", pattern="^[a-z_]{3,40}$")
    order_ref: str | None = Field(default=None, max_length=64)


async def init_db(app: FastAPI) -> None:
    async with app.state.db.engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1004)"))
        for stmt in filter(str.strip, SCHEMA.split(";")):
            await conn.execute(text(stmt))


@router.post("/notifications", status_code=202)
async def send(body: NotificationIn, request: Request) -> dict:
    nid = uuid.uuid4()
    async with request.app.state.db.connection() as conn:
        await conn.execute(text("INSERT INTO notifications.notifications(id,user_id,channel,template,order_ref) "
                                "VALUES (:i,:u,:c,:t,:o)"),
                           {"i": nid, "u": body.user_id, "c": body.channel, "t": body.template, "o": body.order_ref})
    return {"notification_id": str(nid), "status": "queued"}


@router.get("/notifications")
async def list_notifications(request: Request, user_id: int = Query(gt=0), limit: int = Query(20, ge=1, le=100)) -> dict:
    async with request.app.state.db.connection() as conn:
        rows = (await conn.execute(text("SELECT id,user_id,channel,template,order_ref FROM notifications.notifications "
                                        "WHERE user_id=:u ORDER BY created_at DESC LIMIT :l"), {"u": user_id, "l": limit})).all()
    return {"items": [{**dict(r._mapping), "id": str(r.id)} for r in rows]}


app = create_service_app(get_settings(), [router], on_startup=init_db)
