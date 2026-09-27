"""Verifies gameplay outcomes from fresh perception, independent of transport."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport
from runtime_agent.gameworker.vision import DSTScreen, GameObservation


@dataclass(frozen=True, slots=True)
class ActionContract:
    anchors: tuple[str, ...]
    source: frozenset[DSTScreen]
    targets: frozenset[DSTScreen]
    timeout: float = 45.0
    confidence: float = 0.94
    stable_observations: int = 2


CONTRACTS = {
    ActionName.CLICK_REWARD_OPEN: ActionContract(
        ("login_reward_open_button", "login_reward_open_hover"),
        frozenset({DSTScreen.LOGIN_REWARD_AVAILABLE}),
        frozenset({DSTScreen.REWARD_RESULT, DSTScreen.MAIN_MENU}),
    ),
    ActionName.CLICK_OPTIONS: ActionContract(
        ("main_menu_options",), frozenset({DSTScreen.MAIN_MENU}),
        frozenset({DSTScreen.OPTIONS}),
    ),
    ActionName.CLICK_BACK: ActionContract(
        ("options_back",), frozenset({DSTScreen.OPTIONS}),
        frozenset({DSTScreen.OPTIONS_DISCARD_CONFIRM, DSTScreen.MAIN_MENU}),
    ),
    ActionName.DISCARD_OPTIONS: ActionContract(
        ("options_discard_yes",), frozenset({DSTScreen.OPTIONS_DISCARD_CONFIRM}),
        frozenset({DSTScreen.MAIN_MENU}),
    ),
}


@dataclass(slots=True)
class PendingAction:
    result: ActionResult
    contract: ActionContract
    sent_at: float
    last_sequence: int
    candidate: DSTScreen = DSTScreen.UNKNOWN
    count: int = 0


class ActionLifecycle:
    """One in-flight action; transport SENT is terminal only for the transport."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.pending: PendingAction | None = None

    def begin(self, result: ActionResult, observation: GameObservation) -> ActionResult:
        if result.status != ActionStatus.SENT:
            return result
        contract = CONTRACTS.get(result.action)
        if contract is None or self.pending is not None:
            return ActionResult(
                result.action_id, result.action, ActionStatus.REJECTED, result.duration,
                result.runtime_generation, result.worker_generation, result.runtime_id,
                "no action contract or another action is in flight",
            )
        if (observation.screen not in contract.source or not observation.production_ready
                or observation.screen_confidence < contract.confidence):
            return ActionResult(
                result.action_id, result.action, ActionStatus.SAFETY_BLOCKED,
                result.duration, result.runtime_generation, result.worker_generation,
                result.runtime_id, "source state is not visually verified",
            )
        self.pending = PendingAction(result, contract, self.clock(), observation.source_sequence)
        return self._status(ActionStatus.VERIFYING, "awaiting verified screen transition")

    def observe(self, observation: GameObservation) -> ActionResult | None:
        pending = self.pending
        if pending is None:
            return None
        if self.clock() - pending.sent_at >= pending.contract.timeout:
            return self._status(ActionStatus.TIMED_OUT, "verified transition deadline elapsed", clear=True)
        if observation.source_sequence <= pending.last_sequence:
            return None
        if (
            not observation.production_ready
            or observation.observed_monotonic < pending.sent_at
            or observation.fresh_until < self.clock()
            or observation.runtime_id != pending.result.runtime_id
            or observation.runtime_generation != pending.result.runtime_generation
            or observation.worker_generation != pending.result.worker_generation
            or observation.screen_confidence < pending.contract.confidence
        ):
            pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
            return None
        pending.last_sequence = observation.source_sequence
        if observation.screen in pending.contract.targets:
            if observation.screen == pending.candidate:
                pending.count += 1
            else:
                pending.candidate, pending.count = observation.screen, 1
            if pending.count >= pending.contract.stable_observations:
                return self._status(
                    ActionStatus.SUCCEEDED,
                    f"perception verified transition to {observation.screen.value} "
                    f"({observation.screen_confidence:.4f})", clear=True,
                )
        else:
            pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
        return None

    def poll(self) -> ActionResult | None:
        pending = self.pending
        if pending is not None and self.clock() - pending.sent_at >= pending.contract.timeout:
            return self._status(ActionStatus.TIMED_OUT,
                                "verified transition deadline elapsed", clear=True)
        return None

    def current(self) -> ActionResult | None:
        return self._status(ActionStatus.VERIFYING,
                            "awaiting verified screen transition") if self.pending else None

    def abort(self, status: ActionStatus = ActionStatus.FAILED, reason="action aborted"):
        return self._status(status, reason, clear=True) if self.pending else None

    def _status(self, status, reason, *, clear=False):
        assert self.pending is not None
        result = self.pending.result
        terminal = ActionResult(
            result.action_id, result.action, status, result.duration,
            result.runtime_generation, result.worker_generation, result.runtime_id, reason,
        )
        if clear:
            self.pending = None
        return terminal


def click_request(action: ActionName, observation: GameObservation):
    """Resolve only a verified detector anchor; behavior never chooses pixels."""
    contract = CONTRACTS.get(action)
    if contract is None:
        raise ValueError("action has no visual anchor contract")
    detection = next((
        item for item in observation.detections
        if item.kind in contract.anchors and item.detected and item.verified
        and item.bounds is not None and item.confidence >= contract.confidence
    ), None)
    if detection is None or detection.bounds is None:
        raise ValueError("verified action anchor is unavailable")
    bounds = detection.bounds
    return NormalizedPoint((bounds.left + bounds.right) / 2,
                           (bounds.top + bounds.bottom) / 2), Viewport(
                               observation.frame_width, observation.frame_height)
