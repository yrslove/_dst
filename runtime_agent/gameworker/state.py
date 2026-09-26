from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import Lock


class WorkerState(StrEnum):
    DISABLED = "DISABLED"
    INITIALIZING = "INITIALIZING"
    WAITING_FOR_GAME = "WAITING_FOR_GAME"
    OBSERVING = "OBSERVING"
    READY = "READY"
    IDLE_ACTIVITY = "IDLE_ACTIVITY"
    NAVIGATING = "NAVIGATING"
    INTERACTING = "INTERACTING"
    WAITING = "WAITING"
    RECOVERING = "RECOVERING"
    PAUSED = "PAUSED"
    NEEDS_ATTENTION = "NEEDS_ATTENTION"
    ERROR = "ERROR"
    SHUTTING_DOWN = "SHUTTING_DOWN"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class StateTransition:
    at: str
    source: str
    target: str
    reason: str


_ALL_RUNNING = {
    WorkerState.OBSERVING,
    WorkerState.READY,
    WorkerState.IDLE_ACTIVITY,
    WorkerState.NAVIGATING,
    WorkerState.INTERACTING,
    WorkerState.WAITING,
    WorkerState.RECOVERING,
    WorkerState.PAUSED,
    WorkerState.NEEDS_ATTENTION,
    WorkerState.ERROR,
    WorkerState.SHUTTING_DOWN,
}

ALLOWED_TRANSITIONS: dict[WorkerState, set[WorkerState]] = {
    WorkerState.DISABLED: {
        WorkerState.INITIALIZING,
        WorkerState.PAUSED,
        WorkerState.SHUTTING_DOWN,
    },
    WorkerState.INITIALIZING: {
        WorkerState.WAITING_FOR_GAME,
        WorkerState.DISABLED,
        WorkerState.ERROR,
        WorkerState.PAUSED,
        WorkerState.SHUTTING_DOWN,
    },
    WorkerState.WAITING_FOR_GAME: {
        WorkerState.OBSERVING,
        WorkerState.PAUSED,
        WorkerState.ERROR,
        WorkerState.SHUTTING_DOWN,
    },
    WorkerState.OBSERVING: _ALL_RUNNING,
    WorkerState.READY: _ALL_RUNNING,
    WorkerState.IDLE_ACTIVITY: _ALL_RUNNING,
    WorkerState.NAVIGATING: _ALL_RUNNING,
    WorkerState.INTERACTING: _ALL_RUNNING,
    WorkerState.WAITING: _ALL_RUNNING,
    WorkerState.RECOVERING: _ALL_RUNNING,
    WorkerState.PAUSED: {
        WorkerState.OBSERVING,
        WorkerState.DISABLED,
        WorkerState.SHUTTING_DOWN,
        WorkerState.ERROR,
    },
    WorkerState.NEEDS_ATTENTION: {
        WorkerState.OBSERVING,
        WorkerState.PAUSED,
        WorkerState.SHUTTING_DOWN,
    },
    WorkerState.ERROR: {
        WorkerState.INITIALIZING,
        WorkerState.PAUSED,
        WorkerState.SHUTTING_DOWN,
    },
    WorkerState.SHUTTING_DOWN: {WorkerState.STOPPED},
    WorkerState.STOPPED: {WorkerState.INITIALIZING, WorkerState.DISABLED},
}


class WorkerStateMachine:
    def __init__(self, initial: WorkerState, history_size: int = 50):
        self._state = initial
        self._history: deque[StateTransition] = deque(maxlen=history_size)
        self._lock = Lock()

    @property
    def state(self) -> WorkerState:
        with self._lock:
            return self._state

    def transition(self, target: WorkerState, reason: str) -> WorkerState:
        with self._lock:
            if target == self._state:
                return self._state
            if self._state == WorkerState.PAUSED and target not in {
                WorkerState.OBSERVING,
                WorkerState.DISABLED,
                WorkerState.SHUTTING_DOWN,
                WorkerState.ERROR,
            }:
                return self._state
            if target not in ALLOWED_TRANSITIONS[self._state]:
                raise RuntimeError(f"invalid worker transition {self._state}->{target}")
            source = self._state
            self._state = target
            self._history.append(
                StateTransition(
                    datetime.now(timezone.utc).isoformat(), source, target, reason[:200]
                )
            )
            return target

    def history(self) -> list[dict]:
        with self._lock:
            return [asdict(item) for item in self._history]
