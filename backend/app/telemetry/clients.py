"""Thin async clients for Prometheus, Loki and Tempo HTTP APIs."""
from __future__ import annotations

import base64
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from app.core.errors import ServiceUnavailable

log = logging.getLogger("faultlens.telemetry")

_HEX32 = re.compile(r"^[0-9a-f]{32}$")
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _f(v: str) -> float | None:
    x = float(v)
    return None if math.isnan(x) or math.isinf(x) else x


class PrometheusClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base, self.http = base_url.rstrip("/"), client

    async def _get(self, path: str, params: dict) -> Any:
        try:
            r = await self.http.get(f"{self.base}{path}", params=params)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(f"prometheus query failed: {type(exc).__name__}") from exc
        body = r.json()
        if body.get("status") != "success":
            raise ServiceUnavailable(f"prometheus error: {body.get('error')}")
        return body["data"]

    async def instant(self, query: str, at: datetime | None = None) -> list[dict]:
        params = {"query": query}
        if at:
            params["time"] = at.timestamp()
        return (await self._get("/api/v1/query", params))["result"]

    async def instant_by(self, query: str, label: str = "service_name", at: datetime | None = None) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in await self.instant(query, at):
            v = _f(s["value"][1])
            if v is not None and (key := s["metric"].get(label)):
                out[key] = v
        return out

    async def range(self, query: str, start: datetime, end: datetime, step_s: int) -> list[dict]:
        params = {"query": query, "start": start.timestamp(), "end": end.timestamp(), "step": step_s}
        return (await self._get("/api/v1/query_range", params))["result"]

    async def range_by(self, query: str, start: datetime, end: datetime, step_s: int,
                       label: str = "service_name") -> dict[str, list[tuple[float, float]]]:
        out: dict[str, list[tuple[float, float]]] = {}
        for s in await self.range(query, start, end, step_s):
            key = s["metric"].get(label)
            if not key:
                continue
            pts = [(float(t), v) for t, raw in s["values"] if (v := _f(raw)) is not None]
            out[key] = pts
        return out

    async def ready(self) -> bool:
        try:
            r = await self.http.get(f"{self.base}/-/ready")
            return r.status_code == 200
        except httpx.HTTPError:
            return False


@dataclass
class LogRecord:
    timestamp: datetime
    service: str
    level: str
    message: str
    trace_id: str | None
    span_id: str | None
    request_id: str | None
    attributes: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"timestamp": self.timestamp.isoformat(), "service": self.service, "level": self.level,
                "message": self.message, "trace_id": self.trace_id, "span_id": self.span_id,
                "request_id": self.request_id, "attributes": self.attributes}


_LOKI_INTERNAL = {"service_name", "service_namespace", "service_version", "deployment_environment", "detected_level",
                  "severity_text", "severity_number", "trace_id", "span_id", "request_id", "observed_timestamp",
                  "flags", "telemetry_sdk_language", "telemetry_sdk_name", "telemetry_sdk_version", "scope_name",
                  "code_filepath", "code_function", "code_lineno"}


def _norm_level(level: str) -> str:
    level = (level or "INFO").upper()
    return {"WARN": "WARNING", "ERR": "ERROR", "FATAL": "CRITICAL"}.get(level, level)


class LokiClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base, self.http = base_url.rstrip("/"), client

    async def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 200,
                          direction: str = "backward") -> list[LogRecord]:
        params = {"query": logql, "start": int(start.timestamp() * 1e9), "end": int(end.timestamp() * 1e9),
                  "limit": limit, "direction": direction}
        try:
            r = await self.http.get(f"{self.base}/loki/api/v1/query_range", params=params)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(f"loki query failed: {type(exc).__name__}") from exc
        records: list[LogRecord] = []
        for stream in r.json().get("data", {}).get("result", []):
            labels = stream.get("stream") or {}
            for ts, line in stream.get("values", []):
                tid = labels.get("trace_id")
                sid = labels.get("span_id")
                records.append(LogRecord(
                    timestamp=datetime.fromtimestamp(int(ts) / 1e9, tz=UTC),
                    service=labels.get("service_name", "unknown"),
                    level=_norm_level(labels.get("severity_text") or labels.get("detected_level", "")),
                    message=line,
                    # Only expose ids that are well-formed; never fabricate them.
                    trace_id=tid if tid and _HEX32.match(tid) and tid != "0" * 32 else None,
                    span_id=sid if sid and _HEX16.match(sid) else None,
                    request_id=labels.get("request_id"),
                    attributes={k: v for k, v in labels.items() if k not in _LOKI_INTERNAL},
                ))
        records.sort(key=lambda x: x.timestamp, reverse=(direction == "backward"))
        return records[:limit]

    async def metric(self, logql: str, at: datetime) -> list[dict]:
        try:
            r = await self.http.get(f"{self.base}/loki/api/v1/query", params={"query": logql, "time": int(at.timestamp() * 1e9)})
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(f"loki metric query failed: {type(exc).__name__}") from exc
        return r.json()["data"]["result"]

    async def ready(self) -> bool:
        try:
            return (await self.http.get(f"{self.base}/ready")).status_code == 200
        except httpx.HTTPError:
            return False


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    service: str
    name: str
    kind: str
    start_ns: int
    end_ns: int
    status: str  # ok | error | unset
    status_message: str | None
    attributes: dict[str, Any]

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6

    def as_dict(self) -> dict[str, Any]:
        return {"trace_id": self.trace_id, "span_id": self.span_id, "parent_span_id": self.parent_span_id,
                "service": self.service, "name": self.name, "kind": self.kind,
                "start_time": datetime.fromtimestamp(self.start_ns / 1e9, tz=UTC).isoformat(),
                "start_ns": self.start_ns, "duration_ms": round(self.duration_ms, 3), "status": self.status,
                "status_message": self.status_message, "attributes": self.attributes}


