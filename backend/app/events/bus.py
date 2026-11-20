"""Kafka event bus.

Topics (created explicitly; broker auto-creation is disabled):
  faultlens.anomalies  - anomaly.detected                     (producer: detector; consumer: correlator, bridge)
  faultlens.incidents  - incident.*, investigation.completed  (producers: correlator, API; consumer: bridge)
  faultlens.faults     - fault.injected / fault.cleared       (producer: API; consumer: bridge)
  faultlens.health     - service.health_changed, recovery.*   (producer: prober/recovery; consumer: bridge)
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

log = logging.getLogger("faultlens.events")

TOPIC_ANOMALIES = "faultlens.anomalies"
TOPIC_INCIDENTS = "faultlens.incidents"
TOPIC_FAULTS = "faultlens.faults"
TOPIC_HEALTH = "faultlens.health"
ALL_TOPICS = [TOPIC_ANOMALIES, TOPIC_INCIDENTS, TOPIC_FAULTS, TOPIC_HEALTH]


def make_event(event_type: str, data: dict[str, Any], *, source: str) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "type": event_type, "occurred_at": datetime.now(UTC).isoformat(),
            "source": source, "data": data}


class EventPublisher(Protocol):
    async def publish(self, topic: str, event: dict[str, Any], key: str | None = None) -> None: ...


class KafkaPublisher:
    def __init__(self, bootstrap: str) -> None:
        self.bootstrap = bootstrap
        self._producer = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        for attempt in range(20):
            try:
                self._producer = AIOKafkaProducer(bootstrap_servers=self.bootstrap, acks="all", enable_idempotence=True,
                                                  value_serializer=lambda v: json.dumps(v, default=str).encode(),
                                                  key_serializer=lambda k: k.encode() if k else None)
                await self._producer.start()
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("kafka producer not ready", extra={"attempt": attempt, "error": str(exc)})
                await asyncio.sleep(3)
        raise RuntimeError("kafka unavailable")

    async def stop(self) -> None:
        if self._producer:
            await self._producer.stop()

    async def publish(self, topic: str, event: dict[str, Any], key: str | None = None) -> None:
        if self._producer is None:
            raise RuntimeError("producer not started")
        await self._producer.send_and_wait(topic, event, key=key)


class InMemoryPublisher:
    """Used by unit tests and when KAFKA_ENABLED=false."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, topic: str, event: dict[str, Any], key: str | None = None) -> None:
        self.events.append((topic, event))


async def ensure_topics(bootstrap: str) -> None:
    from aiokafka.admin import AIOKafkaAdminClient, NewTopic
    from aiokafka.errors import TopicAlreadyExistsError

    for attempt in range(20):
        admin = AIOKafkaAdminClient(bootstrap_servers=bootstrap)
        try:
            await admin.start()
            existing = set(await admin.list_topics())
            missing = [NewTopic(t, num_partitions=3, replication_factor=1) for t in ALL_TOPICS if t not in existing]
            if missing:
                try:
                    await admin.create_topics(missing)
                except TopicAlreadyExistsError:
                    pass
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("kafka admin not ready", extra={"attempt": attempt, "error": str(exc)})
            await asyncio.sleep(3)
        finally:
            await admin.close()
    raise RuntimeError("could not ensure kafka topics")


_publisher: EventPublisher | None = None


def set_publisher(p: EventPublisher | None) -> None:
    global _publisher
    _publisher = p


def get_publisher() -> EventPublisher:
    if _publisher is None:
        raise RuntimeError("event publisher not initialised")
    return _publisher


async def emit(topic: str, event_type: str, data: dict[str, Any], *, source: str, key: str | None = None) -> None:
    """Publish an event; failures are logged, not raised, so a Kafka outage cannot break user actions
    whose state is already committed in PostgreSQL (the database remains the source of truth)."""
    try:
        await get_publisher().publish(topic, make_event(event_type, data, source=source), key=key)
    except Exception as exc:  # noqa: BLE001
        log.error("failed to publish event", extra={"event_type": event_type, "topic": topic, "error": str(exc)})
