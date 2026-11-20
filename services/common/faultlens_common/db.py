"""Instrumented async database access with a bounded, observable connection pool."""
from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.metrics import Observation
from sqlalchemy import event, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from .faults import FaultManager
from .telemetry import instruments, register_gauge_source

log = logging.getLogger("faultlens.db")
tracer = trace.get_tracer("faultlens.db")


class DatabaseUnavailable(Exception):
    """Raised when a connection cannot be obtained from the pool in time."""


class InstrumentedDatabase:
    def __init__(self, url: str, pool_size: int, pool_timeout_s: float, slow_query_s: float,
                 faults: FaultManager, service_name: str) -> None:
        self.pool_size = pool_size
        self.pool_timeout_s = pool_timeout_s
        self.slow_query_s = slow_query_s
        self.faults = faults
        self.service_name = service_name
        self.engine: AsyncEngine = create_async_engine(
            url, pool_size=pool_size, max_overflow=0, pool_timeout=pool_timeout_s, pool_pre_ping=True,
        )
        faults.attach_db(self)
        SQLAlchemyInstrumentor().instrument(engine=self.engine.sync_engine)
        self._install_query_timing()
        register_gauge_source("db_pool", self._pool_observations)

    def _pool_observations(self):
        pool = self.engine.pool
        yield Observation(pool.checkedout(), {"state": "in_use"})
        yield Observation(self.pool_size, {"state": "max"})

    def _install_query_timing(self) -> None:
        sync_engine = self.engine.sync_engine

        @event.listens_for(sync_engine, "before_cursor_execute")
        def _before(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
            conn.info.setdefault("_q_start", []).append(time.perf_counter())

        @event.listens_for(sync_engine, "after_cursor_execute")
        def _after(conn, cursor, statement, params, context, executemany):  # noqa: ANN001
            started = conn.info["_q_start"].pop()
            elapsed = time.perf_counter() - started
            op = statement.strip().split(None, 1)[0].upper() if statement.strip() else "UNKNOWN"
            instruments().db_query_duration.record(elapsed, {"db.operation": op})
            if elapsed >= self.slow_query_s:
                log.warning("slow query detected", extra={"db_operation": op, "duration_ms": round(elapsed * 1000, 1)})

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection]:
        """Acquire a pooled connection inside a transaction, recording pool wait time."""
        start = time.perf_counter()
        with tracer.start_as_current_span("db.pool.acquire") as span:
            try:
                conn = await self.engine.connect()
            except PoolTimeoutError as exc:
                waited = time.perf_counter() - start
                instruments().db_pool_wait.record(waited)
                instruments().db_pool_timeouts.add(1)
                span.set_status(trace.Status(trace.StatusCode.ERROR, "pool timeout"))
                span.record_exception(exc)
                log.error("database connection pool exhausted: QueuePool limit reached, connection timed out",
                          extra={"pool_size": self.pool_size, "pool_in_use": self.engine.pool.checkedout(),
                                 "waited_ms": round(waited * 1000, 1)})
                raise DatabaseUnavailable("connection pool exhausted") from exc
            waited = time.perf_counter() - start
            instruments().db_pool_wait.record(waited)
            span.set_attribute("db.pool.wait_ms", round(waited * 1000, 2))
        try:
            async with conn.begin():
                delay = self.faults.db_delay_s()
                if delay:
                    # Real server-side latency: the database itself sleeps inside the transaction.
                    await conn.execute(text("SELECT pg_sleep(:s)"), {"s": delay})
                yield conn
        finally:
            await conn.close()

    async def leak_connections(self, n: int) -> list[AsyncConnection]:
        held = []
        for _ in range(min(n, self.pool_size)):
            held.append(await self.engine.connect())
        log.warning("holding pooled connections (fault)", extra={"held": len(held), "pool_size": self.pool_size})
        return held

    async def release_connections(self, conns: list[AsyncConnection]) -> None:
        for c in conns:
            await c.close()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as c:
                await c.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def dispose(self) -> None:
        await self.engine.dispose()
