"""Structured JSON logging with OpenTelemetry trace context.

Every record carries service, level, trace_id, span_id and request_id when available.
trace_id/span_id are taken from the *active* OpenTelemetry span; if there is no valid
span context the fields are omitted rather than invented.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import UTC, datetime

from opentelemetry import trace

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_SENSITIVE_KEYS = re.compile(r"(password|passwd|secret|token|authorization|card_number|cvv|api_key)", re.I)
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def redact(value: object) -> object:
    """Recursively redact values whose key looks sensitive."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SENSITIVE_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def current_trace_context() -> tuple[str | None, str | None]:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "service": self.service_name,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        trace_id, span_id = current_trace_context()
        if trace_id:
            payload["trace_id"] = trace_id
            payload["span_id"] = span_id
        rid = request_id_var.get()
        if rid:
            payload["request_id"] = rid
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}
        if extras:
            payload.update(redact(extras))  # type: ignore[arg-type]
        if record.exc_info:
            payload["error"] = {
                "type": record.exc_info[0].__name__ if record.exc_info[0] else None,
                "detail": str(record.exc_info[1]),
                "stack": self.formatException(record.exc_info)[-2000:],
            }
        return json.dumps(payload, default=str)


class RequestContextFilter(logging.Filter):
    """Adds request_id as a record attribute so the OTLP log exporter ships it to Loki."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        if rid and not hasattr(record, "request_id"):
            record.request_id = rid
        return True


def configure_logging(service_name: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    handler.addFilter(RequestContextFilter())
    root.addHandler(handler)
    # uvicorn access logs duplicate our request logs; keep them quiet.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("httpx", "httpcore", "aiokafka", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
