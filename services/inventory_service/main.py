"""Inventory Service: product catalogue and stock reservations."""
from __future__ import annotations

import random
import uuid

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from faultlens_common.app import create_service_app
from faultlens_common.config import get_settings
from pydantic import BaseModel, Field
from sqlalchemy import text

router = APIRouter(tags=["inventory"])

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS inventory;
CREATE TABLE IF NOT EXISTS inventory.products (
  id SERIAL PRIMARY KEY,
  sku TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  price_cents INTEGER NOT NULL CHECK (price_cents > 0),
  stock INTEGER NOT NULL CHECK (stock >= 0)
);
CREATE TABLE IF NOT EXISTS inventory.reservations (
  id UUID PRIMARY KEY,
  order_ref TEXT NOT NULL,
  product_id INTEGER NOT NULL REFERENCES inventory.products(id),
  quantity INTEGER NOT NULL CHECK (quantity > 0),
  status TEXT NOT NULL DEFAULT 'reserved',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_reservations_order ON inventory.reservations(order_ref);
"""

RESTOCK_LEVEL = 10_000


class ReservationItem(BaseModel):
    product_id: int = Field(gt=0)
    quantity: int = Field(gt=0, le=20)


class ReservationIn(BaseModel):
    order_ref: str = Field(min_length=1, max_length=64)
    items: list[ReservationItem] = Field(min_length=1, max_length=10)


async def init_db(app: FastAPI) -> None:
    async with app.state.db.engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1003)"))
        for stmt in filter(str.strip, SCHEMA.split(";")):
            await conn.execute(text(stmt))
        if (await conn.execute(text("SELECT count(*) FROM inventory.products"))).scalar_one() == 0:
            rng = random.Random(7)
            rows = [{"s": f"SKU-{i:04d}", "n": f"Product {i}", "p": rng.randint(499, 19999), "st": RESTOCK_LEVEL}
                    for i in range(1, 51)]
            await conn.execute(text("INSERT INTO inventory.products(sku,name,price_cents,stock) VALUES (:s,:n,:p,:st)"), rows)


@router.get("/products")
async def list_products(request: Request, limit: int = Query(20, ge=1, le=100)) -> dict:
    async with request.app.state.db.connection() as conn:
        rows = (await conn.execute(text("SELECT id,sku,name,price_cents,stock FROM inventory.products ORDER BY id LIMIT :l"),
                                   {"l": limit})).all()
    return {"items": [dict(r._mapping) for r in rows]}


@router.get("/products/{product_id}")
async def get_product(product_id: int, request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        r = (await conn.execute(text("SELECT id,sku,name,price_cents,stock FROM inventory.products WHERE id=:i"),
                                {"i": product_id})).first()
    if r is None:
        raise HTTPException(404, "product not found")
    return dict(r._mapping)


@router.post("/reservations", status_code=201)
async def reserve(body: ReservationIn, request: Request) -> dict:
    lines = []
    async with request.app.state.db.connection() as conn:
        for item in sorted(body.items, key=lambda i: i.product_id):  # stable lock order avoids deadlocks
            r = (await conn.execute(text("SELECT id,price_cents,stock FROM inventory.products WHERE id=:i FOR UPDATE"),
                                    {"i": item.product_id})).first()
            if r is None:
                raise HTTPException(404, f"product {item.product_id} not found")
            if r.stock < item.quantity:
                raise HTTPException(409, f"insufficient stock for product {item.product_id}")
            new_stock = r.stock - item.quantity
            if new_stock < 100:  # demo store never runs dry: replenish
                new_stock = RESTOCK_LEVEL
            await conn.execute(text("UPDATE inventory.products SET stock=:s WHERE id=:i"), {"s": new_stock, "i": r.id})
            rid = uuid.uuid4()
            await conn.execute(text("INSERT INTO inventory.reservations(id,order_ref,product_id,quantity) VALUES (:id,:o,:p,:q)"),
                               {"id": rid, "o": body.order_ref, "p": r.id, "q": item.quantity})
            lines.append({"reservation_id": str(rid), "product_id": r.id, "quantity": item.quantity,
                          "unit_price_cents": r.price_cents})
    return {"order_ref": body.order_ref, "lines": lines,
            "total_cents": sum(ln["unit_price_cents"] * ln["quantity"] for ln in lines)}


@router.post("/reservations/{order_ref}/release")
async def release(order_ref: str, request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        rows = (await conn.execute(text(
            "UPDATE inventory.reservations SET status='released' WHERE order_ref=:o AND status='reserved' "
            "RETURNING product_id, quantity"), {"o": order_ref})).all()
        for r in rows:
            await conn.execute(text("UPDATE inventory.products SET stock=stock+:q WHERE id=:i"), {"q": r.quantity, "i": r.product_id})
    return {"order_ref": order_ref, "released": len(rows)}


app = create_service_app(get_settings(), [router], on_startup=init_db)
