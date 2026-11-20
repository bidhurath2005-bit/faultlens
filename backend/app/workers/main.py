"""FaultLens worker: probes, anomaly detection, Kafka consumers (correlation + realtime bridge), recovery checks.

Run: python -m app.workers.main
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from datetime import UTC, datetime, timedelta

import httpx
from prometheus_client import Counter, Gauge, Histogram, start_http_server
from sqlalchemy import select

from app.core.config import DEMO_SERVICES, Settings, get_settings
from app.core.logging import configure_logging
from app.core.redis import LeaderLock, get_redis, init_redis, publish_event, subscribe
from app.db.models import Anomaly, Incident, IncidentStatus, Severity
from app.db.session import init_engine, sessionmaker
from app.engines.anomaly.detectors import severity_for
from app.engines.anomaly.iforest import IsolationForestDetector
from app.engines.anomaly.pipeline import Confirmer, FeatureFetcher, detect_service
from app.engines.dependencies import DependencyGraph, observed_graph
from app.engines.rca.engine import run_investigation
from app.engines.rca.evidence import EvidenceCollector
from app.events.bus import (
    ALL_TOPICS,
    TOPIC_ANOMALIES,
    TOPIC_HEALTH,
    TOPIC_INCIDENTS,
    KafkaPublisher,
    emit,
    ensure_topics,
    set_publisher,
)
from app.services import incidents as incsvc
from app.services.settings_store import get_setting
from app.telemetry.clients import Telemetry, set_telemetry

log = logging.getLogger("faultlens.worker")

PROBE_UP = Gauge("faultlens_probe_up", "1 if the service health probe succeeded", ["service_name"])
PROBE_LAT = Gauge("faultlens_probe_duration_seconds", "Health probe latency", ["service_name"])
CYCLE_TIME = Histogram("faultlens_detection_cycle_seconds", "Anomaly detection cycle duration",
                       buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10])
ANOMALIES = Counter("faultlens_anomalies_total", "Anomalies emitted", ["service_name", "method"])
RCA_TIME = Histogram("faultlens_rca_duration_seconds", "RCA investigation duration", buckets=[0.25, 0.5, 1, 2, 5, 10, 30])
EVENT_LAG = Histogram("faultlens_event_bridge_lag_seconds", "Kafka event age when bridged to WebSocket clients",
                      buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5])


class Worker:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.tel = Telemetry.create(settings.prometheus_url, settings.loki_url, settings.tempo_url, settings.telemetry_timeout_s)
        set_telemetry(self.tel)
        self.iforest = IsolationForestDetector()
        self.config: dict | None = None
        self.graph = DependencyGraph()
        self.probe_state: dict[str, bool] = {}
        self.stop = asyncio.Event()
        self.rca_tasks: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------ probes
    async def probe_loop(self) -> None:
        async with httpx.AsyncClient(timeout=2.0) as c:
            while not self.stop.is_set():
                for name in DEMO_SERVICES:
                    url = self.s.service_urls.get(name)
                    if not url:
                        continue
                    t0 = time.perf_counter()
                    try:
                        ok = (await c.get(f"{url}/health")).status_code == 200
                    except httpx.HTTPError:
                        ok = False
                    PROBE_LAT.labels(name).set(time.perf_counter() - t0)
                    PROBE_UP.labels(name).set(1 if ok else 0)
                    prev = self.probe_state.get(name)
                    self.probe_state[name] = ok
                    if prev is not None and prev != ok:
                        await emit(TOPIC_HEALTH, "service.health_changed", {"service": name, "up": ok}, source="prober", key=name)
                await asyncio.sleep(self.s.probe_interval_s)

    # ------------------------------------------------------------ detection
    async def load_config(self) -> dict:
        async with sessionmaker()() as s:
            self.config = await get_setting(s, "detection")
        self.iforest.threshold = self.config["iforest_threshold"]
        return self.config

    async def settings_listener(self) -> None:
        while not self.stop.is_set():
            try:
                async for _ in subscribe("faultlens:settings"):
                    await self.load_config()
                    log.info("detection settings reloaded")
            except Exception as exc:  # noqa: BLE001
                log.warning("settings listener error", extra={"error": str(exc)})
                await asyncio.sleep(5)

    async def detection_loop(self) -> None:
        lock = LeaderLock(get_redis(), "detector", ttl_s=int(self.s.detect_interval_s * 3))
        fetcher = FeatureFetcher(self.tel.prom)
        while not self.stop.is_set():
            started = time.perf_counter()
            try:
                if await lock.acquire_or_renew():
                    await self.detect_once(fetcher)
            except Exception as exc:  # noqa: BLE001
                log.warning("detection cycle failed", extra={"error": str(exc)})
            CYCLE_TIME.observe(time.perf_counter() - started)
            await asyncio.sleep(max(0.5, self.s.detect_interval_s - (time.perf_counter() - started)))

    async def detect_once(self, fetcher: FeatureFetcher) -> int:
        cfg = self.config or await self.load_config()
        confirmer = Confirmer(get_redis(), cfg["confirm_cycles"], cfg["cooldown_s"], self.s.detect_interval_s)
        feats = await fetcher.fetch()
        now = datetime.now(UTC)
        pending: list[tuple[str, dict]] = []
        async with sessionmaker()() as session:
            svcs = await incsvc.services_by_name(session)
            for name, sf in feats.items():
                if name not in svcs:
                    continue
                dets = await confirmer.filter(name, detect_service(sf, cfg, self.iforest))
                for d in dets:
                    a = Anomaly(service_id=svcs[name].id, detected_at=now, metric=d.metric, method=d.method,
                                score=float(d.score), severity=Severity(severity_for(d)), observed_value=float(d.observed),
                                baseline_value=float(d.baseline) if d.baseline is not None else None, details=d.details)
                    session.add(a)
                    await session.flush()
                    ANOMALIES.labels(name, d.method).inc()
                    pending.append((name, {"id": a.id, "service": name, "metric": d.metric, "method": d.method, "score": a.score,
                                           "severity": a.severity.value, "observed": a.observed_value,
                                           "baseline": a.baseline_value, "detected_at": now.isoformat()}))
            # Commit BEFORE publishing: consumers look the row up by id, so an event must never be visible
            # before the row it refers to (transactional-outbox ordering without the outbox table).
            await session.commit()
        for name, data in pending:
            await emit(TOPIC_ANOMALIES, "anomaly.detected", data, source="detector", key=name)
        return len(pending)

    # ------------------------------------------------------------ correlation (Kafka consumer)
    async def refresh_graph(self) -> None:
        while not self.stop.is_set():
            try:
                self.graph = await observed_graph(self.tel.prom, "10m")
            except Exception as exc:  # noqa: BLE001
                log.warning("graph refresh failed", extra={"error": str(exc)})
            await asyncio.sleep(30)

    async def correlator(self) -> None:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(TOPIC_ANOMALIES, bootstrap_servers=self.s.kafka_bootstrap_servers,
                                    group_id="faultlens-correlator", enable_auto_commit=False, auto_offset_reset="latest",
                                    value_deserializer=lambda b: json.loads(b))
        await consumer.start()
        try:
            async for msg in consumer:
                try:
                    await self.handle_anomaly(msg.value)
                except Exception:  # noqa: BLE001
                    log.exception("failed to correlate anomaly", extra={"offset": msg.offset})
                await consumer.commit()  # at-least-once; handle_anomaly is idempotent per anomaly id
        finally:
            await consumer.stop()

    async def handle_anomaly(self, event: dict) -> None:
        aid = event["data"]["id"]
        async with sessionmaker()() as session:
            a = None
            for _ in range(5):
                a = await session.get(Anomaly, aid)
                if a is not None:
                    break
                await asyncio.sleep(0.5)
                session.expire_all()
            if a is None:
                log.warning("anomaly event refers to a missing row; skipping", extra={"anomaly_id": aid})
                return
            if a.incident_id is not None:
                return  # already correlated (at-least-once redelivery)
            inc, created = await incsvc.correlate_anomaly(session, a, self.graph.related)
            await session.commit()
            payload = {"incident": incsvc.incident_to_dict(inc) | {"opened_at": inc.opened_at.isoformat()}, "anomaly_id": aid}
        await emit(TOPIC_INCIDENTS, "incident.created" if created else "incident.updated", payload, source="correlator",
                   key=str(inc.id))
        self.schedule_rca(inc.id, delay=self.s.investigation_delay_s if created else 45)

    def schedule_rca(self, incident_id: int, delay: float) -> None:
        t = self.rca_tasks.get(incident_id)
        if t and not t.done():
            return  # one pending investigation per incident; it will see all evidence so far
        self.rca_tasks[incident_id] = asyncio.create_task(self._rca_after(incident_id, delay))

    async def _rca_after(self, incident_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        r = get_redis()
        if not await r.set(f"rca:lock:{incident_id}", "1", nx=True, ex=40):
            return
        runs = await r.incr(f"rca:runs:{incident_id}")
        await r.expire(f"rca:runs:{incident_id}", 3600)
        if runs > 6:  # bounded automatic re-investigation per incident; manual re-runs remain available
            return
        t0 = time.perf_counter()
        async with sessionmaker()() as session:
            inc = await incsvc.get_incident(session, incident_id)
            if inc.status == IncidentStatus.RESOLVED:
                return
            inv = await run_investigation(session, EvidenceCollector(self.tel, DEMO_SERVICES), inc, trigger="auto")
            await session.commit()
            top = inv.findings[0] if inv.findings else None
            data = {"incident_id": incident_id, "investigation_id": inv.id, "status": inv.status.value, "summary": inv.summary,
                    "incident_title": inc.title,
                    "top": {"cause_type": top.cause_type, "service": top.service.name if top and top.service else None,
                            "score": top.score, "confidence": top.confidence} if top else None}
        RCA_TIME.observe(time.perf_counter() - t0)
        await emit(TOPIC_INCIDENTS, "investigation.completed", data, source="rca", key=str(incident_id))

    # ------------------------------------------------------------ realtime bridge (Kafka -> Redis Pub/Sub)
    async def bridge(self) -> None:
        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(*ALL_TOPICS, bootstrap_servers=self.s.kafka_bootstrap_servers,
                                    group_id="faultlens-realtime-bridge", auto_offset_reset="latest",
                                    value_deserializer=lambda b: json.loads(b))
        await consumer.start()
        try:
            async for msg in consumer:
                ev = msg.value
                try:
                    lag = time.time() - datetime.fromisoformat(ev["occurred_at"]).timestamp()
                    EVENT_LAG.observe(max(0.0, lag))
                except (KeyError, ValueError):
                    pass
                await publish_event({k: ev.get(k) for k in ("id", "type", "occurred_at", "source", "data")})
        finally:
            await consumer.stop()

    # ------------------------------------------------------------ recovery verification
    async def recovery_loop(self) -> None:
        """An incident's recovery is verified when, for every affected service, the probe is up and no anomaly
        has been recorded for 90 s. Applies to open and resolved incidents that are not yet verified."""
        while not self.stop.is_set():
            await asyncio.sleep(15)
            try:
                await self.check_recovery()
            except Exception as exc:  # noqa: BLE001
                log.warning("recovery check failed", extra={"error": str(exc)})

    async def check_recovery(self, quiet_s: int = 90) -> list[int]:
        now = datetime.now(UTC)
        verified = []
        async with sessionmaker()() as session:
            incs = (await session.execute(select(Incident).where(Incident.recovery_verified_at.is_(None),
                                                                 Incident.opened_at <= now - timedelta(seconds=quiet_s))
                                          .with_for_update(of=Incident, skip_locked=True))).unique().scalars().all()
            for inc in incs:
                ids = [s.id for s in inc.services]
                last = (await session.execute(select(Anomaly.detected_at).where(Anomaly.service_id.in_(ids))
                                              .order_by(Anomaly.detected_at.desc()).limit(1))).scalar_one_or_none()
                if last and last > now - timedelta(seconds=quiet_s):
                    continue
                if not all(self.probe_state.get(s.name, False) for s in inc.services):
                    continue
                inc.recovery_verified_at = now
                incsvc.timeline(session, inc, "recovery.verified",
                                f"recovery verified: services {', '.join(sorted(s.name for s in inc.services))} healthy "
                                f"and quiet for {quiet_s}s")
                verified.append(inc.id)
            await session.commit()
        for iid in verified:
            await emit(TOPIC_HEALTH, "recovery.verified", {"incident_id": iid}, source="recovery", key=str(iid))
        return verified

    # ------------------------------------------------------------ main
    async def run(self) -> None:
        start_http_server(self.s.worker_metrics_port)
        await ensure_topics(self.s.kafka_bootstrap_servers)
        pub = KafkaPublisher(self.s.kafka_bootstrap_servers)
        await pub.start()
        set_publisher(pub)
        await self.load_config()
        tasks = [asyncio.create_task(c()) for c in (self.probe_loop, self.detection_loop, self.correlator, self.bridge,
                                                     self.recovery_loop, self.refresh_graph, self.settings_listener)]
        log.info("worker started", extra={"tasks": len(tasks)})
        await self.stop.wait()
        for t in tasks:
            t.cancel()
        await pub.stop()
        await self.tel.aclose()


def main() -> None:
    settings = get_settings()
    configure_logging("worker", settings.log_level)
    init_engine(settings.database_url, pool_size=5)
    init_redis(settings.redis_url)
    w = Worker(settings)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, w.stop.set)
            except NotImplementedError:
                pass
        await w.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
