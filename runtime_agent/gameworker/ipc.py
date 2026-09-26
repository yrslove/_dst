from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WorkerCommandName(StrEnum):
    GAME_READY = "GAME_READY"
    GAME_LOST = "GAME_LOST"
    RUNTIME_VERIFIED = "RUNTIME_VERIFIED"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    SET_MODE = "SET_MODE"
    STATUS = "STATUS"
    STOP = "STOP"


class WorkerCommandResult(StrEnum):
    OK = "OK"
    FAILED = "FAILED"
    INVALID_COMMAND = "INVALID_COMMAND"
    ACTIVE_GATE_CLOSED = "ACTIVE_GATE_CLOSED"
    UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    IPC_QUEUE_FULL = "IPC_QUEUE_FULL"
    WORKER_CRASHED = "WORKER_CRASHED"
    WORKER_STOPPED = "WORKER_STOPPED"
    WORKER_STOPPING = "WORKER_STOPPING"
    WORKER_RESTARTING = "WORKER_RESTARTING"
    STALE_GENERATION = "STALE_GENERATION"
    FORCED_STOP = "FORCED_STOP"
    UNSUPPORTED_NOOP = "UNSUPPORTED_NOOP"


@dataclass(frozen=True, slots=True)
class WorkerIPCCommand:
    """Pickle-safe command envelope owned by one worker-process generation."""

    worker_generation: int
    command: str
    command_id: int | None = None
    values: dict[str, bool | int | float | str | None] = field(default_factory=dict)

    def __getitem__(self, key: str):
        if key in {"worker_generation", "command", "command_id"}:
            return getattr(self, key)
        return self.values[key]

    def get(self, key: str, default=None):
        try:
            return self[key]
        except KeyError:
            return default


@dataclass(frozen=True, slots=True)
class WorkerIPCReport:
    """Replaceable status emitted by one worker-process generation."""

    worker_generation: int
    report: dict


@dataclass(frozen=True, slots=True)
class WorkerIPCAcknowledgement:
    """Terminal command outcome; transported separately from periodic status."""

    worker_generation: int
    command_id: int
    result: str
    report: dict
