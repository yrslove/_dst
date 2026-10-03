"""Passive stationary experiment events; this observer never proposes input."""
from __future__ import annotations

from collections import deque

from runtime_agent.gameworker.vision import DSTScreen


class StationarySession:
    def __init__(self):
        self.state = "JOIN_WORLD"
        self.world_entered_at = None
        self.wait_started_at = None
        self.gift_first_detected_at = None
        self.previous_gift_at = None
        self._wait_monotonic = None
        self._previous_gift_monotonic = None
        self._gift_monotonic = None
        self._last_frame = None
        self._last_screen = None
        self._availability = None
        self._click_id = None
        self._receipt_id = None
        self._reveal_at = None
        self.movement_count = 0
        self.recovery_blocker = None
        self.events = deque(maxlen=128)

    def emit(self, event, timestamp, **values):
        self.events.append({"event": event, "timestamp": timestamp, **values})

    def lost_world(self, timestamp, reason):
        self.state = "RECOVERY_BLOCKER"
        self.recovery_blocker = reason
        self.world_entered_at = None
        self.wait_started_at = None
        self._wait_monotonic = None
        self.emit("RECOVERY_STATE", timestamp, reason=reason)

    def observe(self, observation):
        if (observation is None or not observation.production_ready
                or not observation.is_fresh()
                or observation.source_frame_id == self._last_frame):
            return
        self._last_frame = observation.source_frame_id
        screen = observation.screen
        if screen != self._last_screen:
            if screen in {DSTScreen.DEAD, DSTScreen.WORLD_RESET_PENDING,
                          DSTScreen.DISCONNECTED}:
                self.lost_world(observation.timestamp, screen.value)
            self._last_screen = screen
        if screen == DSTScreen.IN_WORLD_IDLE:
            if self.world_entered_at is None:
                self.world_entered_at = observation.timestamp
                self.emit("WORLD_ENTERED", observation.timestamp)
            if self.recovery_blocker == "GAME_LOST":
                self.recovery_blocker = None
                self.state = "JOIN_WORLD"
                self.emit(
                    "GAME_REJOINED",
                    observation.timestamp,
                    recovery="SAVED_WORLD_REENTRY",
                    movement_count=self.movement_count,
                )
            if self.recovery_blocker:
                self.state = "RECOVERY_BLOCKER"
                return
            if self.wait_started_at is None:
                self.wait_started_at = observation.timestamp
                self._wait_monotonic = observation.observed_monotonic
                self.emit("STATIONARY_WAIT_STARTED", observation.timestamp,
                          station_position="OPERATOR_PRECONDITION", movement_count=0)
            icon = next((d for d in observation.detections if d.kind == "gift_icon"
                         and d.detected and d.verified and d.confidence >= .94), None)
            availability = dict(icon.metadata).get("availability") if icon else None
            present = availability in {"GIFT_AVAILABLE", "IN_WORLD_GIFT_PENDING"}
            if present and self.gift_first_detected_at is None:
                self.gift_first_detected_at = observation.timestamp
                self._gift_monotonic = observation.observed_monotonic
                self.emit("GIFT_FIRST_DETECTED", observation.timestamp,
                          stationary_wait_to_gift_seconds=(
                              max(0.0, self._gift_monotonic - self._wait_monotonic)
                              if self._wait_monotonic is not None else None),
                          previous_gift_to_next_seconds=(
                              max(0.0, self._gift_monotonic - self._previous_gift_monotonic)
                              if self._previous_gift_monotonic is not None else None))
            if availability != self._availability:
                self.emit("GIFT_AVAILABILITY", observation.timestamp,
                          availability=availability or "NO_HUD_GIFT")
                self._availability = availability
            self.state = "GIFT_DETECTED" if present else "STATIONARY_WAIT"
        elif screen in {DSTScreen.IN_WORLD_GIFT_OPENING, DSTScreen.IN_WORLD_GIFT_RECEIVED}:
            self.state = "WAIT_FOR_REVEAL_AND_OPEN_STATE"

    def clicked(self, result, timestamp):
        if result.action_id != self._click_id:
            self._click_id = result.action_id
            self.state = "COLLECT_GIFT"
            self.emit("GIFT_CLICK", timestamp, action_id=result.action_id)

    def reveal_completed(self, timestamp):
        if self._reveal_at is None:
            self._reveal_at = timestamp
            self.state = "VERIFY_GIFT_CLEARED"
            self.emit("REVEAL_COMPLETED", timestamp,
                      verification="RECEIVED_UI_AFTER_TEN_SECOND_DWELL")

    def cleared(self, receipt, timestamp, monotonic):
        if receipt.get("item_id") == self._receipt_id:
            return
        self._receipt_id = receipt.get("item_id")
        self.emit("GIFT_CLEARED", timestamp, item_id=self._receipt_id,
                  native_opened_at=receipt.get("SetItemOpened_Complete_at"))
        self.previous_gift_at = self.gift_first_detected_at
        self._previous_gift_monotonic = self._gift_monotonic
        self.gift_first_detected_at = None
        self._gift_monotonic = None
        self._reveal_at = None
        self.wait_started_at = timestamp
        self._wait_monotonic = monotonic
        self.state = "STATIONARY_WAIT"
        self.emit("STATIONARY_WAIT_STARTED", timestamp, movement_count=self.movement_count)

    def telemetry(self):
        return {"policy": "STATIONARY", "state": self.state,
                "world_entered_at": self.world_entered_at,
                "stationary_wait_started_at": self.wait_started_at,
                "gift_first_detected_at": self.gift_first_detected_at,
                "previous_gift_detected_at": self.previous_gift_at,
                "station_position": "OPERATOR_PRECONDITION",
                "movement_count": self.movement_count,
                "recovery_blocker": self.recovery_blocker,
                "periodic_movement_enabled": False}
