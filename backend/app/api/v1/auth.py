"""Login / refresh / logout / me. See app/core/security.py for the token lifecycle."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, client_ip, current_principal, limit
from app.api.schemas import LoginIn, PasswordChange, TokenOut, UserOut
from app.core.config import Settings, get_settings
from app.core.errors import AppError, Unauthorized
from app.core.security import (
    create_access_token,
    hash_password,
    hash_refresh_token,
    needs_rehash,
    new_refresh_token,
    validate_password_policy,
    verify_password,
)
from app.db.models import RefreshToken, User
from app.db.session import get_session
from app.services.incidents import audit

router = APIRouter(prefix="/auth", tags=["auth"])
COOKIE = "fl_refresh"
COOKIE_PATH = "/api/v1/auth"


def _set_cookie(resp: Response, raw: str, settings: Settings) -> None:
    resp.set_cookie(COOKIE, raw, max_age=settings.refresh_token_ttl_hours * 3600, httponly=True,
                    secure=settings.cookie_secure, samesite="strict", path=COOKIE_PATH)


async def _issue(session: AsyncSession, user: User, settings: Settings, family: uuid.UUID | None = None) -> tuple[str, str, int]:
    access, ttl = create_access_token(user_id=user.id, username=user.username, role=user.role,
                                      secret=settings.jwt_secret, ttl_minutes=settings.access_token_ttl_minutes)
    raw, digest = new_refresh_token()
    session.add(RefreshToken(user_id=user.id, token_hash=digest, family_id=family or uuid.uuid4(),
                             expires_at=datetime.now(UTC) + timedelta(hours=settings.refresh_token_ttl_hours)))
    return access, raw, ttl


@router.post("/login", response_model=TokenOut, dependencies=[Depends(limit("login", 10, 60))])
async def login(body: LoginIn, request: Request, response: Response, session: AsyncSession = Depends(get_session),
                settings: Settings = Depends(get_settings)) -> TokenOut:
    user = (await session.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    # verify_password runs a full hash even for unknown users (no user-enumeration timing oracle).
    if not verify_password(body.password, user.password_hash if user else None) or user is None or not user.is_active:
        await audit(session, actor=None, action="auth.login", target_type="user", target_id=body.username,
                    outcome="failure", client_ip=client_ip(request))
        await session.commit()
        raise Unauthorized("invalid username or password")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    user.last_login_at = datetime.now(UTC)
    access, raw, ttl = await _issue(session, user, settings)
    await audit(session, actor=user, action="auth.login", target_type="user", target_id=user.id, client_ip=client_ip(request))
    _set_cookie(response, raw, settings)
    return TokenOut(access_token=access, expires_in=ttl, user=UserOut.model_validate(user))


@router.post("/refresh", response_model=TokenOut, dependencies=[Depends(limit("refresh", 30, 60))])
async def refresh(response: Response, request: Request, fl_refresh: str | None = Cookie(default=None),
                  session: AsyncSession = Depends(get_session), settings: Settings = Depends(get_settings)) -> TokenOut:
    if not fl_refresh:
        raise Unauthorized("no refresh token")
    tok = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(fl_refresh))
                                 .with_for_update())).scalar_one_or_none()
    now = datetime.now(UTC)
    if tok is None:
        raise Unauthorized("invalid refresh token")
    if tok.revoked_at is not None:
        # Re-use of a rotated token: assume theft, revoke the entire family.
        await session.execute(update(RefreshToken).where(RefreshToken.family_id == tok.family_id, RefreshToken.revoked_at.is_(None))
                              .values(revoked_at=now))
        await audit(session, actor=None, action="auth.refresh_reuse_detected", target_type="user", target_id=tok.user_id,
                    outcome="failure", client_ip=client_ip(request))
        await session.commit()
        response.delete_cookie(COOKIE, path=COOKIE_PATH)
        raise Unauthorized("refresh token reuse detected; all sessions revoked")
    if tok.expires_at <= now:
        raise Unauthorized("refresh token expired")
    user = await session.get(User, tok.user_id)
    if user is None or not user.is_active:
        raise Unauthorized("account disabled")
    tok.revoked_at = now
    access, raw, ttl = await _issue(session, user, settings, family=tok.family_id)
    _set_cookie(response, raw, settings)
    return TokenOut(access_token=access, expires_in=ttl, user=UserOut.model_validate(user))


@router.post("/logout", status_code=204)
async def logout(response: Response, request: Request, fl_refresh: str | None = Cookie(default=None),
                 session: AsyncSession = Depends(get_session)) -> Response:
    if fl_refresh:
        tok = (await session.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(fl_refresh)))).scalar_one_or_none()
        if tok:
            await session.execute(update(RefreshToken).where(RefreshToken.family_id == tok.family_id, RefreshToken.revoked_at.is_(None))
                                  .values(revoked_at=datetime.now(UTC)))
            await audit(session, actor=None, action="auth.logout", target_type="user", target_id=tok.user_id,
                        client_ip=client_ip(request))
    response.status_code = 204
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    return response


@router.get("/me", response_model=UserOut)
async def me(p: Principal = Depends(current_principal)) -> UserOut:
    return UserOut.model_validate(p.user)


@router.post("/password", status_code=204, dependencies=[Depends(limit("pwchange", 5, 300))])
async def change_password(body: PasswordChange, response: Response, p: Principal = Depends(current_principal),
                          session: AsyncSession = Depends(get_session)) -> Response:
    user = await session.get(User, p.user.id)
    if not verify_password(body.current_password, user.password_hash):
        raise Unauthorized("current password is incorrect")
    try:
        validate_password_policy(body.new_password)
    except ValueError as exc:
        raise AppError(str(exc), code="weak_password", status_code=422) from None
    user.password_hash = hash_password(body.new_password)
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
                          .values(revoked_at=datetime.now(UTC)))
    await audit(session, actor=user, action="auth.password_changed", target_type="user", target_id=user.id)
    response.status_code = 204
    return response
