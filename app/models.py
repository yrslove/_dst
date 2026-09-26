from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class AccountState(StrEnum):
    NEW = "NEW"
    PROVISIONING = "PROVISIONING"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    VERIFYING = "VERIFYING"
    READY = "READY"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    DISABLED = "DISABLED"
    ERROR = "ERROR"


class RuntimeState(StrEnum):
    NEW = "NEW"
    PROVISIONING = "PROVISIONING"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    STALE = "STALE"
    ERROR = "ERROR"
    DESTROYING = "DESTROYING"
    DESTROYED = "DESTROYED"


class DesiredState(StrEnum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"


class NodeStatus(StrEnum):
    ONLINE = "ONLINE"
    OFFLINE = "OFFLINE"
    DRAINING = "DRAINING"
    MAINTENANCE = "MAINTENANCE"
    ERROR = "ERROR"


class JobKind(StrEnum):
    SETUP_RUNTIME = "SETUP_RUNTIME"
    PROVISION_RUNTIME = "PROVISION_RUNTIME"
    START_RUNTIME = "START_RUNTIME"
    STOP_RUNTIME = "STOP_RUNTIME"
    RESTART_RUNTIME = "RESTART_RUNTIME"
    REBUILD_RUNTIME = "REBUILD_RUNTIME"
    VERIFY_RUNTIME = "VERIFY_RUNTIME"
    MOVE_RUNTIME = "MOVE_RUNTIME"
    BOOTSTRAP_RUNTIME = "BOOTSTRAP_RUNTIME"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    RETRY = "RETRY"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RemoteViewStatus(StrEnum):
    CREATING = "CREATING"
    ACTIVE = "ACTIVE"
    CLOSING = "CLOSING"
    EXPIRED = "EXPIRED"
    CLOSED = "CLOSED"
    ERROR = "ERROR"


class WorkerMode(StrEnum):
    DISABLED = "DISABLED"
    OBSERVE = "OBSERVE"
    ACTIVE = "ACTIVE"


class ErrorCode(StrEnum):
    NODE_OFFLINE = "NODE_OFFLINE"
    NO_CAPACITY = "NO_CAPACITY"
    INCUS_UNAVAILABLE = "INCUS_UNAVAILABLE"
    RUNTIME_NOT_FOUND = "RUNTIME_NOT_FOUND"
    RUNTIME_START_TIMEOUT = "RUNTIME_START_TIMEOUT"
    RUNTIME_STOP_TIMEOUT = "RUNTIME_STOP_TIMEOUT"
    STEAM_NOT_RUNNING = "STEAM_NOT_RUNNING"
    DST_NOT_RUNNING = "DST_NOT_RUNNING"
    AGENT_STALE = "AGENT_STALE"
    AGENT_VERSION_MISMATCH = "AGENT_VERSION_MISMATCH"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    PROVISION_FAILED = "PROVISION_FAILED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    DISPLAY_UNAVAILABLE = "DISPLAY_UNAVAILABLE"
    DISPLAY_START_FAILED = "DISPLAY_START_FAILED"
    STEAM_START_FAILED = "STEAM_START_FAILED"
    STEAM_NEEDS_LOGIN = "STEAM_NEEDS_LOGIN"
    STEAM_READINESS_TIMEOUT = "STEAM_READINESS_TIMEOUT"
    DST_START_FAILED = "DST_START_FAILED"
    DST_READINESS_TIMEOUT = "DST_READINESS_TIMEOUT"
    RUNTIME_BOOTSTRAP_FAILED = "RUNTIME_BOOTSTRAP_FAILED"
    RUNTIME_BOOTSTRAP_INCOMPLETE = "RUNTIME_BOOTSTRAP_INCOMPLETE"
    RUNTIME_IMAGE_NOT_VERIFIED = "RUNTIME_IMAGE_NOT_VERIFIED"
    REMOTE_VIEW_UNAVAILABLE = "REMOTE_VIEW_UNAVAILABLE"
    REMOTE_VIEW_BACKEND_UNAVAILABLE = "REMOTE_VIEW_BACKEND_UNAVAILABLE"
    REMOTE_VIEW_SESSION_EXPIRED = "REMOTE_VIEW_SESSION_EXPIRED"
    WORKER_DISABLED = "WORKER_DISABLED"
    WORKER_PAUSED = "WORKER_PAUSED"
    WORKER_CAPTURE_FAILED = "WORKER_CAPTURE_FAILED"
    WORKER_DISPLAY_UNAVAILABLE = "WORKER_DISPLAY_UNAVAILABLE"
    WORKER_UNKNOWN_SCREEN = "WORKER_UNKNOWN_SCREEN"
    WORKER_INPUT_FAILED = "WORKER_INPUT_FAILED"
    WORKER_STUCK = "WORKER_STUCK"
    WORKER_RECOVERY_EXHAUSTED = "WORKER_RECOVERY_EXHAUSTED"
    WORKER_CRASHED = "WORKER_CRASHED"
    WORKER_DEADMAN_TIMEOUT = "WORKER_DEADMAN_TIMEOUT"
    WORKER_ASSET_MISSING = "WORKER_ASSET_MISSING"
    WORKER_VISION_LOW_CONFIDENCE = "WORKER_VISION_LOW_CONFIDENCE"
    WORKER_CONFIG_VERSION_MISMATCH = "WORKER_CONFIG_VERSION_MISMATCH"
    AGENT_PROTOCOL_MISMATCH = "AGENT_PROTOCOL_MISMATCH"
    NODE_PROTOCOL_MISMATCH = "NODE_PROTOCOL_MISMATCH"
    SCHEDULER_NOT_LEADER = "SCHEDULER_NOT_LEADER"
    UNKNOWN = "UNKNOWN"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    steam_username: Mapped[str] = mapped_column(
        String(120), unique=True, nullable=False
    )
    email: Mapped[str | None] = mapped_column(String(320))
    klei_email: Mapped[str | None] = mapped_column(String(320))
    notes: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=AccountState.NEW, nullable=False
    )

    secrets: Mapped[AccountSecret] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    runtimes: Mapped[list[RuntimeInstance]] = relationship(back_populates="account")