def _id_to_hex(v: str | None, nbytes: int) -> str | None:
    """Tempo's v1 JSON returns ids base64-encoded (OTLP/JSON proto3 bytes); accept hex too."""
    if not v:
        return None
    if re.fullmatch(rf"[0-9a-fA-F]{{{nbytes * 2}}}", v):
        return v.lower()
    try:
        raw = base64.b64decode(v)
    except (ValueError, TypeError):
        return None
    return raw.hex() if len(raw) == nbytes else None


def _attr_value(v: dict) -> Any:
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in v:
            return int(v[k]) if k == "intValue" else v[k]
    return None


def parse_otlp_trace(payload: dict) -> list[Span]:
    batches = payload.get("batches") or payload.get("resourceSpans") or payload.get("trace", {}).get("resourceSpans") or []
    spans: list[Span] = []
    for rs in batches:
        rattrs = {a["key"]: _attr_value(a["value"]) for a in rs.get("resource", {}).get("attributes", [])}
        service = rattrs.get("service.name", "unknown")
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for s in ss.get("spans", []):
                st = s.get("status", {}) or {}
                code = st.get("code", "")
                status = "error" if code in ("STATUS_CODE_ERROR", 2) else "ok" if code in ("STATUS_CODE_OK", 1) else "unset"
                spans.append(Span(
                    trace_id=_id_to_hex(s.get("traceId"), 16) or "",
                    span_id=_id_to_hex(s.get("spanId"), 8) or "",
                    parent_span_id=_id_to_hex(s.get("parentSpanId"), 8),
                    service=service,
                    name=s.get("name", ""),
                    kind=str(s.get("kind", "SPAN_KIND_INTERNAL")).replace("SPAN_KIND_", "").lower(),
                    start_ns=int(s.get("startTimeUnixNano", 0)),
                    end_ns=int(s.get("endTimeUnixNano", 0)),
                    status=status,
                    status_message=st.get("message"),
                    attributes={a["key"]: _attr_value(a["value"]) for a in s.get("attributes", [])},
                ))
    spans.sort(key=lambda x: x.start_ns)
    return spans


class TempoClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient) -> None:
        self.base, self.http = base_url.rstrip("/"), client

    async def search(self, traceql: str, start: datetime, end: datetime, limit: int = 20) -> list[dict]:
        params = {"q": traceql, "start": int(start.timestamp()), "end": int(end.timestamp()) + 1, "limit": limit,
                  "spss": 10}
        try:
            r = await self.http.get(f"{self.base}/api/search", params=params)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(f"tempo search failed: {type(exc).__name__}") from exc
        out = []
        for t in r.json().get("traces", []):
            out.append({
                "trace_id": _id_to_hex(t.get("traceID", "").rjust(32, "0"), 16),
                "root_service": t.get("rootServiceName"),
                "root_name": t.get("rootTraceName"),
                "start_time": datetime.fromtimestamp(int(t.get("startTimeUnixNano", 0)) / 1e9, tz=UTC).isoformat(),
                "duration_ms": t.get("durationMs", 0),
                "span_count": sum(len(ss.get("spans", [])) for ss in t.get("spanSets", []) or []),
                "matched": sum(ss.get("matched", 0) for ss in t.get("spanSets", []) or []),
            })
        return out

    async def get_trace(self, trace_id: str) -> list[Span]:
        if not _HEX32.match(trace_id):
            raise ValueError("invalid trace id")
        try:
            r = await self.http.get(f"{self.base}/api/traces/{trace_id}", headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise ServiceUnavailable(f"tempo fetch failed: {type(exc).__name__}") from exc
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return parse_otlp_trace(r.json())

    async def ready(self) -> bool:
        try:
            return (await self.http.get(f"{self.base}/ready")).status_code == 200
        except httpx.HTTPError:
            return False


@dataclass
class Telemetry:
    prom: PrometheusClient
    loki: LokiClient
    tempo: TempoClient
    http: httpx.AsyncClient

    @classmethod
    def create(cls, prometheus_url: str, loki_url: str, tempo_url: str, timeout_s: float = 8.0) -> Telemetry:
        http = httpx.AsyncClient(timeout=timeout_s, limits=httpx.Limits(max_connections=50))
        return cls(PrometheusClient(prometheus_url, http), LokiClient(loki_url, http), TempoClient(tempo_url, http), http)

    async def aclose(self) -> None:
        await self.http.aclose()


_telemetry: Telemetry | None = None


def set_telemetry(t: Telemetry | None) -> None:
    global _telemetry
    _telemetry = t


def get_telemetry() -> Telemetry:
    if _telemetry is None:
        raise RuntimeError("telemetry clients not initialised")
    return _telemetry
