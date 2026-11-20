"""FastAPI application factory shared by all demo microservices."""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from starlette.middleware.base import BaseHTTPMiddleware

from .config import ServiceSettings
from .db import DatabaseUnavailable, InstrumentedDatabase
from .faults import FaultManager, FaultRequest
from .logging import configure_logging, request_id_var
from .telemetry import instruments, setup_telemetry

log = logging.getLogger("faultlens.service")

_UNMETERED = {"/health", "/health/ready", "/admin/faults"}


class DownstreamError(Exception):
    def __init__(self, peer: str, status: int | None, detail: str) -> None:
        super().__init__(f"{peer}: {detail}")
        self.peer, self.status, self.detail = peer, status, detail


class ServiceClient:
    """httpx wrapper: propagates W3C trace context (via instrumentation) and the request id,
    applies timeouts and records per-peer client latency/outcome metrics."""

    def __init__(self, settings: ServiceSettings) -> None:
        self._client = httpx.AsyncClient(timeout=settings.downstream_timeout_s)

    async def request(self, peer: str, method: str, url: str, **kw) -> httpx.Response:
        headers = kw.pop("headers", {}) | ({"x-request-id": rid} if (rid := request_id_var.get()) else {})
        start = time.perf_counter()
        outcome, status = "ok", None
        try:
            resp = await self._client.request(method, url, headers=headers, **kw)
            status = resp.status_code
            if resp.status_code >= 500:
                outcome = "error"
                log.error("downstream call failed", extra={"peer_service": peer, "status_code": status, "url_path": httpx.URL(url).path})
                raise DownstreamError(peer, status, f"HTTP {status}")
            return resp
        except httpx.TimeoutException as exc:
            outcome = "timeout"
            log.error("downstream call timed out", extra={"peer_service": peer, "timeout_s": self._client.timeout.read})
            raise DownstreamError(peer, None, "timeout") from exc
        except httpx.TransportError as exc:
            outcome = "connect_error"
            log.error("downstream service unreachable", extra={"peer_service": peer, "error_type": type(exc).__name__})
            raise DownstreamError(peer, None, "unreachable") from exc
        finally:
            instruments().http_client_duration.record(
                time.perf_counter() - start, {"peer.service": peer, "outcome": outcome})

    async def aclose(self) -> None:
        await self._client.aclose()


class RequestMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, faults: FaultManager) -> None:  # noqa: ANN001
        super().__init__(app)
        self.faults = faults

    async def dispatch(self, request: Request, call_next):  # noqa: ANN001
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if len(rid) > 64 or not rid.replace("-", "").isalnum():
            rid = uuid.uuid4().hex  # never trust malformed ids into logs
        token = request_id_var.set(rid)
        path = request.url.path
        start = time.perf_counter()
        route = path
        status = 500
        try:
            forced = None
            if path not in _UNMETERED:
                forced = await self.faults.apply_request_faults()
            elif path == "/health" and self.faults.get("unavailable") is not None:
                forced = 503  # a simulated outage must also fail liveness probes
            if forced is not None:
                status = forced
                msg = "service unavailable (injected)" if forced == 503 else "internal server error (injected)"
                if forced == 500:
                    log.error("unhandled exception while processing request",
                              extra={"http_route": path, "error_kind": "InjectedFault"})
                trace.get_current_span().set_status(trace.Status(trace.StatusCode.ERROR, msg))
                response = JSONResponse({"detail": msg, "request_id": rid}, status_code=forced)
                response.headers["x-request-id"] = rid
                return response
            response = await call_next(request)
            status = response.status_code
            if (r := request.scope.get("route")) is not None:
                route = getattr(r, "path", path)
            response.headers["x-request-id"] = rid
            return response
        finally:
            elapsed = time.perf_counter() - start
            if path not in _UNMETERED:
                attrs = {"http.route": route, "http.method": request.method, "http.status_code": str(status),
                         "status_class": f"{status // 100}xx"}
                instruments().http_server_duration.record(elapsed, attrs)
                instruments().http_server_requests.add(1, attrs)
                lvl = logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
                log.log(lvl, "request completed", extra={"http_method": request.method, "http_route": route,
                                                         "status_code": status, "duration_ms": round(elapsed * 1000, 1)})
            request_id_var.reset(token)


