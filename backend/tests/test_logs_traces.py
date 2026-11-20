from datetime import UTC, datetime

from app.engines.logs import classify, peer_mentions, summarise, template_of, with_excess
from app.engines.traces import analyse_trace, span_category, summarise_traces
from app.telemetry.clients import LogRecord, Span, _id_to_hex, parse_otlp_trace

from .conftest import TRACE_ID, canned_trace

KNOWN = {"order-service", "payment-service", "inventory-service", "user-service", "notification-service"}


def rec(svc, msg, level="ERROR", **attrs):  # noqa: ANN001, ANN201
    return LogRecord(datetime.now(UTC), svc, level, msg, None, None, None, {k: str(v) for k, v in attrs.items()})


def test_template_masks_ids_and_numbers():
    t = template_of("order 3fa85f64-5717-4562-b3fc-2c963f66afa6 failed after 2001.5 ms trace 4bf92f3577b34da6a3ce929d0e0e4736")
    assert t == "order <uuid> failed after <n> ms trace <hex>"


def test_classification_signatures():
    assert "pool_exhausted" in classify(rec("p", "database connection pool exhausted: QueuePool limit reached"))
    assert "timeout" in classify(rec("o", "downstream call timed out", peer_service="payment-service"))
    assert "request_5xx" in classify(rec("o", "request completed", status_code=503))
    assert classify(rec("o", "request completed", "INFO", status_code=201)) == []


def test_excess_subtracts_baseline_rate_and_peer_attribution():
    window = summarise([rec("p", "slow query detected", "WARNING")] * 10 + [rec("o", "downstream call failed", peer_service="payment-service")] * 4)
    baseline = summarise([rec("p", "slow query detected", "WARNING")] * 20)  # 20 in a 20-min baseline = 1/min
    with_excess(window, baseline, window_minutes=3, baseline_minutes=20)
    assert window["p"].excess["slow_query"] == 10 - 3
    assert peer_mentions(window, "payment-service", {"downstream_failed"}) == 4


def test_tempo_ids_decoded_from_base64_and_hex():
    assert _id_to_hex("S/kvNXezTaajzpKdDg5HNg==", 16) == TRACE_ID
    assert _id_to_hex(TRACE_ID, 16) == TRACE_ID
    assert _id_to_hex("garbage!!", 16) is None


def test_parse_and_analyse_trace_pool_exhaustion():
    spans = parse_otlp_trace(canned_trace())
    assert len(spans) == 5 and all(s.trace_id == TRACE_ID for s in spans)
    by_name = {s.name: s for s in spans}
    assert span_category(by_name["connect"]) == "db_pool"  # DB connect beneath pool acquire = pool wait
    a = analyse_trace(spans, KNOWN)
    assert a.has_error
    origins = {(o["service"], o["reason"]) for o in a.error_origins}
    assert origins == {("payment-service", "db_pool")}  # deepest error: in the payment service's pool
    assert a.self_time_ms["payment-service"]["db_pool"] > 1900
    summary = summarise_traces([a], [a])
    assert summary.error_origin_share == {"payment-service": 1.0}
    assert summary.latency_breakdown["payment-service"]["db_pool"] > 0.9


def _span(sid, parent, svc, kind, start, dur, status="unset", **attrs):  # noqa: ANN001, ANN202
    return Span("t" * 32, sid, parent, svc, f"{kind}-{sid}", kind, start * 10**6, (start + dur) * 10**6, status, None, attrs)


def test_unreachable_peer_attributed_to_peer_and_4xx_not_an_origin():
    spans = [_span("1", None, "order-service", "server", 0, 100, "error"),
             _span("2", "1", "order-service", "client", 5, 90, "error", **{"server.address": "inventory-service"})]
    a = analyse_trace(spans, KNOWN)
    assert a.error_origins[0]["service"] == "inventory-service" and a.error_origins[0]["reason"] == "no_response"
    assert a.self_time_ms["inventory-service"]["no_response"] == 90
    business = [_span("1", None, "order-service", "server", 0, 50),
                _span("2", "1", "order-service", "client", 5, 40, "error", **{"server.address": "payment-service"}),
                _span("3", "2", "payment-service", "server", 6, 38)]  # 402 decline: server span not an error
    assert analyse_trace(business, KNOWN).error_origins == []


def test_peer_timeout_counted():
    s = _span("2", "1", "order-service", "client", 0, 3000, "error", **{"server.address": "payment-service"})
    s.status_message = "ReadTimeout: timed out"
    spans = [_span("1", None, "order-service", "server", 0, 3010, "error"), s, _span("3", "2", "payment-service", "server", 1, 3500)]
    assert analyse_trace(spans, KNOWN).peer_timeouts["payment-service"] == 1
