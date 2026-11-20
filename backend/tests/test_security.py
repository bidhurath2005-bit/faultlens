import time

import jwt
import pytest

from app.core.security import (
    Permission,
    TokenError,
    create_access_token,
    decode_access_token,
    has_permission,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    validate_password_policy,
    verify_password,
)
from app.db.models import Role

SECRET = "s" * 40


def test_password_hash_is_salted_argon2_and_verifies():
    h1, h2 = hash_password("correct horse battery"), hash_password("correct horse battery")
    assert h1 != h2 and h1.startswith("$argon2id$")
    assert verify_password("correct horse battery", h1)
    assert not verify_password("wrong", h1)
    assert not verify_password("anything", None)  # unknown user path still hashes, returns False
    assert not verify_password("x", "not-a-hash")


def test_password_policy():
    with pytest.raises(ValueError):
        validate_password_policy("short")
    with pytest.raises(ValueError):
        validate_password_policy("alllowercaseletters"[:13])
    validate_password_policy("Mixed-Case-123")
    validate_password_policy("a very long passphrase only lowercase")


def test_access_token_roundtrip_and_claims():
    tok, ttl = create_access_token(user_id=7, username="alice", role=Role.SRE, secret=SECRET, ttl_minutes=5)
    assert ttl == 300
    c = decode_access_token(tok, SECRET)
    assert c["sub"] == "7" and c["role"] == "sre" and c["typ"] == "access" and c["iss"] == "faultlens"


def test_token_rejections():
    tok, _ = create_access_token(user_id=1, username="a", role=Role.ADMIN, secret=SECRET, ttl_minutes=5)
    with pytest.raises(TokenError):
        decode_access_token(tok, "y" * 40)  # wrong key
    with pytest.raises(TokenError):
        decode_access_token(tok[:-3] + "abc", SECRET)  # tampered
    expired = jwt.encode({"sub": "1", "role": "admin", "typ": "access", "iss": "faultlens", "iat": 1, "exp": int(time.time()) - 10},
                         SECRET, algorithm="HS256")
    with pytest.raises(TokenError):
        decode_access_token(expired, SECRET)
    wrong_type = jwt.encode({"sub": "1", "role": "admin", "typ": "refresh", "iss": "faultlens", "iat": 1,
                             "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    with pytest.raises(TokenError):
        decode_access_token(wrong_type, SECRET)
    none_alg = jwt.encode({"sub": "1", "role": "admin", "typ": "access", "iss": "faultlens", "iat": 1,
                           "exp": int(time.time()) + 60}, None, algorithm="none")
    with pytest.raises(TokenError):
        decode_access_token(none_alg, SECRET)


def test_refresh_tokens_are_random_and_only_hash_stored():
    raw1, h1 = new_refresh_token()
    raw2, _ = new_refresh_token()
    assert raw1 != raw2 and len(raw1) >= 40
    assert h1 == hash_refresh_token(raw1) and raw1 not in h1


@pytest.mark.parametrize("role,perm,allowed", [
    (Role.VIEWER, Permission.READ, True),
    (Role.VIEWER, Permission.INCIDENT_WRITE, False),
    (Role.VIEWER, Permission.FAULT_INJECT, False),
    (Role.VIEWER, Permission.USERS_MANAGE, False),
    (Role.SRE, Permission.INCIDENT_WRITE, True),
    (Role.SRE, Permission.FAULT_INJECT, True),
    (Role.SRE, Permission.SETTINGS_WRITE, False),
    (Role.SRE, Permission.USERS_MANAGE, False),
    (Role.SRE, Permission.AUDIT_READ, False),
    (Role.ADMIN, Permission.USERS_MANAGE, True),
    (Role.ADMIN, Permission.SETTINGS_WRITE, True),
])
def test_permission_matrix(role, perm, allowed):
    assert has_permission(role, perm) is allowed


def test_unknown_role_denied():
    assert not has_permission("superuser", Permission.READ)