class AccountSecret(Base, TimestampMixin):
    __tablename__ = "account_secrets"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    steam_password_enc: Mapped[str | None] = mapped_column(Text)
    email_password_enc: Mapped[str | None] = mapped_column(Text)
    account: Mapped[Account] = relationship(back_populates="secrets")


class Node(Base, TimestampMixin):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    incus_remote: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(
        String(32), default=NodeStatus.OFFLINE, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    draining: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    max_active_slots: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    agent_version: Mapped[str | None] = mapped_column(String(64))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NodeHeartbeat(Base):
    __tablename__ = "node_heartbeats"
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    incus_available: Mapped[bool] = mapped_column(Boolean, nullable=False)
    active_runtime_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class NodeResourceSnapshot(Base):
    __tablename__ = "node_resource_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    cpu_percent: Mapped[float | None] = mapped_column(Float)
    load_1: Mapped[float | None] = mapped_column(Float)
    load_5: Mapped[float | None] = mapped_column(Float)
    load_15: Mapped[float | None] = mapped_column(Float)
    ram_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    ram_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    disk_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    gpu_present: Mapped[bool | None] = mapped_column(Boolean)
    gpu_utilization: Mapped[float | None] = mapped_column(Float)
    vram_used_bytes: Mapped[int | None] = mapped_column(BigInteger)
    vram_total_bytes: Mapped[int | None] = mapped_column(BigInteger)
    active_runtime_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class RuntimeInstance(Base, TimestampMixin):
    __tablename__ = "runtime_instances"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "runtime_generation", name="uq_runtime_account_generation"
        ),
        Index(
            "uq_runtime_one_active_per_account",
            "account_id",
            unique=True,
            sqlite_where=text("active = 1"),
            postgresql_where=text("active = true"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    state: Mapped[str] = mapped_column(
        String(32), default=RuntimeState.NEW, nullable=False
    )
    desired_state: Mapped[str] = mapped_column(
        String(32), default=DesiredState.STOPPED, nullable=False
    )
    network_profile: Mapped[str | None] = mapped_column(String(120))
    runtime_generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    image_version: Mapped[str] = mapped_column(String(120), nullable=False)
    bootstrap_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    bootstrap_phase: Mapped[str | None] = mapped_column(String(64))
    bootstrap_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    bootstrap_error_code: Mapped[str | None] = mapped_column(String(64))
    bootstrap_error_message: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    command_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token_hash: Mapped[str | None] = mapped_column(String(64))
    runtime_token_enc: Mapped[str | None] = mapped_column(Text)
    agent_version: Mapped[str | None] = mapped_column(String(64))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    account: Mapped[Account] = relationship(back_populates="runtimes")


class Job(Base, TimestampMixin):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "uq_job_active_command",
            "runtime_id",
            "kind",
            unique=True,
            sqlite_where=text("status IN ('PENDING','LEASED','RUNNING','RETRY')"),
            postgresql_where=text("status IN ('PENDING','LEASED','RUNNING','RETRY')"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="SET NULL"), index=True
    )
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=JobStatus.PENDING, nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    leased_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False
    )
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RuntimeLease(Base):
    __tablename__ = "runtime_leases"
    __table_args__ = (
        Index(
            "uq_runtime_active_slot_lease",
            "runtime_id",
            unique=True,
            sqlite_where=text("released_at IS NULL"),
            postgresql_where=text("released_at IS NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="CASCADE"), index=True
    )
    lease_type: Mapped[str] = mapped_column(
        String(32), default="ACTIVE_SLOT", nullable=False
    )
    lease_token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    leased_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class RuntimeHeartbeat(Base):
    __tablename__ = "runtime_heartbeats"
    id: Mapped[int] = mapped_column(primary_key=True)
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="CASCADE"), index=True
    )
    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    steam_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dst_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    healthy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    agent_version: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkerStatus(Base):
    __tablename__ = "worker_status"
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="CASCADE"), primary_key=True
    )
    phase: Mapped[str] = mapped_column(String(80), nullable=False)
    steam_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dst_running: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    healthy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    automation_state: Mapped[str] = mapped_column(
        String(80), default="NOOP", nullable=False
    )
    worker_plugin: Mapped[str] = mapped_column(
        String(80), default="noop", nullable=False
    )
    worker_version: Mapped[str] = mapped_column(
        String(32), default="1.0.0", nullable=False
    )
    worker_config_version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )
    worker_mode: Mapped[str] = mapped_column(
        String(16), default="DISABLED", nullable=False
    )
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_action: Mapped[str | None] = mapped_column(String(80))
    last_observation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    error_code: Mapped[str | None] = mapped_column(String(80))
    restart_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class WorkerCommand(Base):
    __tablename__ = "worker_commands"
    id: Mapped[int] = mapped_column(primary_key=True)
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="CASCADE"), index=True
    )
    command: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default="PENDING", nullable=False, index=True
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[str | None] = mapped_column(String(80))


