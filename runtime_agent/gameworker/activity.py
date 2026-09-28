from __future__ import annotations

import json
import logging
import math
from collections import Counter, deque
from dataclasses import dataclass
from typing import Protocol

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.vision import DSTScreen, GameObservation

logger = logging.getLogger("runtime_agent.gameworker.behavior")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    action: ActionName
    duration: float | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionName) or self.action is ActionName.NONE:
            raise ValueError("proposal action is invalid")
        if self.duration is not None and (
            not math.isfinite(self.duration) or self.duration < 0
        ):
            raise ValueError("proposal duration is invalid")
        if self.reason is not None and len(self.reason) > 256:
            raise ValueError("proposal reason exceeds bound")


class Planner(Protocol):
    def propose(self, observation: GameObservation) -> ActionProposal | None: ...


class NullPlanner:
    def propose(self, observation: GameObservation) -> None:
        return None


class FakePlanner:
    def __init__(self, proposal: ActionProposal | None):
        self.proposal = proposal
        self.observations: list[GameObservation] = []

    def propose(self, observation: GameObservation) -> ActionProposal | None:
        self.observations.append(observation)
        return self.proposal


class ActivityController:
    """Conservative policy; validation flags enable only its one-shot test route."""

    def __init__(
        self,
        *,
        validation_flow_enabled: bool = False,
        validation_movement_enabled: bool = False,
    ) -> None:
        self.validation_flow_enabled = validation_flow_enabled
        self.validation_movement_enabled = validation_movement_enabled
        self.validation_complete = False
        self._validation_step = 0
        self._validation_host_retry_count = 0
        self._validation_host_retry_pending = False
        self._validation_host_source_sequence: int | None = None
        self._validation_survivor_retry_count = 0
        self._validation_survivor_retry_pending = False
        self._validation_survivor_source_sequence: int | None = None
        self._validation_world_frames = 0
        self._validation_last_world_sequence: int | None = None
        self.state = DSTScreen.UNKNOWN
        self._candidate = DSTScreen.UNKNOWN
        self._candidate_frames = 0
        self._entered_at: float | None = None
        self._awaiting_reward_transition = False
        self._reward_click_attempts = 0
        self._dry_run_seen = False
        self.intervention_required = False
        self.counters: Counter[str] = Counter(
            {
                "unknown_frames": 0,
                "reward_detected": 0,
                "reward_opened": 0,
                "reward_claimed": 0,
                "gift_detected": 0,
                "gift_claimed": 0,
                "reconnects": 0,
                "recovery_failures": 0,
                "active_actions": 0,
            }
        )
        self.decisions: deque[dict] = deque(maxlen=128)

    def _record(
        self,
        observation: GameObservation,
        action: str,
        reason: str,
        result: str | None = None,
    ) -> None:
        decision = {
            "timestamp": observation.timestamp,
            "observation": observation.source_frame_id,
            "state": observation.screen.value,
            "confidence": observation.screen_confidence,
            "chosen_action": action,
            "reason": reason,
            "action_result": result,
            "next_state": self.state.value,
        }
        self.decisions.append(decision)
        logger.info("dst_behavior_decision %s", json.dumps(decision, sort_keys=True))

    def propose(self, observation: GameObservation) -> ActionProposal | None:
        if not observation.production_ready:
            self._validation_world_frames = 0
            self._validation_last_world_sequence = None
            self.counters["unknown_frames"] += 1
            self._record(observation, "NONE", "unverified observation")
            return None
        if self.intervention_required:
            self._record(observation, "NONE", "intervention required")
            return None
        if observation.screen != self._candidate:
            self._candidate = observation.screen
            self._candidate_frames = 1
        else:
            self._candidate_frames += 1
        if self._candidate_frames >= 2 and observation.screen != self.state:
            self.state = observation.screen
            self._dry_run_seen = False
            self._entered_at = observation.observed_monotonic
            if (
                self._awaiting_reward_transition
                and self.state != DSTScreen.LOGIN_REWARD_AVAILABLE
            ):
                self._awaiting_reward_transition = False
                self._reward_click_attempts = 0
                self.counters["reward_modal_exited"] += 1
            if self.state == DSTScreen.LOGIN_REWARD_AVAILABLE:
                self.counters["reward_detected"] += 1
        if self._awaiting_reward_transition:
            self._record(observation, "NONE", "awaiting reward transition")
            return None
        if self.state != observation.screen or self._candidate_frames < 2:
            self._record(observation, "NONE", "hysteresis")
            return None
        if self.validation_flow_enabled and self._validation_host_retry_pending:
            self._validation_host_retry_pending = False
            host_anchor = next(
                (
                    item for item in observation.detections
                    if item.kind == "main_menu_host_game"
                    and item.detected and item.verified
                    and item.bounds is not None and item.confidence >= 0.94
                ),
                None,
            )
            if (
                observation.screen != DSTScreen.MAIN_MENU
                or observation.screen_confidence < 0.94
                or observation.source_sequence
                <= (self._validation_host_source_sequence or 0)
                or observation.screen_change is None
                or observation.screen_change >= 0.02
                or host_anchor is None
            ):
                self.intervention_required = True
                self._record(
                    observation,
                    "NONE",
                    "Host Game retry withheld; fresh unchanged menu anchor required",
                )
                return None
            proposal = ActionProposal(
                ActionName.CLICK_HOST_GAME,
                reason=(
                    "retry Host Game once after a fresh unchanged menu frame "
                    "confirmed the verified anchor"
                ),
            )
            self._record(observation, proposal.action.value, proposal.reason or "")
            return proposal
        if self._validation_survivor_retry_pending:
            self._validation_survivor_retry_pending = False
            if (
                observation.screen == DSTScreen.CHARACTER_LOADOUT
                and observation.screen_confidence >= 0.94
                and observation.source_sequence
                > (self._validation_survivor_source_sequence or 0)
            ):
                self._validation_step = 4
                self._record(
                    observation, "NONE", "loadout verified after survivor action deadline",
                )
                return None
            anchor_name = (
                "character_select_wilson_hover"
                if observation.screen == DSTScreen.CHARACTER_SELECTION_HOVERED
                else "character_select_wilson_icon"
            )
            anchor = next((
                item for item in observation.detections
                if item.kind == anchor_name and item.detected and item.verified
                and item.bounds is not None and item.confidence >= 0.94
            ), None)
            if (
                observation.screen not in {
                    DSTScreen.CHARACTER_SELECTION,
                    DSTScreen.CHARACTER_SELECTION_HOVERED,
                }
                or observation.screen_confidence < 0.94
                or observation.source_sequence
                <= (self._validation_survivor_source_sequence or 0)
                or observation.screen_change is None
                or observation.screen_change >= 0.02
                or anchor is None
            ):
                self.intervention_required = True
                self._record(
                    observation, "NONE",
                    "survivor retry withheld; fresh unchanged portrait required",
                )
                return None
            proposal = ActionProposal(
                ActionName.SELECT_SURVIVOR,
                reason="retry the verified Wilson portrait once after no loadout transition",
            )
            self._record(observation, proposal.action.value, proposal.reason or "")
            return proposal
        if self.state == DSTScreen.REWARD_RESULT and not self.intervention_required:
            close_button = next(
                (
                    item
                    for item in observation.detections
                    if item.kind == "login_reward_close_button"
                    and item.detected
                    and item.verified
                    and item.bounds is not None
                    and item.confidence >= 0.94
                ),
                None,
            )
            if close_button is None or observation.screen_confidence < 0.94:
                self._record(observation, "NONE", "reward close anchor insufficient")
                return None
            proposal = ActionProposal(
                ActionName.CLICK_REWARD_CLOSE,
                reason="close the visually verified reward result modal",
            )
            self._record(observation, proposal.action.value, proposal.reason or "")
            return proposal
        if self.validation_flow_enabled:
            if self.state in {DSTScreen.DEAD, DSTScreen.WORLD_RESET_PENDING}:
                self._validation_step = 3
                self._validation_world_frames = 0
                self._validation_last_world_sequence = None
                self._record(observation, "NONE", "waiting for world reset")
                return None
            if (
                self.validation_movement_enabled
                and self._validation_step == 7
                and self.state == DSTScreen.PAUSED
            ):
                proposal = ActionProposal(
                    ActionName.RESUME_WORLD,
                    reason="resume the world for bounded pause/resume validation",
                )
                self._record(observation, proposal.action.value, proposal.reason or "")
                return proposal
            if self._validation_step == 0:
                if self.state == DSTScreen.PAUSED:
                    proposal = ActionProposal(
                        ActionName.RESUME_WORLD,
                        reason="resume a visually verified auto-paused world for validation",
                    )
                    self._record(observation, proposal.action.value, proposal.reason or "")
                    return proposal
                if self.state == DSTScreen.HOST_GAME_WORLD_LIST:
                    # Resume from the already-open saved-world list without
                    # replaying the menu click when validation starts mid-flow.
                    self._validation_step = 1
                elif self.state == DSTScreen.HOST_GAME_WORLD_SELECTED:
                    # Continue a world the worker already selected and verified.
                    self._validation_step = 2
                elif self.state == DSTScreen.CHARACTER_SELECTION:
                    # Resume at the live survivor-selection screen.
                    self._validation_step = 3
                elif self.state == DSTScreen.CHARACTER_LOADOUT:
                    self._validation_step = 4
                elif self.state == DSTScreen.IN_WORLD_IDLE:
                    self._validation_step = 3
            route = (
                (DSTScreen.MAIN_MENU, 0, ActionName.CLICK_HOST_GAME,
                 "open the detected Host Game menu"),
                (DSTScreen.HOST_GAME_WORLD_LIST, 1, ActionName.SELECT_EXISTING_WORLD,
                 "select the detected existing saved world"),
                (DSTScreen.HOST_GAME_WORLD_SELECTED, 2,
                 ActionName.START_EXISTING_WORLD,
                 "start the selected existing world"),
            )
            if (
                self.validation_movement_enabled
                and self.state == DSTScreen.IN_WORLD_IDLE
            ):
                if self._validation_step == 5:
                    proposal = ActionProposal(
                        ActionName.MOVE_BACKWARD,
                        duration=0.65,
                        reason="verify a short bounded backward movement",
                    )
                    self._record(observation, proposal.action.value, proposal.reason or "")
                    return proposal
                if self._validation_step == 6:
                    proposal = ActionProposal(
                        ActionName.PAUSE_WORLD,
                        reason="return to safe pause after bounded movement validation",
                    )
                    self._record(observation, proposal.action.value, proposal.reason or "")
                    return proposal
                if self._validation_step == 7:
                    proposal = ActionProposal(
                        ActionName.PAUSE_WORLD,
                        reason="return to safe pause after bounded in-world actions",
                    )
                    self._record(observation, proposal.action.value, proposal.reason or "")
                    return proposal
                if self._validation_step == 8:
                    proposal = ActionProposal(
                        ActionName.PAUSE_WORLD,
                        reason="leave DST in a verified safe pause after validation",
                    )
                    self._record(observation, proposal.action.value, proposal.reason or "")
                    return proposal
            if self._validation_step == 3:
                if self.state == DSTScreen.IN_WORLD_IDLE:
                    if (
                        observation.source_sequence
                        != self._validation_last_world_sequence
                    ):
                        self._validation_world_frames += 1
                        self._validation_last_world_sequence = (
                            observation.source_sequence
                        )
                    if self._validation_world_frames >= 4:
                        if self.validation_movement_enabled:
                            self._validation_step = 3
                            proposal = ActionProposal(
                                ActionName.MOVE_FORWARD,
                                duration=0.65,
                                reason=(
                                    "perform a short bounded forward movement after "
                                    "four fresh in-world observations"
                                ),
                            )
                        else:
                            proposal = ActionProposal(
                                ActionName.PAUSE_WORLD,
                                reason="open the pause menu after four fresh world frames",
                            )
                        self._record(observation, proposal.action.value, proposal.reason or "")
                        return proposal
                    else:
                        self._record(
                            observation,
                            "NONE",
                            "confirming stable in-world observations",
                        )
                    return None
                self._validation_world_frames = 0
                self._validation_last_world_sequence = None
                if self.state in {
                    DSTScreen.CHARACTER_SELECTION,
                    DSTScreen.CHARACTER_SELECTION_HOVERED,
                }:
                    icon = (
                        "character_select_wilson_hover"
                        if self.state == DSTScreen.CHARACTER_SELECTION_HOVERED
                        else "character_select_wilson_icon"
                    )
                    required_anchors = {
                        item.kind
                        for item in observation.detections
                        if item.detected
                        and item.verified
                        and item.confidence >= 0.94
                    }
                    if not {
                        "character_select_wilson_name",
                        icon,
                    } <= required_anchors or observation.screen_confidence < 0.94:
                        self._record(
                            observation,
                            "NONE",
                            "selected character is not sufficiently verified",
                        )
                        return None
                    proposal = ActionProposal(
                        ActionName.SELECT_SURVIVOR,
                        reason=(
                            "click the detected selected survivor to advance "
                            "the lobby to its loadout panel"
                        ),
                    )
                    self._validation_survivor_source_sequence = (
                        observation.source_sequence
                    )
                    self._record(observation, proposal.action.value, proposal.reason)
                    return proposal
                if self.state == DSTScreen.CHARACTER_LOADOUT:
                    self._validation_step = 4
                    self._record(
                        observation,
                        "NONE",
                        "loadout screen verified; waiting for its start target",
                    )
                    return None
            if self._validation_step == 4 and self.state == DSTScreen.CHARACTER_LOADOUT:
                button = next((
                    item for item in observation.detections
                    if item.kind == "character_loadout_go_button"
                    and item.detected and item.verified
                    and item.bounds is not None and item.confidence >= 0.94
                ), None)
                if button is None or observation.screen_confidence < 0.94:
                    self._record(observation, "NONE", "loadout Go anchor insufficient")
                    return None
                proposal = ActionProposal(
                    ActionName.START_SURVIVOR,
                    reason="start the verified Wilson loadout",
                )
                self._record(observation, proposal.action.value, proposal.reason)
                return proposal
            for source, step, action, reason in route:
                if self._validation_step == step and self.state == source:
                    contract_anchors = {
                        ActionName.CLICK_HOST_GAME: "main_menu_host_game",
                        ActionName.SELECT_EXISTING_WORLD:
                            "host_game_existing_world_row",
                        ActionName.START_EXISTING_WORLD:
                            "host_game_world_selected_start",
                        ActionName.SELECT_SURVIVAL:
                            "host_game_playstyle_survival",
                    }
                    anchor = next((
                        item for item in observation.detections
                        if item.kind == contract_anchors[action]
                        and item.detected and item.verified
                        and item.bounds is not None and item.confidence >= 0.94
                    ), None)
                    if anchor is None or observation.screen_confidence < 0.94:
                        self._record(observation, "NONE", "validation anchor insufficient")
                        return None
                    if action == ActionName.CLICK_HOST_GAME:
                        self._validation_host_source_sequence = (
                            observation.source_sequence
                        )
                    proposal = ActionProposal(action, reason=reason)
                    self._record(observation, action.value, reason)
                    return proposal
        if (
            self.state == DSTScreen.LOGIN_REWARD_AVAILABLE
            and not self.intervention_required
            and not self._dry_run_seen
        ):
            button = next(
                (
                    d
                    for d in observation.detections
                    if d.kind in {"login_reward_open_button", "login_reward_open_hover"}
                    and d.detected
                    and d.verified
                    and d.bounds is not None
                ),
                None,
            )
            if button is None or observation.screen_confidence < 0.94:
                self._record(observation, "NONE", "reward anchor insufficient")
                return None
            proposal = ActionProposal(
                ActionName.CLICK_REWARD_OPEN,
                reason="verified Thanks for playing and Open Now anchors",
            )
            self._record(observation, proposal.action.value, proposal.reason or "")
            return proposal
        self._record(observation, "NONE", "screen has no verified action")
        return None

    def on_unknown(self, observation: GameObservation) -> None:
        self.counters["unknown_frames"] += 1
        self._record(observation, "NONE", "unverified or unknown screen")

    def on_action_result(
        self, observation: GameObservation, result: ActionResult
    ) -> ActionResult:
        if result.action in {
            ActionName.CLICK_REWARD_OPEN, ActionName.CLICK_OPTIONS,
            ActionName.CLICK_REWARD_CLOSE, ActionName.CLICK_BACK,
            ActionName.DISCARD_OPTIONS,
            ActionName.CLICK_HOST_GAME, ActionName.SELECT_SURVIVAL,
            ActionName.SELECT_NO_CAVES, ActionName.SELECT_EXISTING_WORLD,
            ActionName.START_EXISTING_WORLD,
            ActionName.SELECT_SURVIVOR,
            ActionName.START_SURVIVOR,
            ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD,
            ActionName.CANCEL, ActionName.RESUME_WORLD,
            ActionName.PAUSE_WORLD,
            ActionName.INTERACT,
        }:
            if result.status == ActionStatus.VERIFYING:
                self.counters["active_actions"] += 1
                self._awaiting_reward_transition = True
            elif result.status == ActionStatus.SUPPRESSED:
                self._dry_run_seen = True
            elif result.status not in {ActionStatus.PREEMPTED} and result.terminal:
                self.intervention_required = True
                self.counters["recovery_failures"] += 1
        self._record(
            observation, result.action.value, "action result", result.status.value
        )
        return result

    def on_verified(self, observation: GameObservation, result: ActionResult) -> None:
        self._awaiting_reward_transition = False
        if result.status == ActionStatus.SUCCEEDED:
            self.counters["verified_actions"] += 1
            self.state = observation.screen
            if self.validation_flow_enabled:
                validation_steps = {
                    ActionName.CLICK_HOST_GAME: 1,
                }
                expected = validation_steps.get(result.action)
                if expected is not None and self._validation_step == expected - 1:
                    self._validation_step = expected
                elif result.action == ActionName.SELECT_EXISTING_WORLD:
                    self._validation_step = (
                        3 if observation.screen in {
                            DSTScreen.LOADING, DSTScreen.IN_WORLD_IDLE,
                        } else 2
                    )
                elif (
                    result.action == ActionName.START_EXISTING_WORLD
                    and self._validation_step == 2
                ):
                    self._validation_step = 3
                elif result.action == ActionName.SELECT_SURVIVOR:
                    self._validation_step = 4
                elif result.action == ActionName.START_SURVIVOR:
                    self._validation_step = 3
                elif result.action == ActionName.MOVE_FORWARD:
                    self._validation_step = 5
                elif result.action == ActionName.MOVE_BACKWARD:
                    self._validation_step = 6
                elif result.action == ActionName.INTERACT or (
                    result.action == ActionName.PAUSE_WORLD
                    and self.validation_movement_enabled
                    and self._validation_step == 6
                ):
                    self._validation_step = 7
                elif (
                    result.action == ActionName.RESUME_WORLD
                    and self.validation_movement_enabled
                    and self._validation_step == 7
                ):
                    self._validation_step = 8
                elif (
                    self.validation_flow_enabled
                    and result.action in {ActionName.CANCEL, ActionName.PAUSE_WORLD}
                ):
                    self.validation_complete = True
                elif result.action == ActionName.RESUME_WORLD:
                    self._validation_step = 3
            if result.action == ActionName.CLICK_REWARD_OPEN:
                self.counters["reward_opened"] += 1
        else:
            self.on_action_failure(result)

    def on_action_failure(self, result: ActionResult) -> None:
        self._awaiting_reward_transition = False
        if (
            self.validation_flow_enabled
            and result.action == ActionName.CLICK_HOST_GAME
            and result.status == ActionStatus.TIMED_OUT
            and result.reason == "verified transition deadline elapsed"
            and self._validation_host_retry_count == 0
            and self._validation_step == 0
        ):
            self._validation_host_retry_count = 1
            self._validation_host_retry_pending = True
            return
        if (
            self.validation_flow_enabled
            and result.action == ActionName.SELECT_SURVIVOR
            and result.status == ActionStatus.TIMED_OUT
            and result.reason == "verified transition deadline elapsed"
            and self._validation_survivor_retry_count == 0
            and self._validation_step == 3
        ):
            self._validation_survivor_retry_count = 1
            self._validation_survivor_retry_pending = True
            return
        self.intervention_required = True
        self.counters["recovery_failures"] += 1

    def next_action(self, observation: GameObservation) -> ActionName:
        proposal = self.propose(observation)
        return proposal.action if proposal else ActionName.NONE
