"""Payment Service: authorises and records payments for orders."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, FastAPI, HTTPException, Request
from faultlens_common.app import create_service_app
from faultlens_common.config import get_settings
from pydantic import BaseModel, Field
from sqlalchemy import text

router = APIRouter(tags=["payments"])

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS payments;
CREATE TABLE IF NOT EXISTS payments.payments (
  id UUID PRIMARY KEY,
  order_ref TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_payments_user ON payments.payments(user_id);
"""


class PaymentIn(BaseModel):
    order_ref: str = Field(min_length=1, max_length=64)
    user_id: int = Field(gt=0)
    amount_cents: int = Field(gt=0, le=10_000_000)
    # Card data is never accepted or logged: the demo uses an opaque tokenised method id.
    payment_method: str = Field(default="tok_demo_visa", pattern=r"^tok_[a-z0-9_]{3,32}$")


async def init_db(app: FastAPI) -> None:
    async with app.state.db.engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1002)"))
        for stmt in filter(str.strip, SCHEMA.split(";")):
            await conn.execute(text(stmt))


@router.post("/payments", status_code=201)
async def create_payment(body: PaymentIn, request: Request) -> dict:
    # Deterministic business rule for realism: tokens ending in "_declined" are declined.
    status = "declined" if body.payment_method.endswith("_declined") else "captured"
    async with request.app.state.db.connection() as conn:
        dup = (await conn.execute(text("SELECT id,status FROM payments.payments WHERE order_ref=:o"),
                                  {"o": body.order_ref})).first()
        if dup:
            return {"payment_id": str(dup.id), "status": dup.status, "idempotent_replay": True}
        pid = uuid.uuid4()
        await conn.execute(text("INSERT INTO payments.payments(id,order_ref,user_id,amount_cents,status) "
                                "VALUES (:id,:o,:u,:a,:s)"),
                           {"id": pid, "o": body.order_ref, "u": body.user_id, "a": body.amount_cents, "s": status})
    if status == "declined":
        raise HTTPException(402, "payment declined")
    return {"payment_id": str(pid), "status": status}


@router.get("/payments/{order_ref}")
async def get_payment(order_ref: str, request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        r = (await conn.execute(text("SELECT id,order_ref,user_id,amount_cents,status FROM payments.payments WHERE order_ref=:o"),
                                {"o": order_ref})).first()
    if r is None:
        raise HTTPException(404, "payment not found")
    d = dict(r._mapping)
    d["id"] = str(d["id"])
    return d


app = create_service_app(get_settings(), [router], on_startup=init_db)
