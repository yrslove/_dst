"""Local gift-zone movement through the existing canonical input path."""
from __future__ import annotations

import re
import time

from runtime_agent.gameworker.actions import ActionName, ActionStatus
from runtime_agent.gameworker.vision import DSTScreen

PROFILES = {"CONTROL": (.10, 2.0), "HIGH_ACTIVITY": (.10, 2.0)}
CLICK_TARGETS = ((8, 0), (8, -6), (8, 0), (0, 0))
TARGETS = ((-18, 0), (18, 0), (0, -10), (0, 10),
           (-12, -8), (12, 8), (-12, 8), (12, -8))
MOVES = {ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD,
         ActionName.TURN_LEFT, ActionName.TURN_RIGHT, ActionName.CLICK_LOCAL_TARGET}


class Locomotion:
    def __init__(self):
        self.profile = None
        self.session_id = None
        self.started = 0.0
        self.deadline = 0.0
        self.until_gift = False
        self.target_valid_seconds = None
        self.next_at = 0.0
        self.direction = ActionName.MOVE_FORWARD
        self.previous_direction = None
        self.pending = {}
        self.last_observed = None
        self.last_valid = False
        self.last_frame = None
        self.movement_commands = 0
        self.moving_seconds = 0.0
        self.verified_movements = 0
        self.valid_elapsed = 0.0
        self.direction_changes = 0
        self.failures = 0
        self.recovery_commands = 0
        self.target_index = 0
        self.target = None
        self.returning = False
        self.blocked_until = {}
        self.cycle = 0
        self.displacement = None
        self.gift_latched = False
        self.anchor_lost = False
        self.pulse_duration = .10
        self.click_anchor = None
        self.click_target = None
        self.click_previous = (0, 0)
        self.click_clearance_pending = False
        self.click_world_origin = None

    def configure(self, profile, session_id, seconds, until_gift=False,
                  target_valid_seconds=None):
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
        if target_valid_seconds is not None and not 1 <= float(target_valid_seconds) <= float(seconds):
            raise ValueError("valid-time target must be bounded by the wall-clock limit")
        self.target_valid_seconds = (float(target_valid_seconds)
                                     if target_valid_seconds is not None else None)

    @property
    def stop_reason(self):
        if self.target_valid_seconds is not None and self.valid_elapsed >= self.target_valid_seconds:
            return "TARGET_VALID_ONLINE_REACHED"
        if self.profile and time.monotonic() >= self.deadline:
            return "MAX_WALL_CLOCK_REACHED"
        return None

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
        self.displacement = getattr(observation, "local_displacement", None)
        self.anchor_lost = self.displacement is None
        if (not self.profile or self.pending or self.gift_latched
                or time.monotonic() < self.next_at or self.stop_reason
                or not observation.production_ready or not observation.is_fresh()
                or observation.screen != DSTScreen.IN_WORLD_IDLE
                or self.anchor_lost):
            return None
        if self.click_anchor is not None:
            if self.click_clearance_pending:
                return ActionName.TURN_RIGHT, .05
            # Registration is relative to the original takeover reference.
            # Never rebase the origin after walking, collection or a failed click.
            self.click_world_origin = (0, 0)
            points = CLICK_TARGETS
            x, y = self.displacement
            if abs(x) > 32 or abs(y) > 20:
                self.returning, self.target = True, (0, 0)
            if self.target is not None:
                dx, dy = self.target[0] - x, self.target[1] - y
                if abs(dx) <= 2 and abs(dy) <= 2:
                    if self.returning:
                        self.returning = False
                    else:
                        self.target_index += 1
                    self.target = None
            if self.target is None:
                self.cycle += 1
                for _ in points:
                    candidate = points[self.target_index % len(points)]
                    if self.blocked_until.get(candidate, 0) <= self.cycle:
                        self.target = candidate
                        break
                    self.target_index += 1
                if self.target is None:
                    self.next_at = time.monotonic() + 2
                    return None
            if self.blocked_until.get(self.target, 0) > self.cycle:
                self.cycle += 1
                self.next_at = time.monotonic() + 2
                return None
            dx, dy = self.target[0] - x, self.target[1] - y
            # Click only a few pixels from the visible player's feet, never far.
            dx, dy = max(-12, min(12, dx)), max(-10, min(10, dy))
            self.click_target = ((self.click_anchor[0] + dx) / 1280,
                                 (self.click_anchor[1] + dy) / 720)
            return ActionName.CLICK_LOCAL_TARGET, None
        x, y = self.displacement
        # Targets are absolute offsets from takeover, never relative random walks.
        if abs(x) > 32 or abs(y) > 20:
            self.returning = True
            self.target = (0, 0)
        for _ in range(len(TARGETS) + 1):
            if self.target is not None and self.blocked_until.get(self.target, 0) > self.cycle:
                self.target, self.returning = None, False
            if self.target is None:
                self.cycle += 1
                for _ in TARGETS:
                    candidate = TARGETS[self.target_index % len(TARGETS)]
                    self.target_index += 1
                    if self.blocked_until.get(candidate, 0) <= self.cycle:
                        self.target = candidate
                        break
                if self.target is None:
                    self.next_at = time.monotonic() + 2
                    return None
            dx, dy = self.target[0]-x, self.target[1]-y
            if abs(dx) <= 3 and abs(dy) <= 3:
                if self.returning:
                    self.returning, self.target = False, None
                else:
                    self.returning, self.target = True, (0, 0)
                continue
            if abs(dx)/18 >= abs(dy)/10:
                self.direction = ActionName.TURN_RIGHT if dx > 0 else ActionName.TURN_LEFT
            else:
                self.direction = ActionName.MOVE_BACKWARD if dy > 0 else ActionName.MOVE_FORWARD
            # Short bounded pulse, followed by fresh measured displacement.
            remaining = abs(dx)/18 if self.direction in {ActionName.TURN_LEFT, ActionName.TURN_RIGHT, ActionName.CLICK_LOCAL_TARGET} else abs(dy)/10
            self.pulse_duration = min(.10, max(.025, .10 * remaining))
            return self.direction, self.pulse_duration
        return None

    def sent(self, result, *, duration=None):
        if (self.profile and result.action in MOVES
                and result.status == ActionStatus.VERIFYING and result.action_id not in self.pending):
            self.movement_commands += 1
            self.pending[result.action_id] = (result.action, duration if duration is not None
                                              else (0.0 if result.action == ActionName.CLICK_LOCAL_TARGET else self.pulse_duration))

    def verified(self, result):
        pulse = self.pending.pop(result.action_id, None)
        if pulse is None:
            return
        direction, duration = pulse
        if self.click_anchor and result.action == ActionName.TURN_RIGHT:
            self.click_clearance_pending = False
        if result.status == ActionStatus.SUCCEEDED:
            self.failures = 0
            self.moving_seconds += duration
            self.verified_movements += 1
            if self.previous_direction is not None and self.previous_direction != direction:
                self.direction_changes += 1
            self.previous_direction = direction
        elif result.status != ActionStatus.PREEMPTED:
            self.failures += 1
            if self.target is not None:
                self.blocked_until[self.target] = self.cycle + 4
            # Do not repeat an obstructed target, including an obstructed center.
            if self.click_anchor:
                # A failed leg cannot count as arrival. Return to the measured
                # origin before attempting another unblocked local leg.
                self.target_index += 1
                self.target, self.returning = (0, 0), True
                self.cycle += 1
            else:
                self.target, self.returning = None, False
        self.next_at = time.monotonic() + PROFILES[self.profile][1]

    def telemetry(self):
        return {
            "profile": self.profile, "session_id": self.session_id,
            "movement_commands": self.movement_commands,
            "moving_seconds": round(self.moving_seconds, 3),
            "verified_movements": self.verified_movements,
            "idle_seconds": round(max(0.0, self.valid_elapsed - self.moving_seconds), 3),
            "direction_changes": self.direction_changes,
            "movement_failures": self.failures,
            "recovery_commands": self.recovery_commands,
            "active_elapsed": round(self.valid_elapsed, 3),
            "valid_online_world_elapsed": round(self.valid_elapsed, 3),
            "target_valid_seconds": self.target_valid_seconds,
            "stop_reason": self.stop_reason,
            "station_zone": {
                "strategy": "local_click_cycle" if self.click_anchor else "takeover_anchor_local_targets",
                "click_anchor": self.click_anchor,
                "click_target": self.click_target,
                "target_offsets_viewport_pixels": CLICK_TARGETS if self.click_anchor else TARGETS,
                "soft_boundary_viewport_pixels": (32, 20),
                "max_pulse_seconds": .10,
                "displacement": self.displacement,
                "anchor_lost": self.anchor_lost,
                "gift_latched": self.gift_latched,
                "target": self.target,
                "blocked_targets": [list(t) for t, expiry in self.blocked_until.items() if expiry > self.cycle],
                "no_displacement_safety_hold": self.anchor_lost and self.click_anchor is None,
            },
            "elapsed_seconds": round(max(0.0, time.monotonic() - self.started), 3)
            if self.profile else 0.0,
        }
