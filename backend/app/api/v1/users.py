"""User administration (Admin only)."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require
from app.api.schemas import Page, UserCreate, UserOut, UserUpdate
from app.core.errors import AppError, Conflict, NotFound
from app.core.security import Permission, hash_password, validate_password_policy
from app.db.models import AuditEvent, RefreshToken, Role, User
from app.db.session import get_session
from app.services.incidents import audit

router = APIRouter(prefix="/users", tags=["users"])
admin = require(Permission.USERS_MANAGE)


def _policy(pw: str) -> None:
    try:
        validate_password_policy(pw)
    except ValueError as exc:
        raise AppError(str(exc), code="weak_password", status_code=422) from None


@router.get("", response_model=Page[UserOut])
async def list_users(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     actor: User = Depends(admin), session: AsyncSession = Depends(get_session)) -> Page[UserOut]:
    total = (await session.execute(select(func.count(User.id)))).scalar_one()
    rows = (await session.execute(select(User).order_by(User.id).limit(limit).offset(offset))).scalars().all()
    return Page(items=[UserOut.model_validate(u) for u in rows], total=total, limit=limit, offset=offset)


@router.post("", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, actor: User = Depends(admin), session: AsyncSession = Depends(get_session)) -> UserOut:
    _policy(body.password)
    if (await session.execute(select(User).where(User.username == body.username))).scalar_one_or_none():
        raise Conflict("username already exists")
    u = User(username=body.username, password_hash=hash_password(body.password), role=body.role, is_active=True)
    session.add(u)
    await session.flush()
    await audit(session, actor=actor, action="user.created", target_type="user", target_id=u.id, details={"role": u.role.value})
    await session.refresh(u)
    return UserOut.model_validate(u)


@router.patch("/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate, actor: User = Depends(admin),
                      session: AsyncSession = Depends(get_session)) -> UserOut:
    u = await session.get(User, user_id, with_for_update=True)
    if u is None:
        raise NotFound("user not found")
    changes: dict = {}
    if u.id == actor.id and (body.role not in (None, Role.ADMIN) or body.is_active is False):
        raise Conflict("admins cannot demote or deactivate themselves")
    if body.role is not None and body.role != u.role:
        changes["role"] = [u.role.value, body.role.value]
        u.role = body.role
    if body.is_active is not None and body.is_active != u.is_active:
        changes["is_active"] = [u.is_active, body.is_active]
        u.is_active = body.is_active
    if body.password:
        _policy(body.password)
        u.password_hash = hash_password(body.password)
        changes["password"] = "reset"
    if changes:
        # Any security-relevant change invalidates the user's sessions.
        await session.execute(update(RefreshToken).where(RefreshToken.user_id == u.id, RefreshToken.revoked_at.is_(None))
                              .values(revoked_at=datetime.now(UTC)))
        await audit(session, actor=actor, action="user.updated", target_type="user", target_id=u.id, details=changes)
    await session.flush()
    await session.refresh(u)
    return UserOut.model_validate(u)


audit_router = APIRouter(prefix="/audit", tags=["audit"])


@audit_router.get("")
async def list_audit(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), action: str | None = Query(None, max_length=64),
                     actor: User = Depends(require(Permission.AUDIT_READ)), session: AsyncSession = Depends(get_session)) -> dict:
    stmt = select(AuditEvent)
    cnt = select(func.count(AuditEvent.id))
    if action:
        stmt, cnt = stmt.where(AuditEvent.action == action), cnt.where(AuditEvent.action == action)
    total = (await session.execute(cnt)).scalar_one()
    rows = (await session.execute(stmt.order_by(AuditEvent.occurred_at.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [{"id": a.id, "occurred_at": a.occurred_at, "actor": a.actor_username, "action": a.action,
                       "target_type": a.target_type, "target_id": a.target_id, "outcome": a.outcome,
                       "client_ip": a.client_ip, "details": a.details} for a in rows],
            "total": total, "limit": limit, "offset": offset}
