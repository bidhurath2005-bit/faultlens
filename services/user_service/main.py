"""User Service: customer accounts. Called by the Order Service to validate buyers."""
from __future__ import annotations

import random

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from faultlens_common.app import create_service_app
from faultlens_common.config import get_settings
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text

router = APIRouter(tags=["users"])


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    tier: str = Field(default="standard", pattern="^(standard|premium)$")


SCHEMA = """
CREATE SCHEMA IF NOT EXISTS users;
CREATE TABLE IF NOT EXISTS users.users (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  tier TEXT NOT NULL DEFAULT 'standard',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def init_db(app: FastAPI) -> None:
    async with app.state.db.engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(1001)"))
        for stmt in filter(str.strip, SCHEMA.split(";")):
            await conn.execute(text(stmt))
        count = (await conn.execute(text("SELECT count(*) FROM users.users"))).scalar_one()
        if count == 0:
            rng = random.Random(42)  # deterministic demo seed data
            rows = [{"n": f"Customer {i}", "e": f"customer{i}@example.com",
                     "t": "premium" if rng.random() < 0.2 else "standard"} for i in range(1, 201)]
            await conn.execute(text("INSERT INTO users.users(name,email,tier) VALUES (:n,:e,:t)"), rows)


def _row(r) -> dict:  # noqa: ANN001
    return {"id": r.id, "name": r.name, "email": r.email, "tier": r.tier}


@router.get("/users/{user_id}")
async def get_user(user_id: int, request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        r = (await conn.execute(text("SELECT id,name,email,tier FROM users.users WHERE id=:id"), {"id": user_id})).first()
    if r is None:
        raise HTTPException(404, "user not found")
    return _row(r)


@router.get("/users")
async def list_users(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict:
    async with request.app.state.db.connection() as conn:
        rows = (await conn.execute(text("SELECT id,name,email,tier FROM users.users ORDER BY id LIMIT :l OFFSET :o"),
                                   {"l": limit, "o": offset})).all()
    return {"items": [_row(r) for r in rows], "limit": limit, "offset": offset}


@router.post("/users", status_code=201)
async def create_user(body: UserIn, request: Request) -> dict:
    async with request.app.state.db.connection() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM users.users WHERE email=:e"), {"e": body.email})).first()
        if exists:
            raise HTTPException(409, "email already registered")
        r = (await conn.execute(text("INSERT INTO users.users(name,email,tier) VALUES (:n,:e,:t) RETURNING id,name,email,tier"),
                                {"n": body.name, "e": body.email, "t": body.tier})).first()
    return _row(r)


app = create_service_app(get_settings(), [router], on_startup=init_db)
