"""Controlled, reversible, bounded fault injection for the local demo environment.

Each fault is real: latency sleeps the request, db_delay runs pg_sleep inside the
transaction, pool_exhaustion checks connections out of the real pool and holds them,
cpu_burn spins a thread, memory_pressure allocates real memory. Every fault has a hard
expiry (auto-restore) and parameter limits enforced here, independent of the caller.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger("faultlens.faults")

FaultType = Literal["latency", "error_500", "unavailable", "db_delay", "pool_exhaustion", "cpu_burn", "memory_pressure"]

LIMITS = {
    "latency_ms": 10_000,
    "db_delay_ms": 5_000,
    "error_rate": 1.0,
    "cpu_threads": 2,
    "memory_mb": 384,
}


class FaultRequest(BaseModel):
    type: FaultType
    duration_s: int = Field(default=300, ge=10, le=900)
    latency_ms: int = Field(default=1500, ge=0, le=LIMITS["latency_ms"])
    jitter_ms: int = Field(default=200, ge=0, le=2000)
    db_delay_ms: int = Field(default=800, ge=0, le=LIMITS["db_delay_ms"])
    error_rate: float = Field(default=0.5, ge=0.0, le=LIMITS["error_rate"])
    cpu_threads: int = Field(default=1, ge=1, le=LIMITS["cpu_threads"])
    memory_mb: int = Field(default=256, ge=16, le=LIMITS["memory_mb"])
    leak_connections: int | None = Field(default=None, ge=1, le=50)

    @model_validator(mode="after")
    def _sane(self) -> FaultRequest:
        if self.type == "error_500" and self.error_rate <= 0:
            raise ValueError("error_rate must be > 0 for error_500")
        return self


@dataclass
class ActiveFault:
    type: str
    params: dict[str, Any]
    started_at: float
    expires_at: float
    handles: dict[str, Any] = field(default_factory=dict, repr=False)

    def public(self) -> dict[str, Any]:
        # Not dataclasses.asdict(): it would deep-copy live handles (DB connections, threads).
        return {"type": self.type, "params": dict(self.params), "started_at": self.started_at,
                "expires_at": self.expires_at, "remaining_s": max(0, round(self.expires_at - time.time()))}


class FaultManager:
    def __init__(self, service_name: str) -> None:
        self.service_name = service_name
        self.active: dict[str, ActiveFault] = {}
        self._lock = asyncio.Lock()
        self._db = None  # set by db module (InstrumentedDatabase)
        self._reaper: asyncio.Task | None = None

    def attach_db(self, db: Any) -> None:
        self._db = db

    def start(self) -> None:
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        if self._reaper:
            self._reaper.cancel()
        await self.clear()

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(2)
            now = time.time()
            for ftype in [k for k, f in self.active.items() if f.expires_at <= now]:
                log.warning("fault expired, restoring normal behaviour", extra={"fault_type": ftype})
                await self.remove(ftype)

    # -- lifecycle -------------------------------------------------------
    async def inject(self, req: FaultRequest) -> ActiveFault:
        async with self._lock:
            if req.type in self.active:
                await self._teardown(self.active.pop(req.type))
            now = time.time()
            fault = ActiveFault(type=req.type, params=req.model_dump(exclude={"type"}), started_at=now,
                                expires_at=now + req.duration_s)
            await self._setup(fault)
            self.active[req.type] = fault
            log.warning("fault injected", extra={"fault_type": req.type, "fault_params": fault.params})
            return fault

    async def remove(self, ftype: str) -> bool:
        async with self._lock:
            fault = self.active.pop(ftype, None)
            if fault:
                await self._teardown(fault)
                log.warning("fault removed", extra={"fault_type": ftype})
            return fault is not None

    async def clear(self) -> int:
        n = 0
        for ftype in list(self.active):
            n += int(await self.remove(ftype))
        return n

    async def _setup(self, fault: ActiveFault) -> None:
        p = fault.params
        if fault.type == "cpu_burn":
            stop = threading.Event()
            threads = [threading.Thread(target=_burn, args=(stop,), daemon=True, name=f"cpu-burn-{i}")
                       for i in range(p["cpu_threads"])]
            for t in threads:
                t.start()
            fault.handles["stop"] = stop
        elif fault.type == "memory_pressure":
            fault.handles["task"] = asyncio.create_task(_grow_memory(fault, p["memory_mb"]))
        elif fault.type == "pool_exhaustion":
            if self._db is None:
                raise RuntimeError("service has no database pool")
            n = p.get("leak_connections") or self._db.pool_size
            fault.handles["conns"] = await self._db.leak_connections(n)

    async def _teardown(self, fault: ActiveFault) -> None:
        if fault.type == "cpu_burn":
            fault.handles["stop"].set()
        elif fault.type == "memory_pressure":
            fault.handles["task"].cancel()
            fault.handles.pop("blocks", None)
        elif fault.type == "pool_exhaustion" and self._db is not None:
            await self._db.release_connections(fault.handles.get("conns", []))

    # -- hooks used by middleware / db layer -----------------------------
    def get(self, ftype: str) -> ActiveFault | None:
        f = self.active.get(ftype)
        return f if f and f.expires_at > time.time() else None

    async def apply_request_faults(self) -> int | None:
        """Called per business request. Returns an HTTP status to short-circuit with, or None."""
        if (f := self.get("unavailable")) is not None:
            return 503
        if (f := self.get("latency")) is not None:
            delay = f.params["latency_ms"] + random.uniform(-1, 1) * f.params["jitter_ms"]
            await asyncio.sleep(max(0.0, delay) / 1000)
        if (f := self.get("error_500")) is not None and random.random() < f.params["error_rate"]:
            return 500
        return None

    def db_delay_s(self) -> float:
        f = self.get("db_delay")
        if f is None:
            return 0.0
        return max(0.0, f.params["db_delay_ms"] * random.uniform(0.8, 1.2)) / 1000

    def snapshot(self) -> list[dict[str, Any]]:
        return [f.public() for f in self.active.values() if f.expires_at > time.time()]


def _burn(stop: threading.Event) -> None:
    x = 0
    while not stop.is_set():
        for i in range(20_000):
            x = (x * 31 + i) % 1_000_003


async def _grow_memory(fault: ActiveFault, target_mb: int) -> None:
    blocks: list[bytearray] = []
    fault.handles["blocks"] = blocks
    step = 16
    while len(blocks) * step < target_mb:
        blocks.append(bytearray(b"\x5a") * (step * 1024 * 1024))  # written bytes => real RSS growth
        await asyncio.sleep(1.0)
    await asyncio.Event().wait()  # hold the memory until the fault is cancelled