async def _loop_lag_monitor(threshold_s: float = 0.2) -> None:
    """Logs real event-loop starvation (e.g. CPU saturation / GIL contention)."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(0.5)
        lag = time.perf_counter() - t0 - 0.5
        if lag > threshold_s:
            log.warning("event loop lag detected; process is CPU starved", extra={"loop_lag_ms": round(lag * 1000)})


async def _memory_watch(threshold: float = 0.8) -> None:
    import psutil

    from .telemetry import _cgroup_memory_limit

    limit = _cgroup_memory_limit()
    proc = psutil.Process()
    while limit:
        await asyncio.sleep(5)
        rss = proc.memory_info().rss
        if rss / limit > threshold:
            log.warning("memory usage high, approaching container limit",
                        extra={"rss_mb": rss // 2**20, "limit_mb": limit // 2**20})


def create_service_app(
    settings: ServiceSettings,
    routers: list[APIRouter],
    *,
    with_db: bool = True,
    on_startup: Callable[[FastAPI], Awaitable[None]] | None = None,
) -> FastAPI:
    configure_logging(settings.service_name, settings.log_level)
    setup_telemetry(settings)
    faults = FaultManager(settings.service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.faults = faults
        app.state.client = ServiceClient(settings)
        app.state.db = None
        if with_db:
            app.state.db = InstrumentedDatabase(settings.database_url, settings.db_pool_size, settings.db_pool_timeout_s,
                                                settings.db_slow_query_threshold_s, faults, settings.service_name)
        faults.start()
        bg = [asyncio.create_task(_loop_lag_monitor()), asyncio.create_task(_memory_watch())]
        if on_startup:
            for attempt in range(30):
                try:
                    await on_startup(app)
                    break
                except Exception as exc:  # noqa: BLE001 - DB may still be starting
                    log.warning("startup hook failed, retrying", extra={"attempt": attempt, "error": str(exc)})
                    await asyncio.sleep(2)
            else:
                raise RuntimeError("startup failed")
        log.info("service started", extra={"fault_injection": settings.fault_injection_allowed})
        yield
        for t in bg:
            t.cancel()
        await faults.stop()
        await app.state.client.aclose()
        if app.state.db:
            await app.state.db.dispose()

    app = FastAPI(title=settings.service_name, version=settings.service_version, lifespan=lifespan)
    app.add_middleware(RequestMiddleware, faults=faults)

    @app.exception_handler(DatabaseUnavailable)
    async def _db_unavailable(request: Request, exc: DatabaseUnavailable):  # noqa: ANN202
        trace.get_current_span().set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        return JSONResponse({"detail": "database unavailable", "request_id": request_id_var.get()}, status_code=503)

    @app.exception_handler(DownstreamError)
    async def _downstream(request: Request, exc: DownstreamError):  # noqa: ANN202
        trace.get_current_span().set_status(trace.Status(trace.StatusCode.ERROR, str(exc)))
        status = 504 if exc.detail == "timeout" else 502
        return JSONResponse({"detail": f"dependency {exc.peer} failed: {exc.detail}",
                             "request_id": request_id_var.get()}, status_code=status)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202
        log.exception("unhandled exception while processing request")
        return JSONResponse({"detail": "internal server error", "request_id": request_id_var.get()}, status_code=500)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        return {"status": "ok", "service": settings.service_name}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> JSONResponse:
        db: InstrumentedDatabase | None = request.app.state.db
        ok = True if db is None else await db.ping()
        return JSONResponse({"status": "ready" if ok else "degraded", "database": ok}, status_code=200 if ok else 503)

    # ---- fault administration (internal; reachable only on the private network) -------------
    admin = APIRouter(prefix="/admin", tags=["fault-injection"])

    def _require_fault_token(x_fault_token: str = Header(default="")) -> None:
        if not settings.fault_injection_allowed:
            raise HTTPException(403, "fault injection disabled in this environment")
        if not hmac.compare_digest(x_fault_token, settings.fault_admin_token):
            raise HTTPException(401, "invalid fault token")

    @admin.get("/faults", dependencies=[Depends(_require_fault_token)])
    async def list_faults() -> dict:
        return {"service": settings.service_name, "faults": faults.snapshot()}

    @admin.post("/faults", dependencies=[Depends(_require_fault_token)], status_code=201)
    async def inject_fault(req: FaultRequest) -> dict:
        if req.type == "pool_exhaustion" and not with_db:
            raise HTTPException(422, "service has no database")
        f = await faults.inject(req)
        return {"service": settings.service_name, "fault": f.public()}

    @admin.delete("/faults", dependencies=[Depends(_require_fault_token)])
    async def clear_faults() -> dict:
        return {"service": settings.service_name, "removed": await faults.clear()}

    @admin.delete("/faults/{fault_type}", dependencies=[Depends(_require_fault_token)])
    async def remove_fault(fault_type: str) -> dict:
        if not await faults.remove(fault_type):
            raise HTTPException(404, "fault not active")
        return {"service": settings.service_name, "removed": 1}

    app.include_router(admin)
    for r in routers:
        app.include_router(r)

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,health/ready,admin/faults", exclude_spans=["receive", "send"])
    HTTPXClientInstrumentor().instrument()
    return app
