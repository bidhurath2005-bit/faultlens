"""Authentication & authorisation dependencies. Every protected route declares a Permission."""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import Forbidden, TooManyRequests, Unauthorized
from app.core.redis import rate_limit
from app.core.security import Permission, TokenError, decode_access_token, has_permission
from app.db.models import User
from app.db.session import get_session

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    user: User
    claims: dict


async def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Principal:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized("authentication required")
    try:
        claims = decode_access_token(creds.credentials, settings.jwt_secret, settings.jwt_algorithm)
    except TokenError:
        raise Unauthorized("invalid or expired token") from None
    user = await session.get(User, int(claims["sub"]))
    # Re-check the live user record: deactivation or role change takes effect immediately.
    if user is None or not user.is_active:
        raise Unauthorized("account disabled")
    if user.role.value != claims.get("role"):
        raise Unauthorized("role changed; please sign in again")
    return Principal(user, claims)


def require(perm: Permission):
    async def _dep(p: Principal = Depends(current_principal)) -> User:
        if not has_permission(p.user.role, perm):
            raise Forbidden(f"role '{p.user.role.value}' lacks permission '{perm.value}'")
        return p.user
    return _dep


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(name: str, per_window: int, window_s: int = 60):
    async def _dep(request: Request) -> None:
        ok, retry = await rate_limit(f"{name}:{client_ip(request)}", per_window, window_s)
        if not ok:
            raise TooManyRequests(f"too many requests; retry in {retry}s")
    return _dep