class WorkerRun(Base):
    __tablename__ = "worker_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="RESTRICT"), index=True
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    plugin: Mapped[str] = mapped_column(String(80), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_active_seconds: Mapped[float] = mapped_column(
        Float, default=0, nullable=False
    )
    pause_seconds: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    actions_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recoveries: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result: Mapped[str | None] = mapped_column(String(40))


class RuntimeImage(Base, TimestampMixin):
    __tablename__ = "runtime_images"
    version: Mapped[str] = mapped_column(String(120), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )


class SchedulerLeadership(Base):
    __tablename__ = "scheduler_leadership"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    holder_id: Mapped[str | None] = mapped_column(String(120))
    fencing_token: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="RESTRICT"), index=True
    )
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT"), index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    result: Mapped[str | None] = mapped_column(String(40))
    start_reason: Mapped[str] = mapped_column(String(80), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), index=True
    )
    runtime_id: Mapped[int | None] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="SET NULL"), index=True
    )
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), index=True
    )
    level: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(120), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )


class AdminUser(Base, TimestampMixin):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class RuntimeViewSession(Base):
    __tablename__ = "runtime_view_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    runtime_id: Mapped[int] = mapped_column(
        ForeignKey("runtime_instances.id", ondelete="CASCADE"), index=True
    )
    admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    backend: Mapped[str] = mapped_column(String(32), default="disabled", nullable=False)
    backend_session_id: Mapped[str | None] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(16), default="VIEW_ONLY", nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), default=RemoteViewStatus.CREATING, nullable=False
    )
    provider_session_id: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_access_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class InventorySnapshot(Base):
    __tablename__ = "inventory_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="RESTRICT"), index=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False, index=True
    )
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False
    )
    estimated_value_optional: Mapped[float | None] = mapped_column(Float)


class RuntimeResourceProfile(Base, TimestampMixin):
    __tablename__ = "runtime_resource_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    ram_reservation_bytes: Mapped[int | None] = mapped_column(BigInteger)
    cpu_estimate: Mapped[float | None] = mapped_column(Float)
    vram_estimate_bytes: Mapped[int | None] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
