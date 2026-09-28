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
    min_screen_change: float = 0.0
    required_detections: tuple[str, ...] = ()
    interaction_prompt_hidden: bool = False
    anchor_point: tuple[float, float] = (0.5, 0.5)


CONTRACTS = {
    ActionName.MOVE_FORWARD: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_screen_change=0.02,
    ),
    ActionName.MOVE_BACKWARD: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_screen_change=0.02,
    ),
    ActionName.TURN_LEFT: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_screen_change=0.02,
    ),
    ActionName.TURN_RIGHT: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_screen_change=0.02,
    ),
    ActionName.INTERACT: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=10.0,
        required_detections=("interaction_prompt",),
        interaction_prompt_hidden=True,
    ),
    ActionName.CANCEL: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.PAUSED}), timeout=30.0,
    ),
    ActionName.RESUME_WORLD: ActionContract(
        (), frozenset({DSTScreen.PAUSED}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=30.0,
    ),
    ActionName.CLICK_REWARD_OPEN: ActionContract(
        ("login_reward_open_button", "login_reward_open_hover"),
        frozenset({DSTScreen.LOGIN_REWARD_AVAILABLE}),
        frozenset({DSTScreen.REWARD_RESULT, DSTScreen.MAIN_MENU}),
    ),
    ActionName.CLICK_REWARD_CLOSE: ActionContract(
        ("login_reward_close_button",),
        frozenset({DSTScreen.REWARD_RESULT}),
        frozenset({DSTScreen.MAIN_MENU}),
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
    ActionName.CLICK_HOST_GAME: ActionContract(
        ("main_menu_host_game",), frozenset({DSTScreen.MAIN_MENU}),
        frozenset({DSTScreen.HOST_GAME_WORLD_LIST}),
        # This template includes extra dark space after the rendered label.
        # Keep the live click near the center of the text, not the padded box.
        anchor_point=(0.4, 0.5),
    ),
    ActionName.SELECT_EXISTING_WORLD: ActionContract(
        ("host_game_existing_world_row",),
        frozenset({DSTScreen.HOST_GAME_WORLD_LIST}),
        frozenset({
            DSTScreen.HOST_GAME_WORLD_SELECTED,
            DSTScreen.LOADING,
            DSTScreen.IN_WORLD_IDLE,
        }),
        timeout=120.0,
    ),
    ActionName.START_EXISTING_WORLD: ActionContract(
        ("host_game_world_selected_start",),
        frozenset({DSTScreen.HOST_GAME_WORLD_SELECTED}),
        frozenset({
            DSTScreen.LOADING,
            DSTScreen.CHARACTER_SELECTION,
            DSTScreen.IN_WORLD_IDLE,
        }),
        timeout=120.0,
    ),
    # The Redux lobby advances from Survivor Select when the selected portrait
    # is clicked. A keyboard interact key does not trigger this UI callback.
    ActionName.SELECT_SURVIVOR: ActionContract(
        ("character_select_wilson_icon",),
        frozenset({DSTScreen.CHARACTER_SELECTION}),
        frozenset({DSTScreen.CHARACTER_LOADOUT}),
        timeout=30.0,
    ),
    ActionName.START_SURVIVOR: ActionContract(
        ("character_loadout_go_button",),
        frozenset({DSTScreen.CHARACTER_LOADOUT}),
        frozenset({DSTScreen.LOADING, DSTScreen.IN_WORLD_IDLE}),
        timeout=120.0,
    ),
    ActionName.SELECT_SURVIVAL: ActionContract(
        ("host_game_playstyle_survival",),
        frozenset({DSTScreen.HOST_GAME_PLAYSTYLE}),
        frozenset({DSTScreen.HOST_GAME_CAVES_PROMPT}),
    ),
    ActionName.SELECT_NO_CAVES: ActionContract(
        ("host_game_caves_option_no_caves",),
        frozenset({DSTScreen.HOST_GAME_CAVES_PROMPT}),
        frozenset({DSTScreen.IN_WORLD_IDLE, DSTScreen.LOADING}),
        timeout=120.0,
    ),
}


def action_precondition_error(
    action: ActionName, observation: GameObservation
) -> str | None:
    """Check source state and verified target evidence before sending input."""
    contract = CONTRACTS.get(action)
    if contract is None:
        return "no action contract"
    if (
        observation.screen not in contract.source
        or not observation.production_ready
        or observation.screen_confidence < contract.confidence
    ):
        return "source state is not visually verified"
    detections = {item.kind: item for item in observation.detections}
    if any(
        (item := detections.get(name)) is None
        or not item.detected
        or not item.verified
        or item.confidence < contract.confidence
        for name in contract.required_detections
    ):
        return "required verified target is not visible"
    return None


@dataclass(slots=True)
class PendingAction:
    result: ActionResult
    contract: ActionContract
    sent_at: float
    last_sequence: int
    candidate: DSTScreen = DSTScreen.UNKNOWN
    count: int = 0
    changed: bool = False


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
        precondition_error = action_precondition_error(result.action, observation)
        if precondition_error is not None:
            return ActionResult(
                result.action_id, result.action, ActionStatus.SAFETY_BLOCKED,
                result.duration, result.runtime_generation, result.worker_generation,
                result.runtime_id, precondition_error,
            )
        self.pending = PendingAction(
            result, contract, self.clock(), observation.source_sequence,
            changed=contract.min_screen_change == 0,
        )
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
        if (
            observation.screen not in pending.contract.targets
            and observation.screen not in pending.contract.source
        ):
            return self._status(
                ActionStatus.FAILED,
                f"unexpected state {observation.screen.value} during action verification",
                clear=True,
            )
        if pending.contract.interaction_prompt_hidden:
            if observation.interaction_prompt_visible.detected:
                pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
                return None
            pending.candidate, pending.count = observation.screen, pending.count + 1
            if pending.count >= pending.contract.stable_observations:
                return self._status(
                    ActionStatus.SUCCEEDED,
                    "verified interaction prompt disappeared after the action",
                    clear=True,
                )
            return None
        if (observation.screen_change is not None
                and observation.screen_change >= pending.contract.min_screen_change):
            pending.changed = True
        if observation.screen in pending.contract.targets:
            if observation.screen == pending.candidate:
                pending.count += 1
            else:
                pending.candidate, pending.count = observation.screen, 1
            if (pending.count >= pending.contract.stable_observations
                    and pending.changed):
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
    point_x, point_y = contract.anchor_point
    return NormalizedPoint(
        bounds.left + point_x * (bounds.right - bounds.left),
        bounds.top + point_y * (bounds.bottom - bounds.top),
    ), Viewport(
                               observation.frame_width, observation.frame_height)
