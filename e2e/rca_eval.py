"""Live RCA accuracy evaluation against the running stack.

For each known failure scenario: reset -> cool down -> inject via the Fault Lab API -> wait for the
automatically opened incident and investigation -> score the ranking against the injected ground truth
-> also score an investigation re-run at T+90 s -> resolve -> reset -> wait for verified recovery.

The RCA engine never sees the ground truth (it reads telemetry only); this script is the only place
that knows which fault was injected.

    python e2e/rca_eval.py [--scenarios payment-pool,inventory-cpu] [--cooldown 150] [--repeat 1]
Writes e2e/results/rca_evaluation.json (summarised in BENCHMARKS.md).
"""
from __future__ import annotations

import argparse
import json
import platform
import time
from datetime import UTC, datetime
from pathlib import Path

from client import admin, resolve_all_open, wait_for

SCENARIOS = {
    # id: (fault payload, expected cause, expected service or None for system-wide)
    "payment-pool": ({"target": "payment-service", "type": "pool_exhaustion"}, "db_pool_exhaustion", "payment-service"),
    "inventory-db-slow": ({"target": "inventory-service", "type": "db_delay", "db_delay_ms": 600}, "db_query_latency", "inventory-service"),
    "payment-timeout": ({"target": "payment-service", "type": "latency", "latency_ms": 3500, "jitter_ms": 200}, "service_timeout", "payment-service"),
    "inventory-cpu": ({"target": "inventory-service", "type": "cpu_burn", "cpu_threads": 2}, "high_cpu", "inventory-service"),
    "notification-memory": ({"target": "notification-service", "type": "memory_pressure", "memory_mb": 384}, "memory_pressure", "notification-service"),
    "inventory-down": ({"target": "inventory-service", "type": "unavailable"}, "downstream_failure", "inventory-service"),
    "traffic-spike": ({"target": "traffic-generator", "type": "traffic_spike", "multiplier": 6}, "traffic_overload", None),
    "payment-500": ({"target": "payment-service", "type": "error_500", "error_rate": 0.6}, "http_500_spike", "payment-service"),
}

OUT = Path(__file__).resolve().parent / "results"


def rank_of(findings: list[dict], cause: str, service: str | None) -> int | None:
    for f in findings:
        if f["cause_type"] == cause and (service is None or f["service"] == service):
            return f["rank"]
    return None


def summarise(f: list[dict]) -> list[dict]:
    return [{"rank": x["rank"], "cause": x["cause_type"], "service": x["service"], "score": x["score"],
             "confidence": x["confidence"]} for x in f]


