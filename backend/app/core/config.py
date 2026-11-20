"""FaultLens backend configuration (12-factor: everything from the environment)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEMO_SERVICES = ["order-service", "user-service", "payment-service", "inventory-service", "notification-service"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, case_sensitive=False, extra="ignore")

    environment: str = "local"
    app_name: str = "FaultLens"
    version: str = "1.0.0"
    log_level: str = "INFO"

    database_url: str = "postgresql+asyncpg://faultlens:faultlens@localhost:5432/faultlens"
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9094"
    kafka_enabled: bool = True

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    tempo_url: str = "http://localhost:3200"
    traffic_generator_url: str = "http://localhost:8090"
    service_urls: dict[str, str] = Field(default_factory=lambda: {s: "" for s in DEMO_SERVICES})
    telemetry_timeout_s: float = 8.0

    cors_origins: list[str] = ["http://localhost:3080", "http://127.0.0.1:3080", "http://localhost:5173"]

    jwt_secret: str = Field(default="", repr=False)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=120)
    refresh_token_ttl_hours: int = Field(default=12, ge=1, le=720)
    cookie_secure: bool = False
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = Field(default="", repr=False)

    fault_injection_enabled: bool = False
    fault_admin_token: str = Field(default="", repr=False)

    llm_provider: str = "none"
    llm_api_key: str = Field(default="", repr=False)
    llm_model: str = ""
    llm_base_url: str = ""

    # worker cadence
    detect_interval_s: float = 10.0
    probe_interval_s: float = 5.0
    worker_metrics_port: int = 9464
    investigation_delay_s: float = 20.0

    @field_validator("jwt_secret")
    @classmethod
    def _secret_strength(cls, v: str) -> str:
        if v and len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @property
    def fault_injection_allowed(self) -> bool:
        return self.fault_injection_enabled and self.environment in {"local", "demo", "test"} and len(self.fault_admin_token) >= 16


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret:
        raise RuntimeError("JWT_SECRET is not configured; run scripts/setup.ps1 to generate .env")
    return s
