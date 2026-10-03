"""Verifies gameplay outcomes from fresh perception, independent of transport."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.fixed_ui import (
    DST_FIXED_1280X720,
    FIXED_UI_ACTION_TARGETS,
)
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
    min_gameplay_change: float = 0.0
    required_detections: tuple[str, ...] = ()
    postcondition_detections: tuple[str, ...] = ()
    postcondition_hidden_detections: tuple[str, ...] = ()
    interaction_prompt_hidden: bool = False
    anchor_point: tuple[float, float] = (0.5, 0.5)


CONTRACTS = {
    ActionName.OPEN_CRAFTING_MENU: ActionContract(
        (),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        timeout=8.0,
        stable_observations=1,
        min_screen_change=0.01,
        postcondition_hidden_detections=("world_present_banner",),
    ),
    ActionName.OPEN_INVENTORY: ActionContract(
        (),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        timeout=8.0,
        stable_observations=1,
        min_screen_change=0.01,
    ),
    ActionName.HOVER_GIFT_ICON: ActionContract(
        ("gift_icon",),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        timeout=8.0,
        confidence=0.85,
        stable_observations=1,
    ),
    ActionName.CLICK_GIFT_ICON: ActionContract(
        ("gift_icon",),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({
            DSTScreen.IN_WORLD_GIFT_OPENING,
            DSTScreen.IN_WORLD_GIFT_RECEIVED,
        }),
        timeout=12.0,
        confidence=0.94,
        stable_observations=1,
    ),
    ActionName.CLICK_INWORLD_USE_LATER: ActionContract(
        ("inworld_gift_use_later",),
        frozenset({DSTScreen.IN_WORLD_GIFT_RECEIVED}),
        frozenset({DSTScreen.IN_WORLD_IDLE}),
        timeout=6.0,
        stable_observations=1,
        required_detections=("inworld_gift_received_title",),
        postcondition_hidden_detections=("inworld_gift_received_title", "inworld_gift_use_later"),
    ),
    ActionName.CLICK_LOCAL_TARGET: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=3.0,
    ),
    ActionName.MOVE_FORWARD: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_gameplay_change=0.006,
    ),
    ActionName.MOVE_BACKWARD: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_gameplay_change=0.006,
    ),
    ActionName.TURN_LEFT: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_gameplay_change=0.006,
    ),
    ActionName.TURN_RIGHT: ActionContract(
        (), frozenset({DSTScreen.IN_WORLD_IDLE}),
        frozenset({DSTScreen.IN_WORLD_IDLE}), timeout=15.0,
        min_gameplay_change=0.006,
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
    ActionName.PAUSE_WORLD: ActionContract(
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
    ActionName.CLICK_REWARD_NEXT: ActionContract(
        ("login_reward_next_button",),
        frozenset({DSTScreen.REWARD_RESULT}),
        frozenset({DSTScreen.LOGIN_REWARD_AVAILABLE, DSTScreen.MAIN_MENU}),
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
        frozenset({DSTScreen.HOST_GAME_WORLD_LIST}), timeout=8.0,
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
            DSTScreen.MODS_DISABLED_CONFIRMATION,
            DSTScreen.CHARACTER_SELECTION,
            DSTScreen.IN_WORLD_IDLE,
        }),
        timeout=120.0,
    ),
    ActionName.CONFIRM_MODS_DISABLED: ActionContract(
        ("mods_disabled_continue",),
        frozenset({DSTScreen.MODS_DISABLED_CONFIRMATION}),
        frozenset({DSTScreen.HOST_GAME_WORLD_SELECTED}),
        timeout=15.0,
        stable_observations=2,
    ),
    # The Redux lobby advances from Survivor Select when Wilson's portrait is
    # clicked. The pointer can leave its gold hover border visible while the
    # loadout transition is pending.
    ActionName.SELECT_SURVIVOR: ActionContract(
        ("character_select_wilson_icon", "character_select_wilson_hover"),
        frozenset({
            DSTScreen.CHARACTER_SELECTION,
            DSTScreen.CHARACTER_SELECTION_HOVERED,
        }),
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
    if action == ActionName.CLICK_GIFT_ICON:
        icon = next(
            (item for item in observation.detections if item.kind == "gift_icon"),
            None,
        )
        if not observation.is_fresh(time.monotonic()):
            return "gift detection is stale"
        if (
            icon is None
            or not icon.detected
            or not icon.verified
            or icon.bounds is None
            or icon.confidence < contract.confidence
            or dict(icon.metadata).get("availability") != "GIFT_AVAILABLE"
        ):
            return "fresh claimable gift detection is unavailable"
    if (
        observation.screen not in contract.source
        or not observation.production_ready
        or (
            observation.screen != DSTScreen.IN_WORLD_IDLE
            and observation.screen_confidence < contract.confidence
        )
    ):
        return "source state is not visually verified"
    detections = {item.kind: item for item in observation.detections}
    if (
        action.value not in FIXED_UI_ACTION_TARGETS
        and contract.anchors
        and not any(
            (item := detections.get(name)) is not None
            and item.detected
            and item.verified
            and item.bounds is not None
            and item.confidence >= contract.confidence
            for name in contract.anchors
        )
    ):
        return "verified action anchor is not visible"
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
    movement_evidence: float | None = None
    local_origin: tuple[float, float] | None = None


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
            local_origin=observation.local_displacement,
            changed=(
                contract.min_screen_change == 0
                and contract.min_gameplay_change == 0
            ),
        )
        return self._status(ActionStatus.VERIFYING, "awaiting verified screen transition")

    def observe(self, observation: GameObservation) -> ActionResult | None:
        pending = self.pending
        if pending is None:
            return None
        if pending.result.action == ActionName.CLICK_LOCAL_TARGET:
            if (pending.local_origin is not None and observation.local_displacement is not None
                    and observation.production_ready and observation.is_fresh(self.clock())
                    and observation.runtime_id == pending.result.runtime_id
                    and observation.runtime_generation == pending.result.runtime_generation
                    and observation.worker_generation == pending.result.worker_generation
                    and observation.source_sequence > pending.last_sequence
                    and observation.source_captured_monotonic >= pending.sent_at
                    and observation.screen == DSTScreen.IN_WORLD_IDLE):
                dx = observation.local_displacement[0] - pending.local_origin[0]
                dy = observation.local_displacement[1] - pending.local_origin[1]
                if dx*dx + dy*dy >= 4:
                    return self._status(ActionStatus.SUCCEEDED, "fresh local click displacement", clear=True)
            if self.clock() - pending.sent_at >= 3:
                return self._status(ActionStatus.TIMED_OUT, "local click not verified; next cycle point", clear=True)
            return None
        if pending.local_origin is not None and pending.result.action in {
                ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD,
                ActionName.TURN_LEFT, ActionName.TURN_RIGHT}:
            if (observation.production_ready and observation.is_fresh(self.clock())
                    and observation.runtime_id == pending.result.runtime_id
                    and observation.runtime_generation == pending.result.runtime_generation
                    and observation.worker_generation == pending.result.worker_generation
                    and observation.source_sequence > pending.last_sequence
                    and observation.source_captured_monotonic >= pending.sent_at
                    and observation.screen == DSTScreen.IN_WORLD_IDLE
                    and observation.local_displacement is not None):
                dx = observation.local_displacement[0] - pending.local_origin[0]
                dy = observation.local_displacement[1] - pending.local_origin[1]
                projected = {ActionName.TURN_RIGHT: dx, ActionName.TURN_LEFT: -dx,
                             ActionName.MOVE_FORWARD: -dy, ActionName.MOVE_BACKWARD: dy}[pending.result.action]
                if projected >= 2:
                    return self._status(ActionStatus.SUCCEEDED,
                                        "fresh world registration verifies directional displacement", clear=True)
            if self.clock() - pending.sent_at >= 3:
                return self._status(ActionStatus.TIMED_OUT,
                                    "local target temporarily obstructed or anchor unavailable", clear=True)
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
            or (
                observation.screen != DSTScreen.IN_WORLD_IDLE
                and observation.screen_confidence < pending.contract.confidence
            )
        ):
            pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
            return None
        pending.last_sequence = observation.source_sequence
        if pending.result.action == ActionName.HOVER_GIFT_ICON:
            if observation.source_captured_monotonic < pending.sent_at:
                return None
            if any(
                d.kind == "gift_hover_response" and d.detected and d.verified
                for d in observation.detections
            ):
                return self._status(
                    ActionStatus.SUCCEEDED,
                    "fresh localized gift hover response",
                    clear=True,
                )
            return None
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
        if (pending.contract.min_screen_change > 0
                and observation.screen_change is not None
                and observation.screen_change >= pending.contract.min_screen_change):
            pending.changed = True
        if (
            observation.gameplay_change is not None
            and observation.gameplay_change >= pending.contract.min_gameplay_change
        ):
            pending.changed = True
            if pending.movement_evidence is None:
                pending.movement_evidence = observation.gameplay_change
        if observation.screen in pending.contract.targets:
            if (pending.result.action == ActionName.CLICK_INWORLD_USE_LATER
                    and any(d.kind == "gift_icon" and d.detected and d.verified
                            and dict(d.metadata).get("availability") in {
                                "GIFT_AVAILABLE", "IN_WORLD_GIFT_PENDING"}
                            for d in observation.detections)):
                pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
                return None
            if any(
                (item := next(
                    (d for d in observation.detections if d.kind == kind), None
                )) is None
                or item.detected
                or not item.verified
                for kind in pending.contract.postcondition_hidden_detections
            ):
                pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
                return None
            if pending.result.action == ActionName.CLICK_GIFT_ICON:
                if observation.source_captured_monotonic < pending.sent_at:
                    return None
                if any(
                    not self._has_fresh_verified_anchor(observation, kind)
                    for kind in pending.contract.postcondition_detections
                ):
                    pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
                    return None
            if observation.screen == pending.candidate:
                pending.count += 1
            else:
                pending.candidate, pending.count = observation.screen, 1
            if (pending.count >= pending.contract.stable_observations
                    and pending.changed):
                evidence = (
                    f"gameplay ROI change={pending.movement_evidence:.6f}; "
                    if pending.movement_evidence is not None
                    else ""
                )
                return self._status(
                    ActionStatus.SUCCEEDED,
                    f"{evidence}perception verified transition to "
                    f"{observation.screen.value} ({observation.screen_confidence:.4f})",
                    clear=True,
                )
        elif (
            pending.result.action == ActionName.CLICK_HOST_GAME
            and observation.screen == DSTScreen.MAIN_MENU
            and observation.screen_confidence >= pending.contract.confidence
            and observation.screen_change is not None
            and observation.screen_change < 0.02
        ):
            if observation.screen == pending.candidate:
                pending.count += 1
            else:
                pending.candidate, pending.count = observation.screen, 1
            if pending.count >= pending.contract.stable_observations:
                return self._status(
                    ActionStatus.TIMED_OUT,
                    "fresh unchanged MAIN_MENU proves Host Game click had no effect",
                    clear=True,
                )
        else:
            pending.candidate, pending.count = DSTScreen.UNKNOWN, 0
        return None

    @staticmethod
    def _has_fresh_verified_anchor(observation: GameObservation, kind: str) -> bool:
        return any(
            item.kind == kind and item.detected and item.verified
            and item.bounds is not None and item.confidence >= 0.94
            for item in observation.detections
        )

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
    """Resolve fixed UI targets from the supported profile, others from vision."""
    contract = CONTRACTS.get(action)
    if contract is None:
        raise ValueError("action has no visual anchor contract")
    fixed_target = FIXED_UI_ACTION_TARGETS.get(action.value)
    if fixed_target is not None:
        point = DST_FIXED_1280X720.point(
            fixed_target, observation.frame_width, observation.frame_height
        )
        return point, Viewport(observation.frame_width, observation.frame_height)
    if action == ActionName.CLICK_GIFT_ICON:
        if not observation.is_fresh(time.monotonic()):
            raise ValueError("gift detection is stale; reacquire before clicking")
        detection = next(
            (
                item for item in observation.detections
                if item.kind == "gift_icon"
                and item.detected
                and item.verified
                and item.bounds is not None
                and item.confidence >= contract.confidence
                and dict(item.metadata).get("availability") == "GIFT_AVAILABLE"
            ),
            None,
        )
        if detection is None:
            raise ValueError("fresh claimable gift detection is unavailable")
        bounds = detection.bounds
        assert bounds is not None
        return NormalizedPoint(
            (bounds.left + bounds.right) / 2,
            (bounds.top + bounds.bottom) / 2,
        ), Viewport(observation.frame_width, observation.frame_height)
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
