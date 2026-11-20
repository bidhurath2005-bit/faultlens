import pytest

from .conftest import TRACE_ID, auth

pytestmark = pytest.mark.db


async def test_health_reports_dependencies(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["checks"] == {"postgres": True, "redis": True, "prometheus": True, "loki": True, "tempo": True}
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"


async def test_services_overview_and_dependencies(client, tokens):
    h = auth(tokens["viewer"])
    svcs = (await client.get("/api/v1/services", headers=h)).json()["items"]
    assert len(svcs) == 5
    pay = next(s for s in svcs if s["name"] == "payment-service")
    # canned Prometheus returns 0.05 for every feature, including availability => classified "down"
    assert pay["dependents"] == ["order-service"] and pay["health"] == "down"
    ov = (await client.get("/api/v1/metrics/overview", params={"minutes": 30}, headers=h)).json()
    assert ov["services_total"] == 5 and "latency_p95" in ov["current"]
    g = (await client.get("/api/v1/dependencies", headers=h)).json()
    assert {(e["source"], e["target"]) for e in g["edges"]} == {("order-service", "payment-service"), ("traffic-generator", "order-service")}
    assert (await client.get("/api/v1/dependencies", params={"window": "7d"}, headers=h)).status_code == 422


async def test_trace_lookup_and_validation(client, tokens):
    h = auth(tokens["viewer"])
    t = (await client.get(f"/api/v1/traces/{TRACE_ID}", headers=h)).json()
    assert {s["service"] for s in t["spans"]} == {"order-service", "payment-service"}
    assert t["service_breakdown"]["payment-service"]["errors"] == 3
    assert (await client.get("/api/v1/traces/" + "0" * 32, headers=h)).status_code == 404
    assert (await client.get("/api/v1/traces/not-a-trace", headers=h)).status_code == 422
    lst = (await client.get("/api/v1/traces", params={"service": "payment-service", "status": "error", "min_duration_ms": 500}, headers=h)).json()
    assert lst["query"] == '{ resource.service.name = "payment-service" && status = error && duration > 500ms }'
    assert lst["items"][0]["trace_id"] == TRACE_ID


async def test_logs_query_building_and_trace_context(client, tokens):
    h = auth(tokens["viewer"])
    r = (await client.get("/api/v1/logs", params={"service": "payment-service", "level": "ERROR,WARNING", "trace_id": TRACE_ID,
                                                  "q": 'pool "x"'}, headers=h)).json()
    assert r["query"] == ('{service_name="payment-service"} | severity_text=~"ERROR|WARN|WARNING" | trace_id="' + TRACE_ID +
                          '" |= "pool \\"x\\""')
    with_ctx = [x for x in r["items"] if x["trace_id"]]
    without = [x for x in r["items"] if not x["trace_id"]]
    assert with_ctx[0]["trace_id"] == TRACE_ID and with_ctx[0]["span_id"] == "d" * 16
    assert without and without[0]["span_id"] is None  # missing context is reported as missing, never invented


@pytest.mark.parametrize("params", [{"service": 'x"} or {job=~".+'}, {"level": "ERROR|.*"}, {"trace_id": "zz"},
                                    {"minutes": 100000}, {"limit": 5000}])
async def test_logs_injection_and_bounds_rejected(client, tokens, params):
    r = await client.get("/api/v1/logs", params=params, headers=auth(tokens["viewer"]))
    assert r.status_code == 422


async def test_service_name_injection_rejected(client, tokens):
    r = await client.get('/api/v1/services/payment"}', headers=auth(tokens["viewer"]))
    assert r.status_code in (404, 422)
    r = await client.get("/api/v1/traces", params={"service": 'a" || true'}, headers=auth(tokens["viewer"]))
    assert r.status_code == 422


async def test_settings_validation_and_audit(client, tokens):
    h = auth(tokens["admin"])
    bad = await client.put("/api/v1/settings", json={"detection": {"zscore_threshold": 0.1}}, headers=h)
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "invalid_setting"
    assert (await client.put("/api/v1/settings", json={"detection": {"enabled_methods": ["magic"]}}, headers=h)).status_code == 422
    ok = await client.put("/api/v1/settings", json={"detection": {"zscore_threshold": 5.0, "thresholds": {"error_rate": 0.1}}}, headers=h)
    assert ok.status_code == 200
    s = (await client.get("/api/v1/settings", headers=auth(tokens["viewer"]))).json()
    assert s["detection"]["zscore_threshold"] == 5.0 and s["detection"]["thresholds"]["error_rate"] == 0.1
    assert s["detection"]["thresholds"]["latency_p95_s"] == 1.0  # untouched keys preserved by deep merge
    assert "api_key" not in str(s["ai"]).replace("api_key_configured", "")


async def test_fault_injection_validation_and_limits(client, tokens, monkeypatch):
    import app.api.v1.faults as faults
    calls = []

    async def fake_call(settings, method, url, json=None):  # noqa: ANN001, ANN202
        calls.append((method, url, json))
        return {"fault": {"type": json["type"] if json else None}}
    monkeypatch.setattr(faults, "_call", fake_call)
    h = auth(tokens["sre"])
    for bad in ({"target": "payment-service", "type": "latency", "latency_ms": 60000},
                {"target": "payment-service", "type": "latency", "duration_s": 3600},
                {"target": "payment-service", "type": "rm -rf"},
                {"target": "../../etc", "type": "latency"}):
        assert (await client.post("/api/v1/fault-injection", json=bad, headers=h)).status_code == 422
    assert (await client.post("/api/v1/fault-injection", json={"target": "user-service", "type": "latency"}, headers=h)).status_code == 422
    r = await client.post("/api/v1/fault-injection", json={"target": "payment-service", "type": "latency", "latency_ms": 500, "duration_s": 60}, headers=h)
    assert r.status_code == 201
    assert calls[-1][1] == "http://payment-service:8000/admin/faults" and calls[-1][2]["latency_ms"] == 500
    hist = (await client.get("/api/v1/fault-injection/history", headers=h)).json()["items"]
    assert hist[0]["type"] == "latency"


async def test_fault_injection_disabled_outside_local(client, tokens, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "environment", "production")
    r = await client.post("/api/v1/fault-injection", json={"target": "payment-service", "type": "latency"}, headers=auth(tokens["admin"]))
    assert r.status_code == 403 and r.json()["error"]["code"] == "fault_injection_disabled"
