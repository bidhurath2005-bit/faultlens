import numpy as np
import pytest

from app.engines.anomaly.detectors import (
    DEFAULT_CONFIG,
    moving_average_detect,
    robust_stats,
    severity_for,
    threshold_detect,
    zscore_detect,
)
from app.engines.anomaly.iforest import IF_FEATURES, IsolationForestDetector
from app.engines.anomaly.pipeline import ServiceFeatures, detect_service

RNG = np.random.default_rng(0)
LAT = RNG.normal(0.05, 0.005, 100)


def test_robust_stats_ignores_outliers_and_floors_scale():
    med, scale = robust_stats(np.array([1.0] * 50 + [1000.0]), noise_floor=0.01)
    assert med == 1.0 and scale >= 0.05  # MAD=0 -> floored at 5% of median


def test_threshold_breach_and_floor_metric():
    t = DEFAULT_CONFIG["thresholds"]
    assert threshold_detect("latency_p95", 2.0, t).score == pytest.approx(2.0)
    assert threshold_detect("latency_p95", 0.2, t) is None
    assert threshold_detect("availability", 0.5, t) is not None  # downward metric
    assert threshold_detect("availability", 1.0, t) is None
    assert threshold_detect("rps", 999, t) is None  # no static threshold for traffic


def test_zscore_detects_spike_but_not_noise_or_tiny_shift():
    assert zscore_detect("latency_p95", LAT, 1.5, 4.0).score > 4
    assert zscore_detect("latency_p95", LAT, 0.055, 4.0) is None
    # Statistically extreme but operationally tiny (below min_delta of 150 ms): not reported.
    flat = np.full(100, 0.010)
    assert zscore_detect("latency_p95", flat, 0.05, 4.0) is None


def test_zscore_direction_awareness():
    rps = RNG.normal(10, 0.5, 100)
    assert zscore_detect("rps", rps, 0.1, 4.0) is not None       # traffic drop ("both")
    avail = np.full(50, 0.6)
    assert zscore_detect("availability", np.ones(50), 0.2, 4.0) is not None  # availability drop: direction=down
    assert zscore_detect("availability", avail, 1.0, 4.0) is None             # availability *rise* is never an anomaly


def test_ewma_detects_onset_and_ignores_return_to_normal():
    assert moving_average_detect("latency_p95", LAT, 1.0, 0.1, 2.0) is not None
    # Spike just ended: the recent history (and so the EWMA) is elevated, but the long-term median is normal.
    after_spike = np.concatenate([np.full(100, 10.0), np.full(20, 50.0)])
    assert moving_average_detect("rps", after_spike, 10.0, 0.1, 2.0) is None


def test_severity_mapping():
    from app.engines.anomaly.detectors import Detection
    assert severity_for(Detection("error_rate", "threshold", 7, 0.5, 0.05)) == "critical"
    assert severity_for(Detection("availability", "threshold", 3, 0.2, 0.9)) == "critical"
    assert severity_for(Detection("latency_p95", "zscore", 9, 1.2, 0.05)) == "high"


def _features(cur_overrides: dict) -> ServiceFeatures:
    base = {"latency_p95": 0.05, "error_rate": 0.0, "rps": 5.0, "cpu": 0.1, "memory_rss": 110e6, "db_pool_util": 0.2,
            "db_query_p95": 0.005, "memory_util": 0.2, "availability": 1.0}
    hist = {k: np.abs(RNG.normal(v, abs(v) * 0.05 + 1e-4, 80)) for k, v in base.items()}
    return ServiceFeatures("payment-service", base | cur_overrides, hist)


def test_detect_service_combines_methods():
    dets = detect_service(_features({"latency_p95": 2.0, "error_rate": 0.5}), DEFAULT_CONFIG, IsolationForestDetector())
    methods = {d.method for d in dets}
    assert {"threshold", "zscore"} <= methods
    assert {d.metric for d in dets} >= {"latency_p95", "error_rate"}


def test_detect_service_quiet_on_normal_data():
    assert detect_service(_features({}), DEFAULT_CONFIG, IsolationForestDetector()) == []


def test_detect_service_respects_enabled_methods_and_min_baseline():
    cfg = DEFAULT_CONFIG | {"enabled_methods": ["threshold"]}
    dets = detect_service(_features({"latency_p95": 2.0}), cfg, None)
    assert {d.method for d in dets} == {"threshold"}
    sf = _features({"latency_p95": 2.0})
    sf.history = {k: v[:5] for k, v in sf.history.items()}
    assert {d.method for d in detect_service(sf, DEFAULT_CONFIG, None)} == {"threshold"}


def test_isolation_forest_flags_multivariate_deviation_with_contributions():
    det = IsolationForestDetector(threshold=-0.09)
    sf = _features({"latency_p95": 2.0, "error_rate": 0.9, "db_pool_util": 1.0})
    d = det.detect("payment-service", sf.baseline_matrix(), sf.vector())
    assert d is not None and d.method == "isolation_forest"
    assert d.details["contributions"][0]["feature"] in IF_FEATURES
    assert det.detect("payment-service", sf.baseline_matrix(), _features({}).vector()) is None
