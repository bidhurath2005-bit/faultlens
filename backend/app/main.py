"""FaultLens API application."""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.v1 import auth, faults, incidents, observability, users
from app.api.v1 import settings as settings_api
from app.core.config import Settings, get_settings
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging
from app.core.redis import close_redis, init_redis
from app.core.security import hash_password, validate_password_policy
from app.db.models import Role, User
from app.db.session import dispose_engine, init_engine, sessionmaker
from app.events.bus import InMemoryPublisher, KafkaPublisher, ensure_topics, set_publisher
from app.events.ws import hub
from app.events.ws import router as ws_router
from app.telemetry.clients import Telemetry, set_telemetry

log = logging.getLogger("faultlens.api")


async def bootstrap_admin(settings: Settings) -> None:
    """First-run admin from environment. Only acts when the users table is empty; never logs the password."""
    async with sessionmaker()() as s:
        if (await s.execute(select(func.count(User.id)))).scalar_one() > 0:
            return
        if not settings.bootstrap_admin_password:
            log.warning("no users exist and BOOTSTRAP_ADMIN_PASSWORD is not set; no admin created")
            return
        validate_password_policy(settings.bootstrap_admin_password)
        s.add(User(username=settings.bootstrap_admin_username, password_hash=hash_password(settings.bootstrap_admin_password),
                   role=Role.ADMIN, is_active=True))
        try:
            await s.commit()
            log.info("bootstrap admin created", extra={"username": settings.bootstrap_admin_username})
        except IntegrityError:
            await s.rollback()  # another worker won the race


def create_app(settings: Settings | None = None, *, manage_resources: bool = True) -> FastAPI:
    """manage_resources=False lets tests own DB/Redis/telemetry/publisher lifecycles (shared event loop)."""
    settings = settings or get_settings()
    configure_logging("api", settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tel = publisher = None
        if manage_resources:
            init_engine(settings.database_url)
            init_redis(settings.redis_url)
            tel = Telemetry.create(settings.prometheus_url, settings.loki_url, settings.tempo_url, settings.telemetry_timeout_s)
            set_telemetry(tel)
            if settings.kafka_enabled:
                await ensure_topics(settings.kafka_bootstrap_servers)
                publisher = KafkaPublisher(settings.kafka_bootstrap_servers)
                await publisher.start()
                set_publisher(publisher)
            else:
                set_publisher(InMemoryPublisher())
            await bootstrap_admin(settings)
        hub.start()
        log.info("faultlens api started", extra={"environment": settings.environment})
        yield
        await hub.stop()
        if manage_resources:
            if publisher:
                await publisher.stop()
            await tel.aclose()
            await close_redis()
            await dispose_engine()

    app = FastAPI(
        title="FaultLens API", version=settings.version, lifespan=lifespan,
        description="Observability & evidence-based root-cause analysis for distributed systems.",
        docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json",
    )
    install_error_handlers(app)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["Authorization", "Content-Type", "X-Request-ID"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # noqa: ANN202
        rid = request.headers.get("x-request-id", "")
        request.state.request_id = rid if rid.isalnum() and len(rid) <= 64 else uuid.uuid4().hex
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/api/v1") and request.url.path != "/api/v1/health/live":
            # Query strings are deliberately not logged (they can carry search text).
            log.info("api request", extra={"method": request.method, "path": request.url.path, "status": response.status_code,
                                           "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                                           "request_id": request.state.request_id})
        return response

    v1 = APIRouter(prefix="/api/v1")
    for r in (observability.router, auth.router, users.router, users.audit_router, incidents.router, faults.router,
              settings_api.router):
        v1.include_router(r)
    app.include_router(v1)
    app.include_router(ws_router)
    return app

