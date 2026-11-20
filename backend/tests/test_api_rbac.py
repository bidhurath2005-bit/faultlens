"""Allowed/denied matrix for every protected operation x role (backend-enforced)."""
import pytest

from .conftest import auth

pytestmark = pytest.mark.db

# (method, path, json, minimum role)
OPS = [
    ("GET", "/api/v1/services", None, "viewer"),
    ("GET", "/api/v1/metrics/overview", None, "viewer"),
    ("GET", "/api/v1/incidents", None, "viewer"),
    ("GET", "/api/v1/anomalies", None, "viewer"),
    ("GET", "/api/v1/logs", None, "viewer"),
    ("GET", "/api/v1/traces", None, "viewer"),
    ("GET", "/api/v1/dependencies", None, "viewer"),
    ("GET", "/api/v1/settings", None, "viewer"),
    ("GET", "/api/v1/fault-injection/scenarios", None, "viewer"),
    ("POST", "/api/v1/incidents", {"title": "manual", "severity": "low", "services": ["payment-service"]}, "sre"),
    ("POST", "/api/v1/fault-injection/reset", None, "sre"),
    ("PUT", "/api/v1/settings", {"preferences": {"refresh_interval_s": 15}}, "admin"),
    ("GET", "/api/v1/users", None, "admin"),
    ("POST", "/api/v1/users", {"username": "newuser", "password": "Long-Enough-123", "role": "viewer"}, "admin"),
    ("GET", "/api/v1/audit", None, "admin"),
]
RANK = {"viewer": 0, "sre": 1, "admin": 2}


@pytest.mark.parametrize("method,path,body,minimum", OPS)
async def test_matrix(client, tokens, method, path, body, minimum, monkeypatch):
    # Fault reset would call real services; stub the outbound call (authorisation is what is under test).
    import app.api.v1.faults as faults

    async def fake_call(*a, **k):  # noqa: ANN002, ANN003, ANN202
        return {"removed": 0}
    monkeypatch.setattr(faults, "_call", fake_call)
    unauth = await client.request(method, path, json=body)
    assert unauth.status_code == 401
    for role, rank in RANK.items():
        r = await client.request(method, path, json=body, headers=auth(tokens[role]))
        if rank >= RANK[minimum]:
            assert r.status_code < 400 or r.status_code == 409, (role, path, r.status_code, r.text)
        else:
            assert r.status_code == 403, (role, path, r.status_code)
            assert r.json()["error"]["code"] == "forbidden"


async def test_viewer_cannot_change_incident_state_or_inject(client, tokens):
    r = await client.post("/api/v1/incidents", json={"title": "x incident", "severity": "low", "services": ["payment-service"]},
                          headers=auth(tokens["sre"]))
    iid = r.json()["id"]
    for action, body in (("acknowledge", {}), ("investigate", {}), ("resolve", {"note": "done"})):
        assert (await client.post(f"/api/v1/incidents/{iid}/{action}", json=body, headers=auth(tokens["viewer"]))).status_code == 403
    assert (await client.post("/api/v1/fault-injection", json={"target": "payment-service", "type": "latency"},
                              headers=auth(tokens["viewer"]))).status_code == 403
    assert (await client.post(f"/api/v1/rca/{iid}/run", headers=auth(tokens["viewer"]))).status_code == 403
    # but can read the results
    assert (await client.get(f"/api/v1/incidents/{iid}", headers=auth(tokens["viewer"]))).status_code == 200


async def test_sre_cannot_manage_users_or_settings(client, tokens):
    h = auth(tokens["sre"])
    assert (await client.patch("/api/v1/users/1", json={"role": "admin"}, headers=h)).status_code == 403
    assert (await client.put("/api/v1/settings", json={"detection": {"zscore_threshold": 5}}, headers=h)).status_code == 403
