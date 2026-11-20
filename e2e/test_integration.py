"""Integration tests against the RUNNING docker compose stack (docker compose up -d first).

    python -m pytest e2e/test_integration.py -v

Verifies real telemetry, not mocks: W3C trace propagation across services, trace<->log correlation through
Loki and Tempo, Prometheus metrics, Kafka->Redis->WebSocket event delivery and database connectivity.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time

import httpx
import pytest
import websockets

from client import admin, ensure_user, wait_for

ORDER = "http://localhost:8100"
TEMPO = "http://localhost:3200"
LOKI = "http://localhost:3100"
PROM = "http://localhost:9090"
WS = "ws://localhost:8000/api/v1/ws/events"
SERVICES = {"order-service", "user-service", "payment-service", "inventory-service", "notification-service"}


@pytest.fixture(scope="module")
def api():  # noqa: ANN201
    return admin()


def place_order(traceparent: str | None = None) -> httpx.Response:
    headers = {"traceparent": traceparent} if traceparent else {}
    body = {"user_id": 5, "items": [{"product_id": 3, "quantity": 1}], "payment_method": "tok_demo_visa"}
    return httpx.post(f"{ORDER}/orders", json=body, headers=headers, timeout=15)


def tempo_trace(trace_id: str) -> dict | None:
    r = httpx.get(f"{TEMPO}/api/traces/{trace_id}", timeout=10)
    return r.json() if r.status_code == 200 else None


def spans_of(trace: dict) -> list[tuple[str, dict]]:
    out = []
    for rs in trace.get("batches", []):
        svc = next(a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name")
        for ss in rs.get("scopeSpans", []):
            out += [(svc, s) for s in ss["spans"]]
    return out


def b64hex(v: str) -> str:
    import base64
    return base64.b64decode(v).hex()


def test_platform_health(api):  # noqa: ANN001
    h = httpx.get("http://localhost:8000/api/v1/health").json()
    assert h["checks"] == {"postgres": True, "redis": True, "prometheus": True, "loki": True, "tempo": True}


def test_trace_context_propagates_across_all_five_services():
    trace_id = secrets.token_hex(16)
    parent = secrets.token_hex(8)
    r = place_order(f"00-{trace_id}-{parent}-01")
    assert r.status_code == 201, r.text
    trace = wait_for(lambda: (t := tempo_trace(trace_id)) and len({s for s, _ in spans_of(t)} & SERVICES) == 5 and t, 60, 3,
                     "trace with all five services in Tempo")
    spans = spans_of(trace)
    assert {s for s, _ in spans} >= SERVICES
    # The order-service server span is a child of the caller's span id we injected.
    root = next(sp for svc, sp in spans if svc == "order-service" and sp["kind"] == "SPAN_KIND_SERVER")
    assert b64hex(root["parentSpanId"]) == parent
    # Every downstream server span's parent is an order-service client span (context crossed the HTTP hop).
    client_ids = {b64hex(sp["spanId"]) for svc, sp in spans if svc == "order-service" and sp["kind"] == "SPAN_KIND_CLIENT"}
    for svc, sp in spans:
        if svc in SERVICES - {"order-service"} and sp["kind"] == "SPAN_KIND_SERVER":
            assert b64hex(sp["parentSpanId"]) in client_ids


def test_logs_carry_matching_trace_and_span_ids():
    trace_id = secrets.token_hex(16)
    assert place_order(f"00-{trace_id}-{secrets.token_hex(8)}-01").status_code == 201
    trace = wait_for(lambda: (t := tempo_trace(trace_id)) and len({s for s, _ in spans_of(t)} & SERVICES) == 5 and t, 60, 3, "trace")
    span_ids = {b64hex(sp["spanId"]) for _, sp in spans_of(trace)}

    def logs():  # noqa: ANN202
        q = f'{{service_namespace="faultlens-demo"}} | trace_id="{trace_id}"'
        end = time.time_ns()
        r = httpx.get(f"{LOKI}/loki/api/v1/query_range", params={"query": q, "start": end - 600 * 10**9, "end": end, "limit": 200})
        streams = r.json()["data"]["result"]
        return streams if len({s["stream"]["service_name"] for s in streams} & SERVICES) == 5 else None
    streams = wait_for(logs, 60, 3, "correlated logs in Loki")
    for s in streams:
        assert s["stream"]["trace_id"] == trace_id
        assert s["stream"]["span_id"] in span_ids  # each log line points at a real span of that trace


def test_trace_to_log_navigation_through_faultlens_api(api):  # noqa: ANN001
    trace_id = secrets.token_hex(16)
    place_order(f"00-{trace_id}-{secrets.token_hex(8)}-01")
    t = wait_for(lambda: (r := api.get(f"/traces/{trace_id}")).status_code == 200 and len(r.json()["service_breakdown"]) >= 5 and r.json(),
                 60, 3, "trace via API")
    logs = wait_for(lambda: (r := api.get("/logs", params={"trace_id": trace_id, "level": "INFO,WARNING,ERROR", "minutes": 30}).json())
                    ["items"] and r, 60, 3, "logs via API")
    span_ids = {s["span_id"] for s in t["spans"]}
    assert all(item["trace_id"] == trace_id and item["span_id"] in span_ids for item in logs["items"])


def test_malformed_or_missing_trace_context_starts_new_trace():
    for tp in ("00-zzzz-bad-01", "garbage", None):
        r = place_order(tp)
        assert r.status_code == 201
    bad = "0" * 32
    assert httpx.get(f"{TEMPO}/api/traces/{bad}").status_code in (400, 404)  # never adopted an all-zero trace id


def test_prometheus_has_golden_signals_for_every_service():
    def q(expr: str) -> set[str]:
        return {r["metric"]["service_name"] for r in httpx.get(f"{PROM}/api/v1/query", params={"query": expr}).json()["data"]["result"]}
    assert q("sum by (service_name) (rate(faultlens_http_server_requests_total[2m]))") >= SERVICES
    assert q("max by (service_name) (faultlens_process_cpu_utilization_ratio)") >= SERVICES
    assert q("max by (service_name) (faultlens_db_pool_connections_ratio)") >= SERVICES
    assert q("max by (service_name) (faultlens_probe_up)") >= SERVICES
    edges = httpx.get(f"{PROM}/api/v1/query", params={"query": "sum by (client, server) (rate(traces_service_graph_request_total[5m]))"}).json()
    pairs = {(r["metric"]["client"], r["metric"]["server"]) for r in edges["data"]["result"]}
    assert {("order-service", s) for s in SERVICES - {"order-service"}} <= pairs


def test_kafka_to_websocket_event_delivery(api):  # noqa: ANN001
    """API -> Kafka (faultlens.incidents) -> worker bridge consumer -> Redis Pub/Sub -> API WebSocket hub -> client."""
    sre = ensure_user(api, "it-sre", "sre", "It-Sre-Passw0rd-1")

    async def run() -> dict:
        async with websockets.connect(WS, origin="http://localhost:3080") as ws:
            await ws.send(json.dumps({"type": "auth", "token": sre.token}))
            assert json.loads(await ws.recv())["type"] == "auth_ok"
            title = f"integration-{secrets.token_hex(3)}"
            await asyncio.to_thread(lambda: sre.post("/incidents", json={"title": title, "severity": "low", "services": ["user-service"]}).raise_for_status())
            deadline = time.time() + 20
            while time.time() < deadline:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                if msg["type"] == "event" and msg["event"]["type"] == "incident.created" and msg["event"]["data"]["incident"]["title"] == title:
                    return msg["event"]
            raise AssertionError("event not delivered")
    ev = asyncio.run(run())
    assert ev["source"] == "api" and ev["data"]["incident"]["services"] == ["user-service"]
    iid = ev["data"]["incident"]["id"]
    sre.post(f"/incidents/{iid}/acknowledge", json={})
    sre.post(f"/incidents/{iid}/resolve", json={"note": "integration test cleanup"})


def test_unauthorised_websocket_rejected():
    async def run() -> int:
        async with websockets.connect(WS, origin="http://localhost:3080") as ws:
            await ws.send(json.dumps({"type": "auth", "token": "invalid"}))
            try:
                await asyncio.wait_for(ws.recv(), 5)
            except websockets.ConnectionClosed as e:
                return e.rcvd.code
        return 0
    assert asyncio.run(run()) == 4401
