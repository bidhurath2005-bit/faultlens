"""Pydantic request/response schemas for the public API."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.models import IncidentStatus, Role, Severity

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class LoginIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: Role
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=12, max_length=256)
    role: Role = Role.VIEWER


class UserUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=256)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


class IncidentOut(BaseModel):
    id: int
    title: str
    status: IncidentStatus
    severity: Severity
    source: str
    primary_service: str | None
    services: list[str]
    opened_at: datetime
    last_activity_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None
    resolution_note: str | None
    recovery_verified_at: datetime | None


class IncidentCreate(BaseModel):
    title: str = Field(min_length=3, max_length=255)
    severity: Severity
    services: list[str] = Field(min_length=1, max_length=5)
    description: str | None = Field(default=None, max_length=2000)


class ResolveIn(BaseModel):
    note: str = Field(min_length=3, max_length=2000)


class TransitionIn(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class AnomalyOut(BaseModel):
    id: int
    service: str
    incident_id: int | None
    detected_at: datetime
    metric: str
    method: str
    score: float
    severity: Severity
    observed_value: float | None
    baseline_value: float | None
    details: dict[str, Any]


FaultType = Literal["latency", "error_500", "unavailable", "db_delay", "pool_exhaustion", "cpu_burn",
                    "memory_pressure", "traffic_spike"]


class FaultIn(BaseModel):
    target: str = Field(pattern=r"^[a-z][a-z0-9-]{1,62}$")
    type: FaultType
    duration_s: int = Field(default=300, ge=10, le=900)
    latency_ms: int | None = Field(default=None, ge=0, le=10_000)
    db_delay_ms: int | None = Field(default=None, ge=0, le=5_000)
    error_rate: float | None = Field(default=None, gt=0, le=1)
    cpu_threads: int | None = Field(default=None, ge=1, le=2)
    memory_mb: int | None = Field(default=None, ge=16, le=384)
    multiplier: float | None = Field(default=None, ge=1, le=20)

    @field_validator("target")
    @classmethod
    def _t(cls, v: str) -> str:
        return v


class SettingsIn(BaseModel):
    detection: dict[str, Any] | None = None
    preferences: dict[str, Any] | None = None


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=500)


TokenOut.model_rebuild()
