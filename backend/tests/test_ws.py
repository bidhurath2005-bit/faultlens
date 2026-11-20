"""WebSocket event delivery: a real uvicorn server in the test event loop + a real websockets client.

Covers: authenticated connect, event delivery via Redis Pub/Sub, unauthenticated / invalid / expired tokens,
origin check, disconnect cleanup and reconnection (a reconnected client receives new events)."""
import asyncio
import json
import socket

import pytest
import pytest_asyncio
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from app.core.config import get_settings
from app.core.redis import publish_event
from app.core.security import create_access_token
from app.db.models import Role
from app.events.bus import make_event
from app.events.ws import hub
from app.main import create_app

pytestmark = pytest.mark.db


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest_asyncio.fixture(loop_scope="session")
async def server(clean):  # noqa: ANN001, ANN201
    port = _free_port()
    cfg = uvicorn.Config(create_app(get_settings(), manage_resources=False), host="127.0.0.1", port=port, log_level="warning",
                         ws="websockets-sansio", lifespan="on")
    srv = uvicorn.Server(cfg)
    task = asyncio.create_task(srv.serve())
    while not srv.started:  # noqa: ASYNC110 - polling uvicorn's start flag in a test fixture
        await asyncio.sleep(0.05)
    yield f"ws://127.0.0.1:{port}/api/v1/ws/events"
    srv.should_exit = True
    await task


def token(role: str = "viewer", ttl: int = 5) -> str:
    s = get_settings()
    return create_access_token(user_id=1, username=role, role=Role(role), secret=s.jwt_secret, ttl_minutes=ttl)[0]


async def connect(url: str, tok: str | None = None):  # noqa: ANN201
    ws = await websockets.connect(url)
    await ws.send(json.dumps({"type": "auth", "token": tok or token()}))
    hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert hello["type"] == "auth_ok", hello
    return ws


async def next_event(ws) -> dict:  # noqa: ANN001
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if msg["type"] == "event":
            return msg["event"]


async def wait_clients(n: int) -> None:
    for _ in range(100):
        if len(hub.clients) == n:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"expected {n} clients, have {len(hub.clients)}")


async def test_authorised_client_receives_published_events(server):
    ws = await connect(server)
    await wait_clients(1)
    await asyncio.sleep(0.3)  # let the hub's subscription settle
    await publish_event(make_event("incident.created", {"incident": {"id": 42, "title": "x"}, "password": "leak?"}, source="test"))
    ev = await next_event(ws)
    assert ev["type"] == "incident.created" and ev["data"]["incident"]["id"] == 42
    assert "password" not in ev["data"]  # sensitive keys are stripped before broadcast
    await ws.close()


@pytest.mark.parametrize("first_frame", [None, '{"type":"hello"}', '{"type":"auth","token":"garbage"}', "not json"])
async def test_unauthenticated_clients_rejected(server, first_frame):
    ws = await websockets.connect(server)
    if first_frame is not None:
        await ws.send(first_frame)
    with pytest.raises(ConnectionClosed) as e:
        await asyncio.wait_for(ws.recv(), 8)
    assert e.value.rcvd.code == 4401


async def test_expired_token_rejected(server):
    import time

    import jwt
    s = get_settings()
    expired = jwt.encode({"sub": "1", "role": "viewer", "typ": "access", "iss": "faultlens", "iat": 1, "exp": int(time.time()) - 5},
                         s.jwt_secret, algorithm="HS256")
    ws = await websockets.connect(server)
    await ws.send(json.dumps({"type": "auth", "token": expired}))
    with pytest.raises(ConnectionClosed) as e:
        await asyncio.wait_for(ws.recv(), 5)
    assert e.value.rcvd.code == 4401


async def test_foreign_origin_rejected(server):
    """Cross-site WebSocket hijacking guard: the handshake itself is refused (HTTP 403), nothing is upgraded."""
    from websockets.exceptions import InvalidStatus
    with pytest.raises(InvalidStatus) as e:
        await websockets.connect(server, origin="http://evil.example")
    assert e.value.response.status_code == 403


async def test_disconnect_cleanup_and_reconnect_resumes_delivery(server):
    ws = await connect(server)
    await wait_clients(1)
    await ws.close()
    await wait_clients(0)
    ws2 = await connect(server, token("sre"))
    await wait_clients(1)
    await asyncio.sleep(0.3)
    await publish_event(make_event("anomaly.detected", {"service": "payment-service", "metric": "latency_p95"}, source="test"))
    assert (await next_event(ws2))["type"] == "anomaly.detected"
    await ws2.close()


async def test_fanout_to_multiple_clients(server):
    a, b = await connect(server), await connect(server, token("admin"))
    await wait_clients(2)
    await asyncio.sleep(0.3)
    await publish_event(make_event("recovery.verified", {"incident_id": 1}, source="test"))
    assert (await next_event(a))["type"] == (await next_event(b))["type"] == "recovery.verified"
    await a.close()
    await b.close()
