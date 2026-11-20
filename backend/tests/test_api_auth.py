import pytest

from .conftest import PASSWORDS, auth

pytestmark = pytest.mark.db


async def login(client, role="admin"):  # noqa: ANN001, ANN201
    r = await client.post("/api/v1/auth/login", json={"username": role, "password": PASSWORDS[role]})
    assert r.status_code == 200, r.text
    return r


async def test_login_success_sets_httponly_refresh_cookie(client):
    r = await login(client)
    body = r.json()
    assert body["user"]["role"] == "admin" and body["token_type"] == "bearer" and body["expires_in"] == 900
    cookie = r.headers["set-cookie"]
    assert "fl_refresh=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1/auth" in cookie
    me = await client.get("/api/v1/auth/me", headers=auth(body["access_token"]))
    assert me.json()["username"] == "admin"


async def test_login_failure_is_generic_and_audited(client, tokens):
    r1 = await client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong-password-x"})
    r2 = await client.post("/api/v1/auth/login", json={"username": "ghost", "password": "wrong-password-x"})
    assert r1.status_code == r2.status_code == 401
    assert r1.json()["error"]["message"] == r2.json()["error"]["message"]  # no user enumeration
    audit = await client.get("/api/v1/audit", params={"action": "auth.login"}, headers=auth(tokens["admin"]))
    assert {a["outcome"] for a in audit.json()["items"]} == {"failure"}
    assert "wrong-password-x" not in audit.text


async def test_refresh_rotates_and_detects_reuse(client):
    r = await login(client)
    old = client.cookies.get("fl_refresh")
    r2 = await client.post("/api/v1/auth/refresh")
    assert r2.status_code == 200 and client.cookies.get("fl_refresh") != old
    # Replaying the rotated token => theft assumed: whole family revoked.
    client.cookies.set("fl_refresh", old, path="/api/v1/auth")
    r3 = await client.post("/api/v1/auth/refresh")
    assert r3.status_code == 401 and "reuse" in r3.json()["error"]["message"]
    client.cookies.clear()
    assert r.status_code == 200


async def test_logout_revokes_refresh(client):
    await login(client)
    tok = client.cookies.get("fl_refresh")
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    client.cookies.set("fl_refresh", tok, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401


async def test_missing_invalid_and_tampered_tokens(client, tokens):
    assert (await client.get("/api/v1/incidents")).status_code == 401
    assert (await client.get("/api/v1/incidents", headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.get("/api/v1/incidents", headers=auth(tokens["viewer"][:-2] + "xx"))).status_code == 401
    assert (await client.get("/api/v1/incidents", headers={"Authorization": "Basic Zm9vOmJhcg=="})).status_code == 401


async def test_deactivated_user_token_rejected_immediately(client, tokens):
    users = (await client.get("/api/v1/users", headers=auth(tokens["admin"]))).json()["items"]
    viewer = next(u for u in users if u["username"] == "viewer")
    assert (await client.patch(f"/api/v1/users/{viewer['id']}", json={"is_active": False}, headers=auth(tokens["admin"]))).status_code == 200
    assert (await client.get("/api/v1/incidents", headers=auth(tokens["viewer"]))).status_code == 401


async def test_role_change_invalidates_existing_token(client, tokens):
    users = (await client.get("/api/v1/users", headers=auth(tokens["admin"]))).json()["items"]
    sre = next(u for u in users if u["username"] == "sre")
    await client.patch(f"/api/v1/users/{sre['id']}", json={"role": "viewer"}, headers=auth(tokens["admin"]))
    r = await client.get("/api/v1/incidents", headers=auth(tokens["sre"]))
    assert r.status_code == 401 and "role changed" in r.json()["error"]["message"]
    await client.patch(f"/api/v1/users/{sre['id']}", json={"role": "sre"}, headers=auth(tokens["admin"]))


async def test_login_rate_limited(client):
    # Fixed 60 s window, 10 attempts/IP: within 21 attempts at most one window rollover can occur, so a 429 must appear.
    codes = []
    for _ in range(21):
        codes.append((await client.post("/api/v1/auth/login", json={"username": "x" * 5, "password": "y"})).status_code)
        if codes[-1] == 429:
            break
    assert codes[-1] == 429 and set(codes[:-1]) == {401} and len(codes) >= 11


async def test_password_change_policy_and_session_revocation(client):
    r = await login(client, "sre")
    tok = r.json()["access_token"]
    weak = await client.post("/api/v1/auth/password", json={"current_password": PASSWORDS["sre"], "new_password": "aaaaaaaaaaaa"}, headers=auth(tok))
    assert weak.status_code == 422
    ok = await client.post("/api/v1/auth/password", json={"current_password": PASSWORDS["sre"], "new_password": "New-Passw0rd-999"}, headers=auth(tok))
    assert ok.status_code == 204
    assert (await client.post("/api/v1/auth/refresh")).status_code == 401  # refresh tokens revoked
    await client.post("/api/v1/auth/password", json={"current_password": "New-Passw0rd-999", "new_password": PASSWORDS["sre"]}, headers=auth(tok))


async def test_admin_cannot_demote_self(client, tokens):
    users = (await client.get("/api/v1/users", headers=auth(tokens["admin"]))).json()["items"]
    me = next(u for u in users if u["username"] == "admin")
    assert (await client.patch(f"/api/v1/users/{me['id']}", json={"role": "viewer"}, headers=auth(tokens["admin"]))).status_code == 409


async def test_user_creation_validation(client, tokens):
    h = auth(tokens["admin"])
    assert (await client.post("/api/v1/users", json={"username": "bob", "password": "short", "role": "sre"}, headers=h)).status_code == 422
    assert (await client.post("/api/v1/users", json={"username": "bad name!", "password": "Long-Enough-123", "role": "sre"}, headers=h)).status_code == 422
    r = await client.post("/api/v1/users", json={"username": "bob", "password": "Long-Enough-123", "role": "sre"}, headers=h)
    assert r.status_code == 201 and "password" not in r.text
    assert (await client.post("/api/v1/users", json={"username": "bob", "password": "Long-Enough-123", "role": "sre"}, headers=h)).status_code == 409
