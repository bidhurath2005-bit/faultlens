"""Small synchronous client for driving a running FaultLens stack in integration/E2E/eval scripts."""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
API = os.environ.get("FAULTLENS_API", "http://localhost:8000/api/v1")


def env_value(key: str) -> str:
    if key in os.environ:
        return os.environ[key]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    raise KeyError(key)


class Api:
    def __init__(self, username: str, password: str) -> None:
        self.username, self.password = username, password
        self.http = httpx.Client(base_url=API, timeout=30)
        self.token = ""
        self.token_at = 0.0
        self.login()

    def login(self) -> dict:
        r = self.http.post("/auth/login", json={"username": self.username, "password": self.password})
        r.raise_for_status()
        body = r.json()
        self.token, self.token_at = body["access_token"], time.time()
        return body

    def _h(self) -> dict:
        if time.time() - self.token_at > 600:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    def get(self, path: str, **kw) -> httpx.Response:
        # GET is idempotent: retry once if the server closed a pooled keep-alive connection under us.
        for attempt in range(3):
            try:
                return self.http.get(path, headers=self._h(), **kw)
            except (httpx.RemoteProtocolError, httpx.ConnectError):
                if attempt == 2:
                    raise
                time.sleep(1)
        raise AssertionError("unreachable")

    def post(self, path: str, **kw) -> httpx.Response:
        return self.http.post(path, headers=self._h(), **kw)

    def put(self, path: str, **kw) -> httpx.Response:
        return self.http.put(path, headers=self._h(), **kw)

    def patch(self, path: str, **kw) -> httpx.Response:
        return self.http.patch(path, headers=self._h(), **kw)


def admin() -> Api:
    return Api(env_value("BOOTSTRAP_ADMIN_USERNAME"), env_value("BOOTSTRAP_ADMIN_PASSWORD"))


def ensure_user(adm: Api, username: str, role: str, password: str) -> Api:
    users = adm.get("/users", params={"limit": 200}).json()["items"]
    existing = next((u for u in users if u["username"] == username), None)
    if existing is None:
        adm.post("/users", json={"username": username, "password": password, "role": role}).raise_for_status()
    else:
        adm.patch(f"/users/{existing['id']}", json={"password": password, "role": role, "is_active": True}).raise_for_status()
    return Api(username, password)


def wait_for(fn, timeout: float, interval: float = 3.0, what: str = "condition"):  # noqa: ANN001, ANN201
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = fn()
        if out:
            return out
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}")


def resolve_all_open(api: Api, note: str = "closed by test harness") -> None:
    for status in ("open", "acknowledged", "investigating"):
        for inc in api.get("/incidents", params={"status": status, "limit": 200}).json()["items"]:
            if inc["status"] == "open":
                api.post(f"/incidents/{inc['id']}/acknowledge", json={})
            api.post(f"/incidents/{inc['id']}/resolve", json={"note": note})
