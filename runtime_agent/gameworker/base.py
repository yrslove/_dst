from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Protocol

from app.runtime.display import DisplayEnvironment


@dataclass(frozen=True, slots=True)
class WorkerContext:
    """Deliberately credential-free context passed into a GameWorker."""

    account_id: int
    runtime_id: int
    display: DisplayEnvironment = field(
        default_factory=lambda: DisplayEnvironment(":99")
    )
    runtime_verified: bool = False
    state: str = "WORKER_IDLE"
    metadata: tuple[()] = ()
    runtime_generation: int = 1

    def __post_init__(self) -> None:
        if self.account_id < 1 or self.runtime_id < 1 or self.runtime_generation < 1:
            raise ValueError("worker context identifiers must be positive")
        if self.metadata == {}:
            object.__setattr__(self, "metadata", ())
        elif self.metadata:
            raise ValueError("worker context metadata is not allowed across IPC")


class Observation(Protocol):
    """Neutral Stage 1 boundary implemented by live and future replay inputs."""

    timestamp: str
    observed_monotonic: float
    observation_generation: int
    source_frame_id: str
    runtime_generation: int
    worker_generation: int
    validity: str

    @property
    def game_available(self) -> bool: ...

    @property
    def valid(self) -> bool: ...

    @property
    def confidence(self) -> float: ...

    def is_fresh(self, now_monotonic: float | None = None) -> bool: ...

    def as_dict(self) -> dict: ...


@dataclass(slots=True)
class WorkerReport:
    plugin: str
    version: str
    config_version: int
    mode: str
    state: str
    healthy: bool
    last_tick_at: str | None = None
    last_action: str | None = None
    last_observation_at: str | None = None
    error_code: str | None = None
    restart_count: int = 0
    would_execute: str | None = None
    telemetry: dict = field(default_factory=dict)
    details: dict = field(default_factory=dict)

    @property
    def phase(self) -> str:
        return self.state

    def as_dict(self) -> dict:
        return asdict(self)


class GameWorker(Protocol):
    def prepare(self, context: WorkerContext) -> WorkerReport: ...
    def on_game_ready(self, context: WorkerContext) -> WorkerReport: ...
    def on_game_lost(self) -> WorkerReport: ...
    def tick(self, context: WorkerContext) -> WorkerReport: ...
    def pause(self) -> WorkerReport: ...
    def resume(self) -> WorkerReport: ...
    def status(self) -> WorkerReport: ...
    def shutdown(self) -> WorkerReport: ...
