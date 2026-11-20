"""Redis: query cache, rate limiting, leader election, de-duplication and Pub/Sub fan-out."""
from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio import Redis

log = logging.getLogger("faultlens.redis")
_redis: Redis | None = None

EVENTS_CHANNEL = "faultlens:events"


def init_redis(url: str) -> Redis:
    global _redis
    _redis = Redis.from_url(url, decode_responses=True, socket_timeout=5, health_check_interval=30)
    return _redis


def get_redis() -> Redis:
    if _redis is None:
        raise RuntimeError("redis not initialised")
    return _redis


def set_redis(r: Redis | None) -> None:  # used by tests
    global _redis
    _redis = r


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def cached(key: str, ttl_s: int, producer: Callable[[], Awaitable[Any]]) -> Any:
    """Read-through cache. Failures of Redis never fail the request (cache is an optimisation)."""
    r = _redis
    if r is not None:
        try:
            hit = await r.get(key)
            if hit is not None:
                return json.loads(hit)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache read failed", extra={"error": str(exc)})
    value = await producer()
    if r is not None:
        try:
            await r.set(key, json.dumps(value, default=str), ex=ttl_s)
        except Exception as exc:  # noqa: BLE001
            log.warning("cache write failed", extra={"error": str(exc)})
    return value


async def rate_limit(key: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Fixed-window counter. Returns (allowed, retry_after_s). Fails open if Redis is down."""
    r = _redis
    if r is None:
        return True, 0
    bucket = int(time.time() // window_s)
    k = f"rl:{key}:{bucket}"
    try:
        n = await r.incr(k)
        if n == 1:
            await r.expire(k, window_s + 1)
    except Exception:  # noqa: BLE001
        return True, 0
    return n <= limit, window_s - int(time.time() % window_s)


class LeaderLock:
    """Single-leader lease so only one worker replica runs periodic detection."""

    def __init__(self, redis: Redis, name: str, ttl_s: int = 30) -> None:
        self.redis, self.key, self.ttl = redis, f"leader:{name}", ttl_s
        self.token = uuid.uuid4().hex

    async def acquire_or_renew(self) -> bool:
        if await self.redis.set(self.key, self.token, nx=True, ex=self.ttl):
            return True
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
        return bool(await self.redis.eval(script, 1, self.key, self.token, self.ttl))


async def subscribe(channel: str):  # noqa: ANN201
    """Async iterator over Pub/Sub message payloads. Polls with a short timeout so idle channels never trip
    the client's socket timeout (blocking listen() would)."""
    pubsub = get_redis().pubsub(ignore_subscribe_messages=True)
    await pubsub.subscribe(channel)
    try:
        while True:
            msg = await pubsub.get_message(timeout=1.0)
            if msg is not None and msg.get("type") == "message":
                yield msg["data"]
    finally:
        await pubsub.aclose()


async def publish_event(event: dict) -> None:
    await get_redis().publish(EVENTS_CHANNEL, json.dumps(event, default=str))
