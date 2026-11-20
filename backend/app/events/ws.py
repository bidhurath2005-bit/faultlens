"""WebSocket hub: authenticated real-time delivery of operational events.

Flow: Kafka topics -> (worker) realtime bridge -> Redis Pub/Sub `faultlens:events` -> every API
process's hub -> connected, authenticated sockets. Redis Pub/Sub is what lets N uvicorn workers
each deliver every event to their own clients.

Protocol:
  client -> {"type":"auth","token":"<access JWT>"}         (must be the first frame, within 5 s)
  server -> {"type":"auth_ok","user":..,"role":..}         or close(4401 / 4403)
  server -> {"type":"event","event":{id,type,occurred_at,data}}
  server -> {"type":"ping"} every 20 s;  client -> {"type":"pong"}
  Tokens are re-validated against expiry on every send; an expired token closes with 4401 so the
  client refreshes and reconnects. Tokens are never placed in URLs (they would leak into logs).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.core.config import get_settings
from app.core.redis import EVENTS_CHANNEL, subscribe
from app.core.security import Permission, TokenError, decode_access_token, has_permission

log = logging.getLogger("faultlens.ws")
router = APIRouter()

PING_INTERVAL_S = 20
AUTH_TIMEOUT_S = 5
SENSITIVE_KEYS = {"password", "token", "secret", "client_ip", "api_key"}


def sanitize(obj):  # noqa: ANN001, ANN201
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if k.lower() not in SENSITIVE_KEYS}
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


@dataclass(eq=False)
class Client:
    ws: WebSocket
    user: str
    role: str
    exp: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=200))


class Hub:
    def __init__(self) -> None:
        self.clients: set[Client] = set()
        self._task: asyncio.Task | None = None
        self.delivered = 0

    def start(self) -> None:
        self._task = asyncio.create_task(self._subscribe_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _subscribe_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                backoff = 1.0
                async for data in subscribe(EVENTS_CHANNEL):
                    try:
                        event = sanitize(json.loads(data))
                    except json.JSONDecodeError:
                        continue
                    self.broadcast(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("redis subscription lost; retrying", extra={"error": str(exc), "backoff_s": backoff})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def broadcast(self, event: dict) -> None:
        for c in list(self.clients):
            if not has_permission(c.role, Permission.READ):
                continue
            try:
                c.queue.put_nowait({"type": "event", "event": event})
            except asyncio.QueueFull:
                log.warning("slow websocket client; dropping event", extra={"user": c.user})


hub = Hub()


async def _authenticate(ws: WebSocket) -> Client | None:
    settings = get_settings()
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=AUTH_TIMEOUT_S)
        msg = json.loads(raw)
    except (TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
        await ws.close(code=4401, reason="authentication required")
        return None
    if not isinstance(msg, dict) or msg.get("type") != "auth" or not isinstance(msg.get("token"), str):
        await ws.close(code=4401, reason="authentication required")
        return None
    try:
        claims = decode_access_token(msg["token"], settings.jwt_secret, settings.jwt_algorithm)
    except TokenError:
        await ws.close(code=4401, reason="invalid or expired token")
        return None
    if not has_permission(claims["role"], Permission.READ):
        await ws.close(code=4403, reason="forbidden")
        return None
    return Client(ws, claims.get("usr", claims["sub"]), claims["role"], int(claims["exp"]))


@router.websocket("/api/v1/ws/events")
async def events_ws(ws: WebSocket) -> None:
    origin = ws.headers.get("origin")
    if origin and origin not in get_settings().cors_origins:
        await ws.close(code=4403, reason="origin not allowed")  # cross-site WebSocket hijacking guard
        return
    await ws.accept()
    client = await _authenticate(ws)
    if client is None:
        return
    hub.clients.add(client)
    await ws.send_json({"type": "auth_ok", "user": client.user, "role": client.role, "token_expires_at": client.exp})

    async def reader() -> None:
        while True:
            msg = await ws.receive_text()
            if len(msg) > 4096:
                await ws.close(code=1009)
                return

    async def writer() -> None:
        last_ping = time.monotonic()
        while True:
            try:
                item = await asyncio.wait_for(client.queue.get(), timeout=1.0)
            except TimeoutError:
                item = None
            if time.time() >= client.exp:
                await ws.close(code=4401, reason="token expired")
                return
            if item is not None:
                await ws.send_json(item)
                hub.delivered += 1
            if time.monotonic() - last_ping >= PING_INTERVAL_S:
                await ws.send_json({"type": "ping", "ts": time.time()})
                last_ping = time.monotonic()

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for t in tasks:
            if t.done() and not t.cancelled():
                t.exception()  # consume (normally WebSocketDisconnect) so it is not reported as unhandled
            t.cancel()
        hub.clients.discard(client)
