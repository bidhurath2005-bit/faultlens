"""Password hashing, JWT access tokens and the role -> permission model.

Token lifecycle (documented in SECURITY.md / ADR-007):
  * access token: JWT (HS256), 15 min default, carried in the Authorization header, kept in memory by the SPA.
  * refresh token: 256-bit random opaque value, stored server-side only as SHA-256, sent as an HttpOnly,
    SameSite=Strict cookie scoped to /api/v1/auth. Rotated on every use; re-use of a rotated token revokes
    the whole token family (theft detection). Logout revokes the family.
"""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from enum import Enum

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.db.models import Role

_hasher = PasswordHasher()  # argon2id with library-recommended parameters, per-hash random salt
_DUMMY_HASH = _hasher.hash("timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_policy(password: str) -> None:
    if len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    classes = sum(bool(any(f(c) for c in password)) for f in (str.islower, str.isupper, str.isdigit))
    if classes < 2 and len(password) < 16:
        raise ValueError("password must mix character classes or be at least 16 characters")


def create_access_token(*, user_id: int, username: str, role: Role, secret: str, ttl_minutes: int,
                        algorithm: str = "HS256") -> tuple[str, int]:
    now = datetime.now(UTC)
    exp = now + timedelta(minutes=ttl_minutes)
    claims = {"sub": str(user_id), "usr": username, "role": role.value, "typ": "access",
              "iat": int(now.timestamp()), "nbf": int(now.timestamp()), "exp": int(exp.timestamp()),
              "jti": uuid.uuid4().hex, "iss": "faultlens"}
    return jwt.encode(claims, secret, algorithm=algorithm), ttl_minutes * 60


class TokenError(Exception):
    pass


def decode_access_token(token: str, secret: str, algorithm: str = "HS256") -> dict:
    try:
        claims = jwt.decode(token, secret, algorithms=[algorithm], issuer="faultlens",
                            options={"require": ["exp", "iat", "sub", "role", "typ"]})
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if claims.get("typ") != "access":
        raise TokenError("wrong token type")
    return claims


def new_refresh_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class Permission(str, Enum):
    READ = "read"                           # dashboards, telemetry, incidents, RCA
    INCIDENT_WRITE = "incident:write"       # create / acknowledge / investigate / resolve
    INVESTIGATION_RUN = "investigation:run"
    FAULT_INJECT = "fault:inject"           # local demo only (also gated by environment)
    SETTINGS_WRITE = "settings:write"
    USERS_MANAGE = "users:manage"
    AUDIT_READ = "audit:read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.READ}),
    Role.SRE: frozenset({Permission.READ, Permission.INCIDENT_WRITE, Permission.INVESTIGATION_RUN, Permission.FAULT_INJECT}),
    Role.ADMIN: frozenset(Permission),
}


def has_permission(role: Role | str, perm: Permission) -> bool:
    try:
        return perm in ROLE_PERMISSIONS[Role(role)]
    except ValueError:
        return False  # unknown role => deny
