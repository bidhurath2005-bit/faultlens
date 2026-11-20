"""Traffic generator: drives realistic, reproducible load against the demo services.

Profiles: normal | high | spike | random. Rate, mix and RNG seed are configurable at
runtime via a small control API (used by the FaultLens Fault Injection Lab for the
"traffic overload" scenario) and at start-up via environment variables.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from faultlens_common.logging import configure_logging
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pydantic import BaseModel, Field

log = logging.getLogger("traffic")
tracer = trace.get_tracer("faultlens.traffic")

ORDER_URL = os.getenv("ORDER_SERVICE_URL", "http://localhost:8100")
USER_URL = os.getenv("USER_SERVICE_URL", "http://localhost:8101")
INVENTORY_URL = os.getenv("INVENTORY_SERVICE_URL", "http://localhost:8103")
CONTROL_TOKEN = os.getenv("FAULT_ADMIN_TOKEN", "")
MAX_RPS = 200.0


class TrafficConfig(BaseModel):
    enabled: bool = True
    profile: Literal["normal", "high", "spike", "random"] = "normal"
    rps: float = Field(default=float(os.getenv("TRAFFIC_RPS", "8")), gt=0, le=MAX_RPS)
    seed: int = int(os.getenv("TRAFFIC_SEED", "1234"))
    order_ratio: float = Field(default=0.6, ge=0, le=1)
    decline_ratio: float = Field(default=0.03, ge=0, le=1)
    spike_multiplier: float = Field(default=6.0, ge=1, le=20)
    concurrency: int = Field(default=64, ge=1, le=256)
    expires_at: float | None = None  # spike profiles auto-revert


class Stats:
    def __init__(self) -> None:
        self.sent = 0
        self.by_status: dict[str, int] = {}
        self.started = time.time()


state = {"config": TrafficConfig(), "stats": Stats(), "base": TrafficConfig()}


def effective_rps(cfg: TrafficConfig, rng: random.Random) -> float:
    if cfg.profile == "high":
        return min(MAX_RPS, cfg.rps * 2.5)
    if cfg.profile == "spike":
        return min(MAX_RPS, cfg.rps * cfg.spike_multiplier)
    if cfg.profile == "random":
        return min(MAX_RPS, max(0.5, rng.gauss(cfg.rps, cfg.rps * 0.35)))
    return cfg.rps


async def one_request(client: httpx.AsyncClient, rng: random.Random, cfg: TrafficConfig) -> None:
    roll = rng.random()
    user_id = rng.randint(1, 200)
    try:
        with tracer.start_as_current_span("traffic.request", kind=trace.SpanKind.INTERNAL):
            if roll < cfg.order_ratio:
                items = [{"product_id": rng.randint(1, 50), "quantity": rng.randint(1, 3)} for _ in range(rng.randint(1, 3))]
                method = "tok_demo_declined" if rng.random() < cfg.decline_ratio else "tok_demo_visa"
                r = await client.post(f"{ORDER_URL}/orders", json={"user_id": user_id, "items": items, "payment_method": method})
            elif roll < cfg.order_ratio + 0.15:
                r = await client.get(f"{ORDER_URL}/orders", params={"user_id": user_id})
            elif roll < cfg.order_ratio + 0.30:
                r = await client.get(f"{INVENTORY_URL}/products/{rng.randint(1, 50)}")
            else:
                r = await client.get(f"{USER_URL}/users/{user_id}")
        key = str(r.status_code)
    except httpx.HTTPError as exc:
        key = type(exc).__name__
    stats: Stats = state["stats"]
    stats.sent += 1
    stats.by_status[key] = stats.by_status.get(key, 0) + 1


async def run_loop() -> None:
    sem_holder: dict[int, asyncio.Semaphore] = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        current_seed = None
        rng = random.Random()
        while True:
            cfg: TrafficConfig = state["config"]
            if cfg.expires_at and time.time() > cfg.expires_at:
                log.info("traffic profile expired, reverting to baseline")
                state["config"] = cfg = state["base"].model_copy()
            if cfg.seed != current_seed:
                rng, current_seed = random.Random(cfg.seed), cfg.seed
            if not cfg.enabled:
                await asyncio.sleep(0.5)
                continue
            sem = sem_holder.setdefault(cfg.concurrency, asyncio.Semaphore(cfg.concurrency))
            rate = effective_rps(cfg, rng)
            if sem.locked():
                await asyncio.sleep(1.0 / rate)  # saturated: drop this tick rather than queue unboundedly
                continue
            await sem.acquire()
            task = asyncio.create_task(one_request(client, random.Random(rng.random()), cfg))
            task.add_done_callback(lambda _, s=sem: s.release())  # bind the semaphore actually acquired
            await asyncio.sleep(rng.expovariate(rate))  # Poisson arrivals


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("traffic-generator")
    provider = TracerProvider(resource=Resource.create({"service.name": "traffic-generator"}))
    if os.getenv("OTEL_ENABLED", "true") == "true":
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
            endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"), insecure=True)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    if os.getenv("TRAFFIC_AUTOSTART", "true") != "true":
        state["config"].enabled = False
    task = asyncio.create_task(run_loop())
    yield
    task.cancel()


app = FastAPI(title="traffic-generator", lifespan=lifespan)


def _auth(x_fault_token: str = Header(default="")) -> None:
    if len(CONTROL_TOKEN) < 16 or not hmac.compare_digest(x_fault_token, CONTROL_TOKEN):
        raise HTTPException(401, "invalid control token")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/config")
async def get_config() -> dict:
    s: Stats = state["stats"]
    return {"config": state["config"].model_dump(), "stats": {"sent": s.sent, "by_status": s.by_status,
                                                                "uptime_s": round(time.time() - s.started)}}


class ConfigPatch(BaseModel):
    enabled: bool | None = None
    profile: Literal["normal", "high", "spike", "random"] | None = None
    rps: float | None = Field(default=None, gt=0, le=MAX_RPS)
    seed: int | None = None
    spike_multiplier: float | None = Field(default=None, ge=1, le=20)
    duration_s: int | None = Field(default=None, ge=10, le=900)


@app.put("/config", dependencies=[Depends(_auth)])
async def put_config(patch: ConfigPatch) -> dict:
    data = state["config"].model_dump() | patch.model_dump(exclude_none=True, exclude={"duration_s"})
    data["expires_at"] = time.time() + patch.duration_s if patch.duration_s and data["profile"] != "normal" else None
    state["config"] = TrafficConfig(**data)
    if data["profile"] == "normal" and data["expires_at"] is None:
        state["base"] = state["config"].model_copy()
    log.warning("traffic configuration changed", extra={"profile": data["profile"], "rps": data["rps"]})
    return await get_config()


@app.post("/config/reset", dependencies=[Depends(_auth)])
async def reset_config() -> dict:
    state["config"] = state["base"].model_copy()
    state["config"].profile = "normal"
    state["config"].expires_at = None
    return await get_config()
