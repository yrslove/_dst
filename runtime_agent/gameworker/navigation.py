from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from runtime_agent.gameworker.actions import ActionName, GameActions
from runtime_agent.gameworker.vision import GameObservation


class StuckDetector:
    def __init__(self, *, attempts: int = 3, minimum_change: float = 0.01):
        self.attempts = attempts
        self.minimum_change = minimum_change
        self._changes: deque[float] = deque(maxlen=attempts)

    def record(self, observation: GameObservation) -> bool:
        if observation.screen_change is None:
            return False
        self._changes.append(observation.screen_change)
        return len(self._changes) == self.attempts and all(
            value < self.minimum_change for value in self._changes
        )

    def reset(self) -> None:
        self._changes.clear()


@dataclass(slots=True)
class NavigationController:
    actions: GameActions
    stuck: StuckDetector
    timeout: float
    started_at: float | None = None

    def begin(self) -> None:
        self.started_at = time.monotonic()
        self.stuck.reset()

    def step(
        self,
        observation: GameObservation,
        direction: ActionName = ActionName.MOVE_FORWARD,
    ):
        if self.started_at is None:
            self.begin()
        if time.monotonic() - self.started_at > self.timeout:
            self.cancel()
            return "TIMEOUT", None
        if self.stuck.record(observation):
            self.cancel()
            return "STUCK", None
        return "OK", self.actions.execute(direction)

    def cancel(self) -> None:
        self.actions.release_all()
        self.started_at = None


class RecoveryController:
    def __init__(self, actions: GameActions, *, max_attempts: int):
        self.actions = actions
        self.max_attempts = max_attempts
        self.attempts = 0

    def reset(self) -> None:
        self.attempts = 0

    def recover(self):
        self.actions.release_all()
        if self.attempts >= self.max_attempts:
            return "EXHAUSTED", None
        sequence = (
            ActionName.MOVE_BACKWARD,
            ActionName.TURN_LEFT,
            ActionName.TURN_RIGHT,
        )
        action = sequence[self.attempts % len(sequence)]
        self.attempts += 1
        return "RETRY", self.actions.execute(action)
