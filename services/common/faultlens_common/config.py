"""Environment-driven configuration shared by every demo microservice."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", case_sensitive=False, extra="ignore")

    service_name: str = Field(default="unknown-service")
    service_version: str = "1.0.0"
    environment: str = "local"

    database_url: str = "postgresql+asyncpg://shop:shop@localhost:5432/shop"
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_pool_timeout_s: float = Field(default=2.0, gt=0, le=30)
    db_slow_query_threshold_s: float = 0.25

    otel_exporter_otlp_endpoint: str = "http://localhost:4317"
    otel_enabled: bool = True
    otel_export_interval_ms: int = 5000

    # Downstream service URLs (only some services use them)
    user_service_url: str = "http://localhost:8101"
    inventory_service_url: str = "http://localhost:8103"
    payment_service_url: str = "http://localhost:8102"
    notification_service_url: str = "http://localhost:8104"
    downstream_timeout_s: float = 3.0

    # Fault injection: disabled unless explicitly enabled AND running in a local/demo environment.
    fault_injection_enabled: bool = False
    fault_admin_token: str = ""
    fault_max_duration_s: int = 900

    log_level: str = "INFO"

    @property
    def fault_injection_allowed(self) -> bool:
        return (
            self.fault_injection_enabled
            and self.environment in {"local", "demo", "test"}
            and len(self.fault_admin_token) >= 16
        )


@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
