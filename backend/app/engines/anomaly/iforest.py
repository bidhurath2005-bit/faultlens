"""Multivariate Isolation Forest over baseline-normalised feature vectors.

Each service's raw features are normalised against *that service's own* baseline
(robust z-units), so one model shape works across services with different scales.
Two modes:
  * live: fit on the service's own recent baseline vectors (when enough points exist);
  * fallback: a model pre-trained on the labelled synthetic dataset (app/ml/train.py).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest

from .detectors import METRIC_SPECS, Detection, robust_stats

log = logging.getLogger("faultlens.iforest")

IF_FEATURES = ["latency_p95", "error_rate", "rps", "cpu", "memory_rss", "db_pool_util", "db_query_p95"]
MODEL_PATH = Path(__file__).resolve().parents[2] / "ml" / "artifacts" / "iforest_synthetic.joblib"
CLIP = 25.0


def normalise(matrix: np.ndarray, baseline: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (normalised matrix, medians, scales). Columns follow IF_FEATURES."""
    meds, scales = [], []
    for j, name in enumerate(IF_FEATURES):
        med, scale = robust_stats(baseline[:, j], METRIC_SPECS[name].noise_floor)
        meds.append(med)
        scales.append(scale)
    meds_a, scales_a = np.array(meds), np.array(scales)
    return np.clip((matrix - meds_a) / scales_a, -CLIP, CLIP), meds_a, scales_a


def augment(z: np.ndarray) -> np.ndarray:
    """Append aggregate deviation features (max |z|, mean |z|).

    Axis-parallel isolation trees rarely split on the single dimension that deviates when only one of
    seven features is anomalous, so such points were barely separable from the normal tail. Measured on
    the synthetic set this raised single-feature anomaly scores from ~-0.025 to ~-0.10 (normal p1 -0.087).
    See BENCHMARKS.md."""
    z2 = np.atleast_2d(z)
    a = np.abs(z2)
    return np.column_stack([z2, a.max(axis=1), a.mean(axis=1)])


def build_model(seed: int = 7, contamination: float | str = "auto") -> IsolationForest:
    return IsolationForest(n_estimators=100, max_samples="auto", contamination=contamination, random_state=seed)


class IsolationForestDetector:
    def __init__(self, threshold: float = -0.09, refit_every_s: int = 300, clock=time.time) -> None:  # noqa: ANN001
        self.threshold = threshold
        self.clock = clock
        self.refit_every_s = refit_every_s
        self._live: dict[str, tuple[float, IsolationForest]] = {}
        self._fallback: IsolationForest | None = None
        if MODEL_PATH.exists():
            try:
                self._fallback = joblib.load(MODEL_PATH)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not load pre-trained model", extra={"error": str(exc)})

    def _model_for(self, service: str, baseline_z: np.ndarray) -> tuple[IsolationForest | None, str]:
        now = self.clock()
        cached = self._live.get(service)
        if cached and now - cached[0] < self.refit_every_s:
            return cached[1], "live"
        if len(baseline_z) >= 40:
            m = build_model().fit(augment(baseline_z))
            self._live[service] = (now, m)
            return m, "live"
        return self._fallback, "pretrained"

    def detect(self, service: str, baseline: np.ndarray, current: np.ndarray) -> Detection | None:
        """baseline: (n, F) raw vectors; current: (F,) raw vector. Score = -decision_function (higher = worse)."""
        if baseline.shape[0] < 10:
            return None
        base_z, meds, scales = normalise(baseline, baseline)
        cur_z = np.clip((current - meds) / scales, -CLIP, CLIP)
        model, mode = self._model_for(service, base_z)
        if model is None:
            return None
        decision = float(model.decision_function(augment(cur_z))[0])
        if decision >= self.threshold:
            return None
        # Require at least one feature to move by an operationally meaningful amount in its bad direction.
        contrib = []
        for j, name in enumerate(IF_FEATURES):
            spec = METRIC_SPECS[name]
            delta = current[j] - meds[j]
            bad = delta >= spec.min_delta if spec.direction == "up" else abs(delta) >= spec.min_delta
            contrib.append((name, float(cur_z[j]), bool(bad), float(current[j]), float(meds[j])))
        meaningful = [c for c in contrib if c[2]]
        if not meaningful:
            return None
        top = max(meaningful, key=lambda c: abs(c[1]))
        return Detection(
            metric=top[0], method="isolation_forest", score=round(-decision, 4), observed=top[3], baseline=top[4],
            details={"decision_function": round(decision, 4), "model": mode,
                     "contributions": [{"feature": c[0], "z": round(c[1], 2)} for c in
                                       sorted(contrib, key=lambda c: -abs(c[1]))[:4]]},
        )