def run_one(api, sid: str, fault_s: int) -> dict:  # noqa: ANN001
    payload, cause, service = SCENARIOS[sid]
    t_inject = datetime.now(UTC)
    r = api.post("/fault-injection", json=payload | {"duration_s": fault_s})
    r.raise_for_status()
    print(f"[{sid}] injected at {t_inject:%H:%M:%S}", flush=True)

    def new_incident():  # noqa: ANN202
        items = api.get("/incidents", params={"since": t_inject.isoformat(), "limit": 20}).json()["items"]
        return items[-1] if items else None  # earliest incident opened after injection

    result: dict = {"scenario": sid, "expected": {"cause": cause, "service": service}, "injected_at": t_inject.isoformat()}
    try:
        inc = wait_for(new_incident, 150, what="incident")
    except TimeoutError:
        result.update(detected=False)
        return result
    anomalies = api.get("/anomalies", params={"incident_id": inc["id"], "limit": 500}).json()["items"]
    first = min(datetime.fromisoformat(a["detected_at"]) for a in anomalies) if anomalies else None
    result.update(detected=True, incident_id=inc["id"],
                  detection_latency_s=round((first - t_inject).total_seconds(), 1) if first else None,
                  methods_fired=sorted({a["method"] for a in anomalies}))

    def first_auto():  # noqa: ANN202
        invs = api.get(f"/incidents/{inc['id']}").json()["investigations"]
        done = [i for i in invs if i["status"] != "running"]
        return min(done, key=lambda i: i["id"]) if done else None

    try:
        first = wait_for(first_auto, 120, what="automatic investigation")
        inv = api.get(f"/rca/investigations/{first['id']}").json()
        result["auto"] = {"investigation_id": inv["id"], "status": inv["status"],
                          "completed_after_injection_s": round((datetime.fromisoformat(inv["completed_at"]) - t_inject).total_seconds(), 1),
                          "duration_ms": inv["duration_ms"], "rank_of_truth": rank_of(inv["findings"], cause, service),
                          "findings": summarise(inv["findings"])}
    except TimeoutError:
        result["auto"] = None

    # Re-run at ~T+90 s with more evidence available.
    remaining = 90 - (datetime.now(UTC) - t_inject).total_seconds()
    if remaining > 0:
        time.sleep(remaining)
    rr = api.post(f"/rca/{inc['id']}/run")
    if rr.status_code == 200:
        d = rr.json()
        result["t90"] = {"status": d["status"], "duration_ms": d["duration_ms"],
                         "rank_of_truth": rank_of(d["findings"], cause, service), "findings": summarise(d["findings"])}
    print(f"[{sid}] incident #{inc['id']} detect={result.get('detection_latency_s')}s "
          f"auto_rank={(result.get('auto') or {}).get('rank_of_truth')} t90_rank={(result.get('t90') or {}).get('rank_of_truth')} "
          f"top={(result.get('t90') or {}).get('findings', [{}])[:1]}", flush=True)
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default=",".join(SCENARIOS))
    ap.add_argument("--cooldown", type=int, default=150)
    ap.add_argument("--fault-seconds", type=int, default=180)
    ap.add_argument("--repeat", type=int, default=1)
    args = ap.parse_args()
    api = admin()
    OUT.mkdir(exist_ok=True)
    results = []
    started = time.time()
    for rep in range(args.repeat):
        for sid in args.scenarios.split(","):
            api.post("/fault-injection/reset")
            resolve_all_open(api)
            print(f"[{sid}] cooling down {args.cooldown}s", flush=True)
            time.sleep(args.cooldown)
            res = run_one(api, sid, args.fault_seconds)
            res["repeat"] = rep
            api.post("/fault-injection/reset")
            if res.get("incident_id"):
                iid = res["incident_id"]
                t_reset = time.time()
                try:
                    wait_for(lambda iid=iid: api.get(f"/incidents/{iid}").json()["recovery_verified_at"], 300, 5, "recovery")
                    res["recovery_verified_after_reset_s"] = round(time.time() - t_reset, 1)
                except TimeoutError:
                    res["recovery_verified_after_reset_s"] = None
            results.append(res)
            (OUT / "rca_evaluation.partial.json").write_text(json.dumps(results, indent=2))
    resolve_all_open(api)

    def acc(key: str, k: int) -> float:
        scored = [r for r in results if r.get(key)]
        return round(sum(1 for r in scored if r[key]["rank_of_truth"] and r[key]["rank_of_truth"] <= k) / len(results), 3) if results else 0.0

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "scenarios_run": len(results),
        "detected": sum(1 for r in results if r.get("detected")),
        "auto_top1": acc("auto", 1), "auto_top3": acc("auto", 3),
        "t90_top1": acc("t90", 1), "t90_top3": acc("t90", 3),
        "median_detection_latency_s": sorted(r["detection_latency_s"] for r in results if r.get("detection_latency_s") is not None)[
            len([r for r in results if r.get("detection_latency_s") is not None]) // 2] if any(r.get("detection_latency_s") for r in results) else None,
        "wall_time_min": round((time.time() - started) / 60, 1),
        "note": "Accuracy denominators include undetected scenarios (counted as misses).",
        "results": results,
    }
    (OUT / "rca_evaluation.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, indent=2))


if __name__ == "__main__":
    main()
