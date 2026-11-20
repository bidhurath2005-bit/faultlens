"""Unit tests for the shared service runtime (no database or collector required)."""
from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
import pytest
from fastapi import APIRouter
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

os.environ.setdefault("OTEL_ENABLED", "false")

from faultlens_common.app import create_service_app  # noqa: E402
from faultlens_common.config import ServiceSettings  # noqa: E402
from faultlens_common.faults import FaultManager, FaultRequest  # noqa: E402
from faultlens_common.logging import JsonFormatter, redact, request_id_var  # noqa: E402

TOKEN = "x" * 32


def make_app(**overrides):
    s = ServiceSettings(service_name="test-service", otel_enabled=False, fault_injection_enabled=True,
                        fault_admin_token=TOKEN, environment="local", **overrides)
    r = APIRouter()

    @r.get("/work")
    async def work() -> dict:
        logging.getLogger("t").info("doing work")
        return {"ok": True}

    @r.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("kaboom")

    return create_service_app(s, [r], with_db=False)


@pytest.fixture
async def client():
    app = make_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://t") as c:
            yield c


def test_json_formatter_includes_trace_context_only_when_valid():
    trace.set_tracer_provider(TracerProvider())
    fmt = JsonFormatter("svc")
    rec = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    out = json.loads(fmt.format(rec))
    assert "trace_id" not in out and out["service"] == "svc" and out["level"] == "INFO"
    with trace.get_tracer("t").start_as_current_span("s") as span:
        out = json.loads(fmt.format(rec))
        ctx = span.get_span_context()
        assert out["trace_id"] == format(ctx.trace_id, "032x")
        assert out["span_id"] == format(ctx.span_id, "016x")


def test_request_id_and_redaction():
    token = request_id_var.set("abc123")
    try:
        rec = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
        rec.password = "hunter2"
        rec.payload = {"card_number": "4111", "amount": 5}
        out = json.loads(JsonFormatter("svc").format(rec))
    finally:
        request_id_var.reset(token)
    assert out["request_id"] == "abc123"
    assert out["password"] == "[REDACTED]"
    assert out["payload"] == {"card_number": "[REDACTED]", "amount": 5}
    assert redact({"Authorization": "Bearer x"}) == {"Authorization": "[REDACTED]"}


def test_fault_request_limits():
    with pytest.raises(ValueError):
        FaultRequest(type="latency", latency_ms=60_000)
    with pytest.raises(ValueError):
        FaultRequest(type="memory_pressure", memory_mb=4096)
    with pytest.raises(ValueError):
        FaultRequest(type="latency", duration_s=86_400)
    with pytest.raises(ValueError):
        FaultRequest(type="rm_rf")  # type: ignore[arg-type]


async def test_fault_manager_inject_expire_and_clear():
    fm = FaultManager("svc")
    await fm.inject(FaultRequest(type="error_500", error_rate=1.0, duration_s=10))
    assert await fm.apply_request_faults() == 500
    fm.active["error_500"].expires_at = 0  # simulate expiry
    assert fm.get("error_500") is None
    assert await fm.apply_request_faults() is None
    await fm.inject(FaultRequest(type="unavailable", duration_s=10))
    assert await fm.apply_request_faults() == 503
    assert await fm.clear() >= 1
    assert fm.snapshot() == []


async def test_cpu_burn_is_reversible():
    fm = FaultManager("svc")
    await fm.inject(FaultRequest(type="cpu_burn", cpu_threads=1, duration_s=10))
    stop = fm.active["cpu_burn"].handles["stop"]
    assert not stop.is_set()
    await fm.remove("cpu_burn")
    assert stop.is_set()


async def test_health_and_request_id_header(client):
    r = await client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    r = await client.get("/work", headers={"x-request-id": "req42"})
    assert r.headers["x-request-id"] == "req42"
    r = await client.get("/work", headers={"x-request-id": "bad id\nwith newline"})
    assert r.headers["x-request-id"] != "bad id\nwith newline"


async def test_unhandled_error_returns_500_json(client):
    r = await client.get("/boom")
    assert r.status_code == 500 and r.json()["detail"] == "internal server error"


async def test_fault_admin_requires_token(client):
    r = await client.post("/admin/faults", json={"type": "error_500"})
    assert r.status_code == 401
    r = await client.post("/admin/faults", json={"type": "error_500", "error_rate": 1.0}, headers={"x-fault-token": TOKEN})
    assert r.status_code == 201, r.text
    assert (await client.get("/work")).status_code == 500
    assert (await client.delete("/admin/faults", headers={"x-fault-token": TOKEN})).json()["removed"] == 1
    assert (await client.get("/work")).status_code == 200


async def test_unavailable_fault_fails_health(client):
    await client.post("/admin/faults", json={"type": "unavailable"}, headers={"x-fault-token": TOKEN})
    assert (await client.get("/health")).status_code == 503
    assert (await client.get("/work")).status_code == 503
    await client.delete("/admin/faults", headers={"x-fault-token": TOKEN})
    assert (await client.get("/health")).status_code == 200


async def test_latency_fault_delays_requests(client):
    await client.post("/admin/faults", json={"type": "latency", "latency_ms": 300, "jitter_ms": 0},
                      headers={"x-fault-token": TOKEN})
    t0 = asyncio.get_running_loop().time()
    await client.get("/work")
    assert asyncio.get_running_loop().time() - t0 >= 0.29
    await client.delete("/admin/faults", headers={"x-fault-token": TOKEN})


async def test_pool_exhaustion_rejected_without_db(client):
    r = await client.post("/admin/faults", json={"type": "pool_exhaustion"}, headers={"x-fault-token": TOKEN})
    assert r.status_code == 422


async def test_fault_injection_disabled_outside_local():
    s = ServiceSettings(service_name="t", otel_enabled=False, fault_injection_enabled=True, fault_admin_token=TOKEN,
                        environment="production")
    assert not s.fault_injection_allowed
    app = create_service_app(s, [], with_db=False)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/admin/faults", json={"type": "error_500"}, headers={"x-fault-token": TOKEN})
    assert r.status_code == 403
