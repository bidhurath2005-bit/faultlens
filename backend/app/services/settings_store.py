"""Runtime-configurable settings persisted in PostgreSQL (detection thresholds, UI preferences)."""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.db.models import AppSetting
from app.engines.anomaly.detectors import DEFAULT_CONFIG

DEFAULTS: dict[str, dict[str, Any]] = {
    "detection": DEFAULT_CONFIG,
    "preferences": {"refresh_interval_s": 10, "default_time_range_m": 30, "theme": "dark"},
}

_BOUNDS = {
    "zscore_threshold": (1.5, 20), "ewma_alpha": (0.01, 0.99), "ewma_ratio_threshold": (1.1, 20),
    "iforest_threshold": (-0.5, 0.0), "min_baseline_points": (5, 200), "confirm_cycles": (1, 10),
    "cooldown_s": (10, 3600), "min_rps": (0, 100),
}
_THRESHOLD_BOUNDS = {"latency_p95_s": (0.05, 60), "error_rate": (0.001, 1), "cpu": (0.1, 16), "memory_util": (0.1, 1),
                     "db_pool_util": (0.1, 1), "db_query_p95_s": (0.01, 60), "availability_min": (0.1, 1)}
_METHODS = {"threshold", "zscore", "moving_average", "isolation_forest"}


def _merge(base: dict, patch: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate_detection(cfg: dict[str, Any]) -> dict[str, Any]:
    for k, (lo, hi) in _BOUNDS.items():
        v = cfg.get(k)
        if not isinstance(v, int | float) or not lo <= v <= hi:
            raise AppError(f"detection.{k} must be between {lo} and {hi}", code="invalid_setting", status_code=422)
    for k, (lo, hi) in _THRESHOLD_BOUNDS.items():
        v = cfg.get("thresholds", {}).get(k)
        if not isinstance(v, int | float) or not lo <= v <= hi:
            raise AppError(f"detection.thresholds.{k} must be between {lo} and {hi}", code="invalid_setting", status_code=422)
    methods = cfg.get("enabled_methods")
    if not isinstance(methods, list) or not methods or not set(methods) <= _METHODS:
        raise AppError(f"enabled_methods must be a non-empty subset of {sorted(_METHODS)}", code="invalid_setting", status_code=422)
    unknown = set(cfg) - set(DEFAULT_CONFIG)
    if unknown:
        raise AppError(f"unknown detection keys: {sorted(unknown)}", code="invalid_setting", status_code=422)
    return cfg


async def get_setting(session: AsyncSession, key: str) -> dict[str, Any]:
    row = await session.get(AppSetting, key)
    return _merge(DEFAULTS[key], row.value) if row else copy.deepcopy(DEFAULTS[key])


async def put_setting(session: AsyncSession, key: str, patch: dict[str, Any], user_id: int) -> dict[str, Any]:
    current = await get_setting(session, key)
    merged = _merge(current, patch)
    if key == "detection":
        validate_detection(merged)
    row = await session.get(AppSetting, key)
    if row:
        row.value, row.updated_by_id = merged, user_id
    else:
        session.add(AppSetting(key=key, value=merged, updated_by_id=user_id))
    await session.flush()
    return merged
