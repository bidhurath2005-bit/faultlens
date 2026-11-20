"""Reproducible detector training + evaluation on the labelled SYNTHETIC dataset.

    python -m app.ml.evaluate [--persist]      (from backend/)

Writes app/ml/artifacts/iforest_synthetic.joblib (fallback model) and
app/ml/artifacts/anomaly_evaluation.json; with --persist and DATABASE_URL also stores
rows in model_evaluations. Detection is replayed causally: at step t each detector sees only
the baseline window [t-120, t-8) (30 min, 2 min gap - same as production) and the current point.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from collections import defaultdict
from pathlib import Path

import joblib
import numpy as np

from app.engines.anomaly.detectors import DEFAULT_CONFIG, moving_average_detect, threshold_detect, zscore_detect
from app.engines.anomaly.iforest import IF_FEATURES, IsolationForestDetector, augment, build_model, normalise

from .synthetic import DATASET_DESCRIPTION, FEATURES, dataset, make_scenario

ART = Path(__file__).resolve().parent / "artifacts"
BASELINE, GAP = 120, 8
METHODS = ["threshold", "zscore", "moving_average", "isolation_forest", "ensemble"]
STAT_METRICS = ["latency_p95", "error_rate", "rps", "cpu", "memory_rss", "db_query_p95"]


def train_fallback(seed: int = 7) -> Path:
    """Fit the fallback IF on baseline-normalised NORMAL synthetic windows (no anomalous points)."""
    vecs = []
    for s in (101, 102, 103, 104):
        sc = make_scenario("noisy_latency", s)  # unlabeled, normal-only scenario
        base = sc.data[:BASELINE]
        z, _, _ = normalise(sc.data[BASELINE:], base)
        vecs.append(z)
    X = np.vstack(vecs)
    model = build_model(seed).fit(augment(X))
    ART.mkdir(parents=True, exist_ok=True)
    path = ART / "iforest_synthetic.joblib"
    joblib.dump(model, path)
    return path


def _confirm(raw: np.ndarray, n: int) -> np.ndarray:
    """Apply the production confirmation rule: N consecutive positive cycles."""
    out = np.zeros_like(raw)
    streak = 0
    for i, v in enumerate(raw):
        streak = streak + 1 if v else 0
        out[i] = 1 if streak >= n else 0
    return out


def replay(sc, cfg: dict) -> dict[str, np.ndarray]:
    t_len = len(sc.data)
    preds = {m: np.zeros(t_len, dtype=int) for m in METHODS}
    step = {"t": 0}
    iforest = IsolationForestDetector(threshold=cfg["iforest_threshold"], refit_every_s=20, clock=lambda: step["t"])
    col = {f: i for i, f in enumerate(FEATURES)}
    for t in range(BASELINE + GAP, t_len):
        step["t"] = t
        hist = sc.data[t - BASELINE - GAP: t - GAP]
        cur = sc.data[t]
        for m in STAT_METRICS + ["db_pool_util"]:
            v = float(cur[col[m]])
            if threshold_detect(m, v, cfg["thresholds"]):
                preds["threshold"][t] = 1
            if m in STAT_METRICS:
                h = hist[:, col[m]]
                if zscore_detect(m, h, v, cfg["zscore_threshold"]):
                    preds["zscore"][t] = 1
                if moving_average_detect(m, h, v, cfg["ewma_alpha"], cfg["ewma_ratio_threshold"]):
                    preds["moving_average"][t] = 1
        idx = [col[f] for f in IF_FEATURES]
        if iforest.detect(sc.name, hist[:, idx], cur[idx]):
            preds["isolation_forest"][t] = 1
    n = cfg["confirm_cycles"]
    out = {m: _confirm(preds[m], n) for m in METHODS if m != "ensemble"}
    out["ensemble"] = np.max(np.vstack([out[m] for m in out]), axis=0)
    return out


def score(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    tp = int(((p == 1) & (y == 1)).sum())
    fp = int(((p == 1) & (y == 0)).sum())
    fn = int(((p == 0) & (y == 1)).sum())
    tn = int(((p == 0) & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    return {"precision": round(prec, 4), "recall": round(rec, 4), "f1": round(f1, 4),
            "false_positive_rate": round(fp / (fp + tn) if fp + tn else 0.0, 5),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn}


def evaluate(seeds: tuple[int, ...] = (11, 22, 33)) -> dict:
    cfg = DEFAULT_CONFIG
    t0 = time.perf_counter()
    scenarios = dataset(seeds)
    ys, ps = [], defaultdict(list)
    per_type: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    delays: dict[str, list[int]] = defaultdict(list)
    for sc in scenarios:
        pred = replay(sc, cfg)
        start = BASELINE + GAP
        y = sc.labels[start:]
        ys.append(y)
        for m in METHODS:
            p = pred[m][start:]
            ps[m].append(p)
            per_type[sc.kind][m].append((y, p))
        if sc.onset is not None:
            hits = np.where(pred["ensemble"][sc.onset: sc.onset + 60] == 1)[0]
            delays[sc.kind].append(int(hits[0]) if len(hits) else -1)
    y_all = np.concatenate(ys)
    result = {
        "dataset": "synthetic-v1", "dataset_description": DATASET_DESCRIPTION.format(n=len(scenarios), seeds=list(seeds)),
        "config": {k: cfg[k] for k in ("zscore_threshold", "ewma_alpha", "ewma_ratio_threshold", "iforest_threshold", "confirm_cycles")},
        "overall": {m: score(y_all, np.concatenate(ps[m])) for m in METHODS},
        "per_type": {k: {m: score(np.concatenate([a for a, _ in v[m]]), np.concatenate([b for _, b in v[m]])) for m in METHODS}
                     for k, v in per_type.items()},
        "detection_delay_steps": {k: v for k, v in delays.items()},
        "detection_delay_note": "steps of 15 s from onset to first ensemble detection (-1 = missed)",
        "runtime_s": round(time.perf_counter() - t0, 1),
        "points_evaluated": int(len(y_all)), "anomalous_points": int(y_all.sum()),
    }
    return result


async def persist(result: dict) -> None:
    from app.db.models import ModelEvaluation
    from app.db.session import init_engine, sessionmaker
    init_engine(os.environ["DATABASE_URL"], pool_size=1)
    async with sessionmaker()() as s:
        for m, metrics in result["overall"].items():
            s.add(ModelEvaluation(kind="anomaly", detector=m, dataset=result["dataset"],
                                  dataset_description=result["dataset_description"], metrics=metrics,
                                  params=result["config"] | {"per_type": {k: v[m] for k, v in result["per_type"].items()}}))
        await s.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persist", action="store_true")
    ap.add_argument("--from-json", action="store_true", help="persist artifacts/anomaly_evaluation.json without recomputing")
    args = ap.parse_args()
    if args.from_json:
        asyncio.run(persist(json.loads((ART / "anomaly_evaluation.json").read_text())))
        print("persisted existing evaluation to model_evaluations")
        return
    path = train_fallback()
    print(f"trained fallback isolation forest -> {path}")
    res = evaluate()
    (ART / "anomaly_evaluation.json").write_text(json.dumps(res, indent=2))
    print(json.dumps(res["overall"], indent=2))
    print(f"runtime {res['runtime_s']}s over {res['points_evaluated']} points")
    if args.persist:
        asyncio.run(persist(res))
        print("persisted to model_evaluations")


if __name__ == "__main__":
    main()
