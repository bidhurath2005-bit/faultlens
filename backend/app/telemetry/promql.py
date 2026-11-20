"""PromQL catalogue. Single place that knows the metric names emitted by the services
(services/common/faultlens_common/telemetry.py) after Collector -> Prometheus conversion.

Each feature is a `sum/avg by (service_name)` expression so one query covers all services.
`{w}` is the rate window, `{svc}` an optional service_name matcher.
"""
from __future__ import annotations

FEATURES: dict[str, str] = {
    "rps": 'sum by (service_name) (rate(faultlens_http_server_requests_total{{{svc}}}[{w}]))',
    # `or 0 * total` yields an explicit 0 for services that produced no 5xx series at all.
    "error_rate": (
        '(sum by (service_name) (rate(faultlens_http_server_requests_total{{status_class="5xx"{svc_c}}}[{w}]))'
        ' or 0 * sum by (service_name) (rate(faultlens_http_server_requests_total{{{svc}}}[{w}])))'
        ' / clamp_min(sum by (service_name) (rate(faultlens_http_server_requests_total{{{svc}}}[{w}])), 1e-9)'
    ),
    "latency_p50": 'histogram_quantile(0.50, sum by (service_name, le) (rate(faultlens_http_server_duration_seconds_bucket{{{svc}}}[{w}])))',
    "latency_p95": 'histogram_quantile(0.95, sum by (service_name, le) (rate(faultlens_http_server_duration_seconds_bucket{{{svc}}}[{w}])))',
    "latency_p99": 'histogram_quantile(0.99, sum by (service_name, le) (rate(faultlens_http_server_duration_seconds_bucket{{{svc}}}[{w}])))',
    "latency_avg": (
        'sum by (service_name) (rate(faultlens_http_server_duration_seconds_sum{{{svc}}}[{w}]))'
        ' / clamp_min(sum by (service_name) (rate(faultlens_http_server_duration_seconds_count{{{svc}}}[{w}])), 1e-9)'
    ),
    "cpu": 'avg by (service_name) (avg_over_time(faultlens_process_cpu_utilization_ratio{{{svc}}}[{w}]))',
    "memory_rss": 'avg by (service_name) (avg_over_time(faultlens_process_memory_bytes{{kind="rss"{svc_c}}}[{w}]))',
    "memory_util": (
        'avg by (service_name) (avg_over_time(faultlens_process_memory_bytes{{kind="rss"{svc_c}}}[{w}]))'
        ' / on (service_name) avg by (service_name) (faultlens_process_memory_bytes{{kind="limit"{svc_c}}})'
    ),
    "db_pool_util": (
        'max by (service_name) (max_over_time(faultlens_db_pool_connections_ratio{{state="in_use"{svc_c}}}[{w}]))'
        ' / on (service_name) max by (service_name) (faultlens_db_pool_connections_ratio{{state="max"{svc_c}}})'
    ),
    "db_pool_wait_p95": 'histogram_quantile(0.95, sum by (service_name, le) (rate(faultlens_db_pool_wait_seconds_bucket{{{svc}}}[{w}])))',
    "db_pool_timeouts": 'sum by (service_name) (rate(faultlens_db_pool_timeouts_total{{{svc}}}[{w}]))',
    "db_query_p95": 'histogram_quantile(0.95, sum by (service_name, le) (rate(faultlens_db_query_duration_seconds_bucket{{{svc}}}[{w}])))',
    "availability": 'avg by (service_name) (avg_over_time(faultlens_probe_up{{{svc}}}[{w}]))',
}

# Client-side view: calls from `service_name` to `peer_service`, split by outcome.
CLIENT_ERRORS_BY_PEER = (
    'sum by (service_name, peer_service) (rate(faultlens_http_client_duration_seconds_count{{outcome!="ok"}}[{w}]))'
)
CLIENT_CALLS_BY_PEER = 'sum by (service_name, peer_service) (rate(faultlens_http_client_duration_seconds_count[{w}]))'

# Dependency edges observed from trace span pairs (Collector servicegraph connector).
EDGE_RPS = 'sum by (client, server) (rate(traces_service_graph_request_total[{w}]))'
EDGE_FAILED = 'sum by (client, server) (rate(traces_service_graph_request_failed_total[{w}]))'
EDGE_P95 = ('histogram_quantile(0.95, sum by (client, server, le) '
            '(rate(traces_service_graph_request_server_seconds_bucket[{w}])))')

STATUS_BREAKDOWN = 'sum by (service_name, status_class) (rate(faultlens_http_server_requests_total{{{svc}}}[{w}]))'


def feature_query(name: str, window: str = "1m", service: str | None = None) -> str:
    svc = f'service_name="{service}"' if service else ""
    return FEATURES[name].format(w=window, svc=svc, svc_c=("," + svc) if svc else "")


def validate_service_name(name: str) -> str:
    """Guard against PromQL/LogQL injection: service names are restricted to a safe charset."""
    import re

    if not re.fullmatch(r"[a-z][a-z0-9-]{1,62}", name):
        raise ValueError("invalid service name")
    return name
