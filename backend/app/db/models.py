"""Normalised relational model for FaultLens platform state.

Raw telemetry (metrics/logs/traces) stays in Prometheus/Loki/Tempo. PostgreSQL stores
only derived, durable state: incidents, anomalies, investigations, findings, audit, users.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


def _enum(e: type[enum.Enum], name: str) -> Enum:
    return Enum(e, name=name, values_callable=lambda x: [m.value for m in x], validate_strings=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)


class Role(str, enum.Enum):
    ADMIN = "admin"
    SRE = "sre"
    VIEWER = "viewer"


class IncidentStatus(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SEVERITY_RANK = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


class InvestigationStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


# ------------------------------------------------------------------ identity
class User(TimestampMixin, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(_enum(Role, "user_role"), nullable=False, default=Role.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint("char_length(username) >= 3", name="ck_users_username_len"),)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    user: Mapped[User] = relationship()


# ------------------------------------------------------------------ catalogue
class Service(TimestampMixin, Base):
    __tablename__ = "services"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    owner_team: Mapped[str] = mapped_column(String(64), nullable=False, default="platform")
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    __table_args__ = (CheckConstraint("tier BETWEEN 1 AND 3", name="ck_services_tier"),)


# ------------------------------------------------------------------ incidents
class Incident(TimestampMixin, Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[IncidentStatus] = mapped_column(_enum(IncidentStatus, "incident_status"), nullable=False,
                                                   default=IncidentStatus.OPEN)
    severity: Mapped[Severity] = mapped_column(_enum(Severity, "severity"), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    primary_service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    recovery_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    primary_service: Mapped[Service | None] = relationship(lazy="joined")
    services: Mapped[list[Service]] = relationship(secondary="incident_services", lazy="selectin")
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        Index("ix_incidents_status_opened", "status", "opened_at"),
        CheckConstraint("source IN ('auto','manual')", name="ck_incidents_source"),
        CheckConstraint("(status <> 'resolved') OR (resolved_at IS NOT NULL)", name="ck_incidents_resolved_ts"),
    )


class IncidentService(Base):
    __tablename__ = "incident_services"
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), primary_key=True)


class IncidentEvent(Base):
    """Incident timeline entry (state changes, anomalies attached, investigations, recovery)."""
    __tablename__ = "incident_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    actor: Mapped[User | None] = relationship(lazy="joined")
    __table_args__ = (Index("ix_incident_events_incident_time", "incident_id", "occurred_at"),)


class Anomaly(Base):
    __tablename__ = "anomalies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("incidents.id", ondelete="SET NULL"), index=True)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    metric: Mapped[str] = mapped_column(String(48), nullable=False)
    method: Mapped[str] = mapped_column(String(32), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    severity: Mapped[Severity] = mapped_column(_enum(Severity, "severity"), nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Float)
    baseline_value: Mapped[float | None] = mapped_column(Float)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    service: Mapped[Service] = relationship(lazy="joined")
    __table_args__ = (
        Index("ix_anomalies_service_time", "service_id", "detected_at"),
        Index("ix_anomalies_detected_at", "detected_at"),
        CheckConstraint("method IN ('threshold','zscore','moving_average','isolation_forest')", name="ck_anomalies_method"),
    )


class Investigation(Base):
    __tablename__ = "investigations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[InvestigationStatus] = mapped_column(_enum(InvestigationStatus, "investigation_status"), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="auto")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    engine_version: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    findings: Mapped[list[RootCauseFinding]] = relationship(back_populates="investigation", lazy="selectin",
                                                            order_by="RootCauseFinding.rank", cascade="all, delete-orphan")
    __table_args__ = (CheckConstraint("window_end >= window_start", name="ck_investigations_window"),)


class RootCauseFinding(Base):
    __tablename__ = "root_cause_findings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    investigation_id: Mapped[int] = mapped_column(ForeignKey("investigations.id", ondelete="CASCADE"), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id"))
    cause_type: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    signals: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    investigation: Mapped[Investigation] = relationship(back_populates="findings")
    service: Mapped[Service | None] = relationship(lazy="joined")
    recommendations: Mapped[list[Recommendation]] = relationship(lazy="selectin", order_by="Recommendation.priority",
                                                                  cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("investigation_id", "rank", name="uq_findings_rank"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_findings_score"),
    )


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    finding_id: Mapped[int] = mapped_column(ForeignKey("root_cause_findings.id", ondelete="CASCADE"), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    risk: Mapped[str] = mapped_column(String(16), nullable=False, default="low")


# ------------------------------------------------------------------ governance
class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_username: Mapped[str | None] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(16), nullable=False, default="success")
    client_ip: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class FaultInjection(Base):
    """Record of lab activity. NOTE: the RCA engine never reads this table (see tests/test_rca_isolation.py)."""
    __tablename__ = "fault_injections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    target: Mapped[str] = mapped_column(String(64), nullable=False)
    fault_type: Mapped[str] = mapped_column(String(32), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (Index("ix_fault_injections_active", "ended_at", "expires_at"),)


class ModelEvaluation(Base):
    __tablename__ = "model_evaluations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # anomaly | rca
    detector: Mapped[str] = mapped_column(String(48), nullable=False)
    dataset: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_description: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (CheckConstraint("kind IN ('anomaly','rca')", name="ck_model_eval_kind"),)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
