"""OpenTelemetry bootstrap: traces, metrics and logs exported over OTLP to the Collector.

Metric names defined here are the contract with the FaultLens backend's PromQL queries
(see backend/app/telemetry/promql.py). The Collector's Prometheus exporter converts
`faultlens.http.server.duration` (unit s) into `faultlens_http_server_duration_seconds_*`.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import psutil
from opentelemetry import metrics, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from .config import ServiceSettings

_LATENCY_BUCKETS = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0]

log = logging.getLogger(__name__)


@dataclass
class Instruments:
    http_server_duration: metrics.Histogram
    http_server_requests: metrics.Counter
    http_client_duration: metrics.Histogram
    db_query_duration: metrics.Histogram
    db_pool_wait: metrics.Histogram
    db_pool_timeouts: metrics.Counter


_instruments: Instruments | None = None
_gauge_sources: dict[str, Callable[[], Iterable[Observation]]] = {}


def instruments() -> Instruments:
    if _instruments is None:
        raise RuntimeError("telemetry not initialised")
    return _instruments


def register_gauge_source(name: str, fn: Callable[[], Iterable[Observation]]) -> None:
    _gauge_sources[name] = fn


def _cgroup_memory_limit() -> int | None:
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path).read().strip()
            if raw != "max" and int(raw) < 1 << 60:
                return int(raw)
        except (OSError, ValueError):
            continue
    return None


class _ProcessStats:
    def __init__(self) -> None:
        self.proc = psutil.Process(os.getpid())
        self.proc.cpu_percent(None)
        self.mem_limit = _cgroup_memory_limit() or psutil.virtual_memory().total

    def cpu(self, _: CallbackOptions) -> Iterable[Observation]:
        # Fraction of one CPU core used since the last collection (can exceed 1.0).
        yield Observation(self.proc.cpu_percent(None) / 100.0)

    def memory(self, _: CallbackOptions) -> Iterable[Observation]:
        yield Observation(self.proc.memory_info().rss, {"kind": "rss"})
        yield Observation(self.mem_limit, {"kind": "limit"})


def setup_telemetry(settings: ServiceSettings) -> Instruments:
    global _instruments
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "service.version": settings.service_version,
            "deployment.environment": settings.environment,
            "service.namespace": "faultlens-demo",
        }
    )
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

    tracer_provider = TracerProvider(resource=resource)
    readers = []
    if settings.otel_enabled:
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True))
        )
        readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True),
                export_interval_millis=settings.otel_export_interval_ms,
            )
        )
    trace.set_tracer_provider(tracer_provider)

    views = [
        View(instrument_name="faultlens.*duration", aggregation=ExplicitBucketHistogramAggregation(_LATENCY_BUCKETS)),
        View(instrument_name="faultlens.db.pool.wait", aggregation=ExplicitBucketHistogramAggregation(_LATENCY_BUCKETS)),
    ]
    meter_provider = MeterProvider(resource=resource, metric_readers=readers, views=views)
    metrics.set_meter_provider(meter_provider)

    if settings.otel_enabled:
        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=settings.otel_exporter_otlp_endpoint, insecure=True))
        )
        # The OTLP handler attaches trace_id/span_id from the active context automatically.
        logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

    meter = metrics.get_meter("faultlens.demo")
    stats = _ProcessStats()
    meter.create_observable_gauge("faultlens.process.cpu.utilization", [stats.cpu], unit="1",
                                  description="Fraction of one CPU core used by the service process")
    meter.create_observable_gauge("faultlens.process.memory", [stats.memory], unit="By",
                                  description="Process RSS and container memory limit")

    def _gauges(_: CallbackOptions) -> Iterable[Observation]:
        for fn in list(_gauge_sources.values()):
            yield from fn()

    meter.create_observable_gauge("faultlens.db.pool.connections", [_gauges], unit="1",
                                  description="DB connection pool state (state=in_use|max)")

    _instruments = Instruments(
        http_server_duration=meter.create_histogram("faultlens.http.server.duration", unit="s"),
        http_server_requests=meter.create_counter("faultlens.http.server.requests", unit="1"),
        http_client_duration=meter.create_histogram("faultlens.http.client.duration", unit="s"),
        db_query_duration=meter.create_histogram("faultlens.db.query.duration", unit="s"),
        db_pool_wait=meter.create_histogram("faultlens.db.pool.wait", unit="s"),
        db_pool_timeouts=meter.create_counter("faultlens.db.pool.timeouts", unit="1"),
    )
    log.info("telemetry initialised", extra={"otlp_endpoint": settings.otel_exporter_otlp_endpoint})
    return _instruments


def monotonic() -> float:
    return time.perf_counter()
