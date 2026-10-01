from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class AccountCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    steam_username: str = Field(min_length=1, max_length=120)
    steam_password: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=320)
    email_password: str | None = Field(default=None, max_length=500)
    klei_email: str | None = Field(default=None, max_length=320)
    notes: str | None = Field(default=None, max_length=2000)
    network_profile: str | None = Field(default=None, max_length=120)

    @field_validator("label", "steam_username")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=500)


class RuntimeHeartbeatRequest(BaseModel):
    runtime_id: int
    phase: Literal[
        "BOOTING",
        "DISPLAY_STARTING",
        "DISPLAY_READY",
        "STEAM_STARTING",
        "STEAM_READY",
        "DST_STARTING",
        "DST_PROCESS_RUNNING",
        "DST_READINESS_UNVERIFIED",
        "GAME_READY",
        "WORKER_IDLE",
        "NEEDS_ATTENTION",
        "ERROR",
        "SHUTTING_DOWN",
    ]
    steam_running: bool = False
    dst_running: bool = False
    healthy: bool = False
    automation_state: str = Field(default="NOOP", max_length=80)
    worker_plugin: str = Field(default="noop", max_length=80)
    worker_state: str = Field(default="NOOP", max_length=80)
    worker_last_tick_at: str | None = Field(default=None, max_length=64)
    worker_last_action: str | None = Field(default=None, max_length=80)
    worker_last_observation_at: str | None = Field(default=None, max_length=64)
    worker_error_code: str | None = Field(default=None, max_length=80)
    worker_restart_count: int = Field(default=0, ge=0, le=1000)
    worker: dict[str, Any] = Field(default_factory=dict, max_length=32)
    worker_command_results: list[dict[str, Any]] = Field(
        default_factory=list, max_length=32
    )
    details: dict[str, Any] = Field(default_factory=dict, max_length=32)
    capabilities: dict[str, Any] = Field(default_factory=dict, max_length=16)
    agent_version: str = Field(min_length=1, max_length=64)
    protocol_version: int = 1

    @field_validator("automation_state", "worker_state")
    @classmethod
    def known_worker_state(cls, value: str) -> str:
        allowed = {
            "NOOP",
            "WORKER_IDLE",
            "DISABLED",
            "INITIALIZING",
            "WAITING_FOR_GAME",
            "OBSERVING",
            "READY",
            "IDLE_ACTIVITY",
            "NAVIGATING",
            "INTERACTING",
            "WAITING",
            "RECOVERING",
            "PAUSED",
            "NEEDS_ATTENTION",
            "ERROR",
            "SHUTTING_DOWN",
            "STOPPED",
        }
        if value not in allowed:
            raise ValueError("unknown worker state")
        return value


class ViewSessionCreate(BaseModel):
    mode: Literal["VIEW_ONLY", "INTERACTIVE"] = "VIEW_ONLY"


class ViewAccessRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class WorkerModeRequest(BaseModel):
    mode: Literal["DISABLED", "OBSERVE", "ACTIVE"]
    locomotion_profile: Literal["CONTROL", "HIGH_ACTIVITY"] | None = None
    experiment_session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    experiment_seconds: float = Field(default=120, ge=1, le=604800)
    experiment_until_gift: bool = False
    experiment_target_valid_seconds: float | None = Field(default=None, gt=0, le=604800)
    experiment_continue_after_claim: bool = False


class RebuildRuntimeRequest(BaseModel):
    image_version: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("image_version")
    @classmethod
    def strip_image_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ResourceMetrics(BaseModel):
    cpu_percent: float | None = None
    load_1: float | None = None
    load_5: float | None = None
    load_15: float | None = None
    ram_used_bytes: int | None = Field(default=None, ge=0)
    ram_total_bytes: int | None = Field(default=None, ge=0)
    disk_used_bytes: int | None = Field(default=None, ge=0)
    disk_total_bytes: int | None = Field(default=None, ge=0)
    gpu_present: bool | None = None
    gpu_utilization: float | None = None
    vram_used_bytes: int | None = Field(default=None, ge=0)
    vram_total_bytes: int | None = Field(default=None, ge=0)


class NodeHeartbeatRequest(BaseModel):
    node_id: int
    agent_version: str = Field(min_length=1, max_length=64)
    protocol_version: int = 1
    incus_available: bool
    active_runtime_count: int = Field(ge=0)
    capabilities: dict[str, Any] = Field(default_factory=dict, max_length=16)
    resources: ResourceMetrics
    timestamp: str | None = None


class MoveRuntimeRequest(BaseModel):
    destination_node_id: int


class EventQuery(BaseModel):
    limit: int = Field(default=100, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
