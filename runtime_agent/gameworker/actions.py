from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from threading import Event

from runtime_agent.gameworker.config import InputBindings, WorkerMode
from runtime_agent.gameworker.input import DeadmanSafety, InputController


class ActionName(StrEnum):
    NONE = "NONE"
    MOVE_FORWARD = "MOVE_FORWARD"
    MOVE_BACKWARD = "MOVE_BACKWARD"
    TURN_LEFT = "TURN_LEFT"
    TURN_RIGHT = "TURN_RIGHT"
    INTERACT = "INTERACT"
    CANCEL = "CANCEL"
    OPEN_INVENTORY = "OPEN_INVENTORY"
    RECOVERY = "RECOVERY"
    PAUSE = "PAUSE"


@dataclass(frozen=True, slots=True)
class ActionResult:
    action: ActionName
    duration: float
    result: str
    dry_run: bool


class GameActions:
    """The only layer that knows concrete DST key bindings."""

    def __init__(
        self,
        controller: InputController,
        deadman: DeadmanSafety,
        bindings: InputBindings,
        *,
        mode: WorkerMode,
        action_timeout: float,
    ):
        self.controller = controller
        self.deadman = deadman
        self.bindings = bindings
        self.mode = mode
        self.action_timeout = action_timeout
        self.cancel_event = Event()

    def set_mode(self, mode: WorkerMode) -> None:
        self.mode = mode
        if mode != WorkerMode.ACTIVE:
            self.cancel()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.controller.release_all()

    def release_all(self) -> None:
        self.controller.release_all()

    def reset_cancel(self) -> None:
        self.cancel_event.clear()

    def _hold(self, action: ActionName, key: str, duration: float) -> ActionResult:
        duration = max(0.0, min(duration, self.action_timeout))
        if self.mode != WorkerMode.ACTIVE:
            return ActionResult(action, duration, "WOULD_EXECUTE", True)
        if self.cancel_event.is_set():
            return ActionResult(action, 0.0, "CANCELLED", False)
        started = time.monotonic()
        with self.controller.lease.acquire(timeout=0.2):
            if not self.controller.action_limiter.allow():
                return ActionResult(action, 0.0, "RATE_LIMITED", False)
            self.deadman.touch()
            self.controller.key_down(key)
            try:
                while time.monotonic() - started < duration:
                    if self.cancel_event.wait(0.05):
                        return ActionResult(
                            action, time.monotonic() - started, "CANCELLED", False
                        )
                    self.deadman.touch()
            finally:
                self.controller.key_up(key)
        return ActionResult(action, time.monotonic() - started, "OK", False)

    def _press(self, action: ActionName, key: str) -> ActionResult:
        if self.mode != WorkerMode.ACTIVE:
            return ActionResult(action, 0.0, "WOULD_EXECUTE", True)
        if self.cancel_event.is_set():
            return ActionResult(action, 0.0, "CANCELLED", False)
        with self.controller.lease.acquire(timeout=0.2):
            if not self.controller.action_limiter.allow():
                return ActionResult(action, 0.0, "RATE_LIMITED", False)
            self.deadman.touch()
            self.controller.key_press(key)
        return ActionResult(action, 0.0, "OK", False)

    def move_forward(self, duration: float = 0.35) -> ActionResult:
        return self._hold(ActionName.MOVE_FORWARD, self.bindings.move_up, duration)

    def move_backward(self, duration: float = 0.25) -> ActionResult:
        return self._hold(ActionName.MOVE_BACKWARD, self.bindings.move_down, duration)

    def turn_left(self, duration: float = 0.20) -> ActionResult:
        return self._hold(ActionName.TURN_LEFT, self.bindings.move_left, duration)

    def turn_right(self, duration: float = 0.20) -> ActionResult:
        return self._hold(ActionName.TURN_RIGHT, self.bindings.move_right, duration)

    def interact(self) -> ActionResult:
        return self._press(ActionName.INTERACT, self.bindings.interact)

    def cancel_ui(self) -> ActionResult:
        return self._press(ActionName.CANCEL, self.bindings.cancel)

    def open_inventory(self) -> ActionResult:
        return self._press(ActionName.OPEN_INVENTORY, self.bindings.inventory)

    def execute(self, action: ActionName) -> ActionResult:
        return {
            ActionName.MOVE_FORWARD: self.move_forward,
            ActionName.MOVE_BACKWARD: self.move_backward,
            ActionName.TURN_LEFT: self.turn_left,
            ActionName.TURN_RIGHT: self.turn_right,
            ActionName.INTERACT: self.interact,
            ActionName.CANCEL: self.cancel_ui,
            ActionName.OPEN_INVENTORY: self.open_inventory,
        }.get(
            action,
            lambda: ActionResult(
                ActionName.NONE, 0.0, "NOOP", self.mode != WorkerMode.ACTIVE
            ),
        )()
