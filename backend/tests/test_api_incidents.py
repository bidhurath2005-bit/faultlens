from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.db.models import Anomaly, AuditEvent, Service, Severity
from app.db.session import sessionmaker
from app.services.incidents import correlate_anomaly

from .conftest import auth

pytestmark = pytest.mark.db


async def make(client, tokens, **kw):  # noqa: ANN001, ANN003, ANN201
    body = {"title": "Checkout degraded", "severity": "high", "services": ["payment-service"]} | kw
    r = await client.post("/api/v1/incidents", json=body, headers=auth(tokens["sre"]))
    assert r.status_code == 201, r.text
    return r.json()


async def test_full_lifecycle_timeline_audit_and_events(client, tokens, clean):
    inc = await make(client, tokens)
    h = auth(tokens["sre"])
    assert inc["status"] == "open"
    assert (await client.post(f"/api/v1/incidents/{inc['id']}/acknowledge", json={}, headers=h)).json()["status"] == "acknowledged"
    assert (await client.post(f"/api/v1/incidents/{inc['id']}/investigate", json={}, headers=h)).json()["status"] == "investigating"
    r = await client.post(f"/api/v1/incidents/{inc['id']}/resolve", json={"note": "pool size raised"}, headers=h)
    assert r.json()["status"] == "resolved" and r.json()["resolved_at"] and r.json()["resolution_note"] == "pool size raised"
    detail = (await client.get(f"/api/v1/incidents/{inc['id']}", headers=h)).json()
    kinds = [e["kind"] for e in detail["timeline"]]
    assert kinds == ["incident.opened", "status.acknowledged", "status.investigating", "status.resolved"]
    assert detail["timeline"][1]["actor"] == "sre"
    types = [e["type"] for _, e in clean.events]
    assert types == ["incident.created", "incident.acknowledged", "incident.investigating", "incident.resolved"]
    async with sessionmaker()() as s:
        actions = (await s.execute(select(AuditEvent.action).order_by(AuditEvent.id))).scalars().all()
    assert actions == ["incident.created", "incident.acknowledged", "incident.investigating", "incident.resolved"]


@pytest.mark.parametrize("path,body", [("resolve", {"note": "too early"}), ("investigate", {})])
async def test_invalid_transitions_rejected(client, tokens, path, body):
    inc = await make(client, tokens)
    r = await client.post(f"/api/v1/incidents/{inc['id']}/{path}", json=body, headers=auth(tokens["sre"]))
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"


async def test_resolved_is_terminal(client, tokens):
    inc = await make(client, tokens)
    h = auth(tokens["sre"])
    await client.post(f"/api/v1/incidents/{inc['id']}/acknowledge", json={}, headers=h)
    await client.post(f"/api/v1/incidents/{inc['id']}/resolve", json={"note": "done"}, headers=h)
    assert (await client.post(f"/api/v1/incidents/{inc['id']}/acknowledge", json={}, headers=h)).status_code == 409


async def test_validation_errors_are_structured(client, tokens):
    h = auth(tokens["sre"])
    r = await client.post("/api/v1/incidents", json={"title": "x", "severity": "apocalyptic", "services": []}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error" and r.json()["error"]["details"]
    r = await client.post("/api/v1/incidents", json={"title": "valid title", "severity": "low", "services": ["nope-service"]}, headers=h)
    assert r.status_code == 404
    inc = await make(client, tokens)
    await client.post(f"/api/v1/incidents/{inc['id']}/acknowledge", json={}, headers=h)
    assert (await client.post(f"/api/v1/incidents/{inc['id']}/resolve", json={"note": ""}, headers=h)).status_code == 422
    assert (await client.get("/api/v1/incidents/999999", headers=h)).status_code == 404


async def test_pagination_and_filters(client, tokens):
    for i in range(5):
        await make(client, tokens, title=f"incident number {i}", severity="low" if i % 2 else "critical")
    h = auth(tokens["viewer"])
    p = (await client.get("/api/v1/incidents", params={"limit": 2, "offset": 2}, headers=h)).json()
    assert p["total"] == 5 and len(p["items"]) == 2 and p["offset"] == 2
    crit = (await client.get("/api/v1/incidents", params={"severity": "critical"}, headers=h)).json()
    assert crit["total"] == 3 and {i["severity"] for i in crit["items"]} == {"critical"}
    assert (await client.get("/api/v1/incidents", params={"limit": 1000}, headers=h)).status_code == 422
    assert (await client.get("/api/v1/incidents", params={"service": "payment-service"}, headers=h)).json()["total"] == 5


async def test_anomaly_correlation_groups_related_services(clean):
    async with sessionmaker()() as s:
        svcs = {x.name: x for x in (await s.execute(select(Service))).scalars()}
        now = datetime.now(UTC)

        def anomaly(name, sev=Severity.MEDIUM):  # noqa: ANN001, ANN202
            a = Anomaly(service_id=svcs[name].id, detected_at=now, metric="latency_p95", method="zscore", score=9,
                        severity=sev, observed_value=2.0, baseline_value=0.05, details={})
            s.add(a)
            return a
        a1 = anomaly("payment-service")
        await s.flush()
        await s.refresh(a1, ["service"])
        inc1, created1 = await correlate_anomaly(s, a1, lambda a, b: {a, b} <= {"payment-service", "order-service"})
        a2 = anomaly("order-service", Severity.CRITICAL)
        await s.flush()
        await s.refresh(a2, ["service"])
        inc2, created2 = await correlate_anomaly(s, a2, lambda a, b: {a, b} <= {"payment-service", "order-service"})
        a3 = anomaly("notification-service")
        await s.flush()
        await s.refresh(a3, ["service"])
        inc3, created3 = await correlate_anomaly(s, a3, lambda a, b: False)
        await s.commit()
    assert created1 and not created2 and created3
    assert inc1.id == inc2.id != inc3.id
    assert inc2.severity == Severity.CRITICAL  # escalated by the more severe correlated anomaly
    assert {x.name for x in inc2.services} == {"payment-service", "order-service"}


async def test_rca_run_and_assistant_with_mocked_telemetry(client, tokens):
    inc = await make(client, tokens)
    h = auth(tokens["sre"])
    assert (await client.get(f"/api/v1/rca/{inc['id']}", headers=h)).status_code == 404
    r = await client.post(f"/api/v1/rca/{inc['id']}/run", headers=h)
    assert r.status_code == 200, r.text
    inv = r.json()
    assert inv["status"] in {"completed", "inconclusive"}
    assert set(inv["evidence"]["sources"]) == {"metrics", "logs", "traces", "dependencies"}
    assert inv["evidence"]["traces"]["error_origin_share"] == {"payment-service": 1.0}  # from the canned trace
    assert (await client.get(f"/api/v1/rca/investigations/{inv['id']}", headers=h)).json()["id"] == inv["id"]
    a = await client.post(f"/api/v1/assistant/incidents/{inc['id']}", json={"question": "what do the logs show?"}, headers=auth(tokens["viewer"]))
    assert a.status_code == 200 and a.json()["provider"] == "deterministic-template" and a.json()["validated"]
