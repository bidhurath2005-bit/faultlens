"""End-to-end failure scenario against the RUNNING stack (PART 17 + 26.6).

  1. An authorised SRE logs in; a Viewer exists.     2. SRE opens the live event stream (WebSocket).
  3. Normal metrics are verified.                     4. Payment latency is injected via the Fault Lab.
  5. Telemetry shifts; trace context still crosses services; logs/traces correlate.
  6. The anomaly arrives over the WebSocket.          7. The automatically opened incident is investigated;
     the top-ranked hypothesis must match the injected fault (payment-service slowdown).
  8. Logs and traces related to the incident are retrievable.
  9. Viewer can read everything but cannot change state or inject faults.
 10. SRE acknowledges -> investigates -> resolves; the fault is removed; recovery is verified.

    python -m pytest e2e/test_e2e_flow.py -v -s      (takes ~5-6 minutes)
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
import websockets

from client import admin, ensure_user, resolve_all_open, wait_for

WS = "ws://localhost:8000/api/v1/ws/events"


class LiveStream:
    """Background WebSocket subscriber collecting events into a queue."""

    def __init__(self, token: str) -> None:
        self.events: queue.Queue = queue.Queue()
        self.ready = threading.Event()
        self.stop = False
        self.t = threading.Thread(target=lambda: asyncio.run(self._run(token)), daemon=True)
        self.t.start()
        assert self.ready.wait(10), "websocket did not authenticate"

    async def _run(self, token: str) -> None:
        async with websockets.connect(WS, origin="http://localhost:3080") as ws:
            await ws.send(json.dumps({"type": "auth", "token": token}))
            assert json.loads(await ws.recv())["type"] == "auth_ok"
            self.ready.set()
            while not self.stop:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), 1))
                except TimeoutError:
                    continue
                if msg["type"] == "event":
                    self.events.put(msg["event"])

    def wait(self, pred, timeout: float) -> dict:  # noqa: ANN001
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ev = self.events.get(timeout=1)
            except queue.Empty:
                continue
            if pred(ev):
                return ev
        raise TimeoutError("expected live event not received")


@pytest.mark.timeout(900)
def test_full_failure_lifecycle():
    adm = admin()
    adm.post("/fault-injection/reset").raise_for_status()
    resolve_all_open(adm)
    sre = ensure_user(adm, "e2e-sre", "sre", "E2e-Sre-Passw0rd-1")
    viewer = ensure_user(adm, "e2e-viewer", "viewer", "E2e-Viewer-Passw0rd")
    timings: dict[str, float] = {}

    # 2. live dashboard stream
    live = LiveStream(sre.token)

    # 3. normal metrics
    ov = sre.get("/metrics/overview").json()
    assert ov["services_reporting"] == 5 and ov["current"]["rps"] > 1
    pay = next(s for s in sre.get("/services").json()["items"] if s["name"] == "payment-service")
    baseline_p95 = pay["metrics"]["latency_p95"]
    assert baseline_p95 < 0.5, f"payment-service not healthy before the test: p95={baseline_p95}"

    # 4. inject payment latency (below the caller timeout: slowdown, not outage)
    t0 = time.time()
    injected_at = datetime.now(UTC)
    r = sre.post("/fault-injection", json={"target": "payment-service", "type": "latency", "latency_ms": 2000, "jitter_ms": 100, "duration_s": 300})
    assert r.status_code == 201, r.text
    live.wait(lambda e: e["type"] == "fault.injected", 15)

    # 6. anomaly delivered over the WebSocket
    anomaly = live.wait(lambda e: e["type"] == "anomaly.detected" and e["data"]["service"] == "payment-service", 120)
    timings["anomaly_live_s"] = round(time.time() - t0, 1)
    assert anomaly["data"]["metric"] in {"latency_p95", "rps"} or anomaly["data"]["method"]

    created = live.wait(lambda e: e["type"] == "incident.created", 60)
    iid = created["data"]["incident"]["id"]
    timings["incident_live_s"] = round(time.time() - t0, 1)

    # 7. investigation: ranked hypotheses must match the injected fault
    inv_ev = live.wait(lambda e: e["type"] == "investigation.completed" and e["data"]["incident_id"] == iid, 120)
    timings["rca_live_s"] = round(time.time() - t0, 1)
    rca = sre.get(f"/rca/{iid}").json()
    top = rca["findings"][0]
    assert (top["cause_type"], top["service"]) == ("service_timeout", "payment-service"), rca["findings"][:3]
    assert top["score"] >= 0.5 and top["recommendations"]
    assert inv_ev["data"]["top"]["cause_type"] == "service_timeout"
    assert all(v == "ok" for v in rca["evidence"]["sources"].values())

    # 5/8. correlated logs + traces for the incident window
    traces = sre.get("/traces", params={"service": "payment-service", "min_duration_ms": 1500, "minutes": 10}).json()["items"]
    assert traces, "slow payment traces should exist"
    trace = sre.get(f"/traces/{traces[0]['trace_id']}").json()
    assert len(trace["service_breakdown"]) >= 2  # crossed service boundaries
    pay_span = max((s for s in trace["spans"] if s["service"] == "payment-service" and s["kind"] == "server"), key=lambda s: s["duration_ms"])
    assert pay_span["duration_ms"] >= 1500
    logs = wait_for(lambda: sre.get("/logs", params={"trace_id": trace["trace_id"], "level": "INFO,WARNING,ERROR", "minutes": 30}).json()["items"],
                    45, 3, "logs for slow trace")
    assert all(entry["trace_id"] == trace["trace_id"] for entry in logs)
    assert rca["evidence"]["traces"]["analysed"] > 0

    # 9. viewer: read yes, write no
    assert viewer.get(f"/incidents/{iid}").status_code == 200
    assert viewer.get(f"/rca/{iid}").status_code == 200
    assert viewer.post(f"/incidents/{iid}/acknowledge", json={}).status_code == 403
    assert viewer.post("/fault-injection", json={"target": "payment-service", "type": "latency"}).status_code == 403
    assert viewer.post("/fault-injection/reset").status_code == 403

    # 10. lifecycle + recovery
    assert sre.post(f"/incidents/{iid}/acknowledge", json={}).json()["status"] == "acknowledged"
    live.wait(lambda e: e["type"] == "incident.acknowledged" and e["data"]["incident"]["id"] == iid, 20)
    assert sre.post(f"/incidents/{iid}/investigate", json={}).json()["status"] == "investigating"
    sre.post("/fault-injection/reset").raise_for_status()
    live.wait(lambda e: e["type"] == "fault.cleared", 20)
    resolved = sre.post(f"/incidents/{iid}/resolve", json={"note": "e2e: injected payment latency removed"}).json()
    assert resolved["status"] == "resolved"
    t_reset = time.time()
    live.wait(lambda e: e["type"] == "recovery.verified" and e["data"]["incident_id"] == iid, 300)
    timings["recovery_verified_after_reset_s"] = round(time.time() - t_reset, 1)
    after = next(s for s in sre.get("/services").json()["items"] if s["name"] == "payment-service")
    assert after["metrics"]["latency_p95"] < 0.5
    detail = sre.get(f"/incidents/{iid}").json()
    kinds = [e["kind"] for e in detail["timeline"]]
    for k in ("incident.opened", "investigation.completed", "status.acknowledged", "status.investigating", "status.resolved", "recovery.verified"):
        assert k in kinds, kinds
    live.stop = True
    print("E2E timings:", json.dumps(timings), "injected_at", injected_at.isoformat())
    out = Path(__file__).resolve().parent / "results"
    out.mkdir(exist_ok=True)
    with open(out / "e2e_timings.json", "w") as f:
        json.dump(timings | {"incident_id": iid, "top": top["title"], "score": top["score"]}, f, indent=2)
