"""Two bounded locomotion profiles; input and recovery remain owned by GameWorker."""
from __future__ import annotations

import re
import time

from runtime_agent.gameworker.actions import ActionName, ActionStatus
from runtime_agent.gameworker.vision import DSTScreen

PROFILES = {"CONTROL": (.4, 18.0), "HIGH_ACTIVITY": (1.6, .5)}


class Locomotion:
    def __init__(self):
        self.profile = None
        self.session_id = None
        self.started = 0.0
        self.deadline = 0.0
        self.until_gift = False
        self.next_at = 0.0
        self.direction = ActionName.MOVE_FORWARD
        self.previous_direction = None
        self.pending = {}
        self.last_observed = None
        self.last_valid = False
        self.last_frame = None
        self.movement_commands = 0
        self.moving_seconds = 0.0
        self.valid_elapsed = 0.0
        self.direction_changes = 0
        self.failures = 0

    def configure(self, profile, session_id, seconds, until_gift=False):
        if profile not in PROFILES or not isinstance(session_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", session_id
        ):
            raise ValueError("invalid locomotion profile/session identity")
        if isinstance(seconds, bool) or not 1 <= float(seconds) <= 604800:
            raise ValueError("experiment duration must be bounded to seven days")
        self.__init__()
        self.profile, self.session_id = profile, session_id
        self.started = time.monotonic()
        self.deadline = self.started + float(seconds)
        self.until_gift = bool(until_gift)

    def suspend(self):
        self.last_valid = False
        self.last_observed = None

    def observe(self, observation, active):
        if not self.profile or observation.source_frame_id == self.last_frame:
            return
        now = observation.observed_monotonic
        valid = bool(active and observation.production_ready and observation.is_fresh()
                     and observation.screen == DSTScreen.IN_WORLD_IDLE)
        if observation.screen in {DSTScreen.LOADING, DSTScreen.DEAD, DSTScreen.WORLD_RESET_PENDING}:
            self.failures = 0
        if self.last_observed is not None and self.last_valid and valid:
            # Never credit a stale observation/heartbeat gap as online time.
            elapsed = max(0.0, now - self.last_observed)
            if elapsed <= 5.0:
                self.valid_elapsed += elapsed
        self.last_valid, self.last_observed = valid, now
        self.last_frame = observation.source_frame_id

    def proposal(self, observation):
        if (not self.profile or self.pending or time.monotonic() < self.next_at
                or time.monotonic() >= self.deadline
                or not observation.production_ready or not observation.is_fresh()
                or observation.screen != DSTScreen.IN_WORLD_IDLE):
            return None
        return self.direction, PROFILES[self.profile][0]

    def sent(self, result):
        if (self.profile and result.action in {ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD}
                and result.status == ActionStatus.VERIFYING and result.action_id not in self.pending):
            self.movement_commands += 1
            self.pending[result.action_id] = result.action

    def verified(self, result):
        direction = self.pending.pop(result.action_id, None)
        if direction is None:
            return
        if result.status == ActionStatus.SUCCEEDED:
            self.failures = 0
            self.moving_seconds += PROFILES[self.profile][0]
            if self.previous_direction is not None and self.previous_direction != direction:
                self.direction_changes += 1
            self.previous_direction = direction
        else:
            self.failures += 1
        self.direction = (ActionName.MOVE_BACKWARD if direction == ActionName.MOVE_FORWARD
                          else ActionName.MOVE_FORWARD)
        self.next_at = time.monotonic() + PROFILES[self.profile][1]

    def telemetry(self):
        return {
            "profile": self.profile, "session_id": self.session_id,
            "movement_commands": self.movement_commands,
            "moving_seconds": round(self.moving_seconds, 3),
            "idle_seconds": round(max(0.0, self.valid_elapsed - self.moving_seconds), 3),
            "direction_changes": self.direction_changes,
            "movement_failures": self.failures,
            "active_elapsed": round(self.valid_elapsed, 3),
            "valid_online_world_elapsed": round(self.valid_elapsed, 3),
            "elapsed_seconds": round(max(0.0, time.monotonic() - self.started), 3)
            if self.profile else 0.0,
        }
