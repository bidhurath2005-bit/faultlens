"""Order Service: the entry point of the checkout workflow.

POST /orders -> User (validate) -> Inventory (reserve) -> Payment (charge) -> Notification (confirm)
On payment failure the inventory reservation is released (compensation).
"""
from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from faultlens_common.app import DownstreamError, create_service_app
from faultlens_common.config import get_settings
from pydantic import BaseModel, Field
from sqlalchemy import text

log = logging.getLogger("order")
router = APIRouter(tags=["orders"])

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS orders;
CREATE TABLE IF NOT EXISTS orders.orders (
  id UUID PRIMARY KEY,
  user_id INTEGER NOT NULL,
  total_cents INTEGER NOT NULL,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_orders_user ON orders.orders(user_id);
"""


class OrderItem(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=20)


class OrderIn(BaseModel):
    user_id: int = Field(gt=0)
    items: list[OrderItem] = Field(min_length=1, max_length=10)
    payment_method: str = Field(default="tok_demo_visa", pattern=r"^tok_[a-z0-9_]{3,32}$")


async def init_db(app: FastAPI) -> None:
    async with app.state.db.engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1000)"))
        for stmt in filter(str.strip, SCHEMA.split(";")):
            await conn.execute(text(stmt))


@router.post("/orders", status_code=201)
async def create_order(body: OrderIn, request: Request) -> dict:
    s = request.app.state.settings
    client = request.app.state.client
    order_id = uuid.uuid4()
    ref = str(order_id)

    user = await client.request("user-service", "GET", f"{s.user_service_url}/users/{body.user_id}")
    if user.status_code == 404:
        raise HTTPException(422, "unknown user")

    resv = await client.request("inventory-service", "POST", f"{s.inventory_service_url}/reservations",
                                json={"order_ref": ref, "items": [i.model_dump() for i in body.items]})
    if resv.status_code >= 400:
        raise HTTPException(409 if resv.status_code == 409 else 422, resv.json().get("detail", "reservation failed"))
    total = resv.json()["total_cents"]

    try:
        pay = await client.request("payment-service", "POST", f"{s.payment_service_url}/payments",
                                   json={"order_ref": ref, "user_id": body.user_id, "amount_cents": total,
                                         "payment_method": body.payment_method})
    except DownstreamError:
        await _compensate(request, ref)
        raise
    if pay.status_code == 402:
        await _compensate(request, ref)
        async with request.app.state.db.connection() as conn:
            await conn.execute(text("INSERT INTO orders.orders(id,user_id,total_cents,status) VALUES (:i,:u,:t,'payment_declined')"),
                               {"i": order_id, "u": body.user_id, "t": total})
        raise HTTPException(402, "payment declined")

    async with request.app.state.db.connection() as conn:
        await conn.execute(text("INSERT INTO orders.orders(id,user_id,total_cents,status) VALUES (:i,:u,:t,'confirmed')"),
                           {"i": order_id, "u": body.user_id, "t": total})

    notified = True
    try:
        await client.request("notification-service", "POST", f"{s.notification_service_url}/notifications",
                             json={"user_id": body.user_id, "order_ref": ref})
    except DownstreamError:
        # Confirmation is best-effort: the order stands, but we record the degraded path.
        notified = False
        log.warning("order confirmed but notification failed", extra={"order_id": ref})
    return {"order_id": ref, "status": "confirmed", "total_cents": total, "notified": notified}


async def _compensate(request: Request, ref: str) -> None:
    s = request.app.state.settings
    try:
        await request.app.state.client.request("inventory-service", "POST", f"{s.inventory_service_url}/reservations/{ref}/release")
    except DownstreamError:
        log.error("failed to release reservation during compensation", extra={"order_id": ref})


@router.get("/orders/{order_id}")
async def get_order(order_id: uuid.UUID, request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        r = (await conn.execute(text("SELECT id,user_id,total_cents,status,created_at FROM orders.orders WHERE id=:i"),
                                {"i": order_id})).first()
    if r is None:
        raise HTTPException(404, "order not found")
    return {**dict(r._mapping), "id": str(r.id)}


@router.get("/orders")
async def list_orders(request: Request, user_id: int = Query(gt=0), limit: int = Query(20, ge=1, le=100)) -> dict:
    async with request.app.state.db.connection() as conn:
        rows = (await conn.execute(text("SELECT id,user_id,total_cents,status,created_at FROM orders.orders "
                                        "WHERE user_id=:u ORDER BY created_at DESC LIMIT :l"), {"u": user_id, "l": limit})).all()
    return {"items": [{**dict(r._mapping), "id": str(r.id)} for r in rows]}


app = create_service_app(get_settings(), [router], on_startup=init_db)
