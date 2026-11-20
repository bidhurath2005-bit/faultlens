"""Structured JSON logging for the platform itself, with secret redaction."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_SENSITIVE = re.compile(r"(password|secret|token|authorization|cookie|api_key)", re.I)
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def _redact(v: object, key: str = "") -> object:
    if key and _SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(v, dict):
        return {k: _redact(x, str(k)) for k, x in v.items()}
    return v


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "service": f"faultlens-{self.component}",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _RESERVED and not k.startswith("_"):
                out[k] = _redact(v, k)
        if record.exc_info:
            out["error"] = {"type": getattr(record.exc_info[0], "__name__", None), "detail": str(record.exc_info[1])}
        return json.dumps(out, default=str)


def configure_logging(component: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter(component))
    root.addHandler(h)
    for noisy in ("httpx", "httpcore", "aiokafka", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
