"""Test fixtures.

Unit tests (engines, security, detectors) need nothing external. API/DB tests need PostgreSQL and Redis:
locally they use the compose services (faultlens_test database, Redis DB 15); in CI, service containers.
Telemetry backends are replaced by an httpx MockTransport with canned Prometheus/Loki/Tempo responses.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).resolve().parents[1]


def _dotenv(key: str, default: str = "") -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
    return default


os.environ.setdefault("JWT_SECRET", "test-secret-" + "x" * 40)
os.environ.setdefault(
    "DATABASE_URL",
    f"postgresql+asyncpg://faultlens:{_dotenv('FAULTLENS_DB_PASSWORD', 'faultlens')}@localhost:5432/faultlens_test")
os.environ.setdefault("REDIS_URL", f"redis://:{_dotenv('REDIS_PASSWORD')}@localhost:6379/15")
os.environ.setdefault("KAFKA_ENABLED", "false")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("FAULT_INJECTION_ENABLED", "true")
os.environ.setdefault("FAULT_ADMIN_TOKEN", "t" * 32)
os.environ.setdefault("SERVICE_URLS", json.dumps({"payment-service": "http://payment-service:8000"}))
os.environ.setdefault("BOOTSTRAP_ADMIN_PASSWORD", "")
os.environ.setdefault("LOG_LEVEL", "WARNING")

from app.core.config import get_settings  # noqa: E402
from app.core.redis import close_redis, get_redis, init_redis  # noqa: E402
from app.core.security import create_access_token, hash_password  # noqa: E402
from app.db.models import Role, User  # noqa: E402
from app.db.session import dispose_engine, init_engine, sessionmaker  # noqa: E402
from app.events.bus import InMemoryPublisher, set_publisher  # noqa: E402
from app.main import create_app  # noqa: E402
from app.telemetry.clients import LokiClient, PrometheusClient, Telemetry, TempoClient, set_telemetry  # noqa: E402

PASSWORDS = {"admin": "Admin-Passw0rd-123", "sre": "Sre-Passw0rd-12345", "viewer": "Viewer-Passw0rd-12"}


def pytest_collection_modifyitems(items):  # noqa: ANN001
    import pytest_asyncio as pa
    marker = pytest.mark.asyncio(loop_scope="session")
    for item in items:
        if pa.is_async_test(item):
            item.add_marker(marker, append=False)


# ---------------------------------------------------------------- canned telemetry
def _b64(hexid: str) -> str:
    return base64.b64encode(bytes.fromhex(hexid)).decode()


TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"


def canned_trace() -> dict:
    def span(sid, parent, name, kind, start_ms, dur_ms, status=None, attrs=None):  # noqa: ANN001, ANN202
        s = {"traceId": _b64(TRACE_ID), "spanId": _b64(sid), "name": name, "kind": f"SPAN_KIND_{kind}",
             "startTimeUnixNano": str(int(1_700_000_000_000 + start_ms) * 1_000_000),
             "endTimeUnixNano": str(int(1_700_000_000_000 + start_ms + dur_ms) * 1_000_000),
             "attributes": [{"key": k, "value": {"stringValue": v}} for k, v in (attrs or {}).items()]}
        if parent:
            s["parentSpanId"] = _b64(parent)
        if status:
            s["status"] = {"code": "STATUS_CODE_ERROR", "message": status}
        return s

    def rs(svc, spans):  # noqa: ANN001, ANN202
        return {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": svc}}]},
                "scopeSpans": [{"spans": spans}]}

    return {"batches": [
        rs("order-service", [span("a" * 16, None, "POST /orders", "SERVER", 0, 2100, "502"),
                             span("b" * 16, "a" * 16, "POST", "CLIENT", 10, 2050, "503", {"server.address": "payment-service"})]),
        rs("payment-service", [span("c" * 16, "b" * 16, "POST /payments", "SERVER", 15, 2040, "503"),
                               span("d" * 16, "c" * 16, "db.pool.acquire", "INTERNAL", 16, 2030, "pool timeout"),
                               span("e" * 16, "d" * 16, "connect", "CLIENT", 17, 2020, "QueuePool limit", {"db.system": "postgresql"})]),
    ]}


def telemetry_handler(request: httpx.Request) -> httpx.Response:
    p = request.url.path
    now = time.time()
    if p.endswith("/loki/api/v1/query_range"):
        stream = {"service_name": "payment-service", "severity_text": "ERROR", "trace_id": TRACE_ID, "span_id": "d" * 16,
                  "request_id": "r1", "pool_size": "5"}
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": stream, "values": [[str(int(now * 1e9)), "database connection pool exhausted: QueuePool limit reached"]]},
            {"stream": {"service_name": "order-service", "severity_text": "INFO"}, "values": [[str(int(now * 1e9) - 5), "no trace context line"]]},
        ]}})
    if p.endswith("/api/v1/query"):
        q = request.url.params.get("query", "")
        if "traces_service_graph" in q:
            res = [{"metric": {"client": "order-service", "server": "payment-service"}, "value": [now, "4.0"]},
                   {"metric": {"client": "traffic-generator", "server": "order-service"}, "value": [now, "5.0"]}]
        else:
            res = [{"metric": {"service_name": s}, "value": [now, v]}
                   for s, v in (("order-service", "0.05"), ("payment-service", "0.02"))]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": res}})
    if p.endswith("/api/v1/query_range"):
        pts = [[now - 60 * i, "0.05"] for i in range(10, 0, -1)]
        res = [{"metric": {"service_name": "payment-service"}, "values": pts}]
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": res}})
    if p.endswith("/api/search"):
        return httpx.Response(200, json={"traces": [{"traceID": TRACE_ID, "rootServiceName": "order-service", "rootTraceName": "POST /orders",
                                                     "startTimeUnixNano": str(int(now * 1e9)), "durationMs": 2100,
                                                     "spanSets": [{"spans": [{}, {}], "matched": 2}]}]})
    if p.startswith("/api/traces/"):
        if p.endswith(TRACE_ID):
            return httpx.Response(200, json=canned_trace())
        return httpx.Response(404)
    if p in ("/-/ready", "/ready"):
        return httpx.Response(200, text="ready")
    return httpx.Response(404)


@pytest.fixture(scope="session")
def mock_telemetry() -> Telemetry:
    http = httpx.AsyncClient(transport=httpx.MockTransport(telemetry_handler))
    return Telemetry(PrometheusClient("http://prom", http), LokiClient("http://loki", http), TempoClient("http://tempo", http), http)


# ---------------------------------------------------------------- database / redis
def _migrate() -> None:
    env = os.environ | {"PYTHONPATH": str(BACKEND)}
    for cmd in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *cmd], cwd=BACKEND, env=env, check=True, capture_output=True)  # noqa: S603 - fixed argv


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def infra(mock_telemetry):  # noqa: ANN001, ANN201
    _migrate()
    init_engine(os.environ["DATABASE_URL"], pool_size=5)
    init_redis(os.environ["REDIS_URL"])
    set_telemetry(mock_telemetry)
    pub = InMemoryPublisher()
    set_publisher(pub)
    async with sessionmaker()() as s:
        for role, pw in PASSWORDS.items():
            s.add(User(username=role, password_hash=hash_password(pw), role=Role(role)))
        await s.commit()
    yield pub
    await close_redis()
    await dispose_engine()


@pytest_asyncio.fixture(loop_scope="session", autouse=False)
async def clean(infra):  # noqa: ANN001, ANN201
    """Per-test isolation for mutable tables (users and the service catalogue are kept)."""
    from sqlalchemy import text
    async with sessionmaker()() as s:
        await s.execute(text("TRUNCATE incidents, anomalies, investigations, root_cause_findings, recommendations, "
                             "incident_events, incident_services, audit_events, fault_injections, app_settings, "
                             "model_evaluations, refresh_tokens RESTART IDENTITY CASCADE"))
        await s.execute(text("UPDATE users SET is_active = true"))
        await s.commit()
    await get_redis().flushdb()
    infra.events.clear()
    yield infra


@pytest_asyncio.fixture(loop_scope="session")
async def client(clean):  # noqa: ANN001, ANN201
    app = create_app(get_settings(), manage_resources=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def user_id(role: str) -> int:
    from sqlalchemy import select
    async with sessionmaker()() as s:
        return (await s.execute(select(User.id).where(User.username == role))).scalar_one()


@pytest_asyncio.fixture(loop_scope="session")
async def tokens(clean) -> dict[str, str]:  # noqa: ANN001
    s = get_settings()
    out = {}
    for role in PASSWORDS:
        out[role] = create_access_token(user_id=await user_id(role), username=role, role=Role(role), secret=s.jwt_secret,
                                        ttl_minutes=5)[0]
    return out


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def event_loop_policy():  # noqa: ANN201
    if sys.platform == "win32":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()
