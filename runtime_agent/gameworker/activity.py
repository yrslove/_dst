from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, deque
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol

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


class DailyGiftState(StrEnum):
    UNKNOWN = "UNKNOWN"
    GIFT_AVAILABILITY_UNKNOWN = "GIFT_AVAILABILITY_UNKNOWN"
    NO_REWARD_AVAILABLE = "NO_REWARD_AVAILABLE"
    GIFT_AVAILABLE = "GIFT_AVAILABLE"
    GIFT_INTERACTION_STARTED = "GIFT_INTERACTION_STARTED"
    GIFT_UI_OPEN = "GIFT_UI_OPEN"
    DAILY_GIFT_CONFIRMED = "DAILY_GIFT_CONFIRMED"
    GIFT_UI_CLOSED = "GIFT_UI_CLOSED"


class InWorldGiftState(StrEnum):
    UNKNOWN = "UNKNOWN"
    AVAILABILITY_UNKNOWN = "GIFT_AVAILABILITY_UNKNOWN"
    NO_GIFT = "NO_REWARD_AVAILABLE"
    PENDING_STATION = "IN_WORLD_GIFT_PENDING"
    ACTIONABLE = "IN_WORLD_GIFT_ACTIONABLE"
    OPENING = "IN_WORLD_GIFT_OPENING"
    RECEIVED = "IN_WORLD_GIFT_RECEIVED"
    CLAIMED = "IN_WORLD_GIFT_CLAIMED"
    UI_CLOSED = "IN_WORLD_GIFT_UI_CLOSED"
    CONFIRMED = "IN_WORLD_GIFT_CONFIRMED"


@dataclass(frozen=True, slots=True)
class DailyGiftConfirmation:
    semantic: str
    evidence_frame_id: str
    evidence_sequence: int
    observed_at: str
    action_id: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "semantic": self.semantic,
            "evidence_frame_id": self.evidence_frame_id,
            "evidence_sequence": self.evidence_sequence,
            "observed_at": self.observed_at,
            "action_id": self.action_id,
        }


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

    RECOVERABLE_WORLD_ENTRY_STATES = frozenset(
        {
            DSTScreen.MAIN_MENU,
            DSTScreen.HOST_GAME_WORLD_LIST,
            DSTScreen.HOST_GAME_WORLD_SELECTED,
            DSTScreen.MODS_DISABLED_CONFIRMATION,
            DSTScreen.CHARACTER_SELECTION,
            DSTScreen.CHARACTER_SELECTION_HOVERED,
            DSTScreen.CHARACTER_LOADOUT,
            DSTScreen.IN_WORLD_IDLE,
            DSTScreen.LOADING,
            DSTScreen.DEAD,
            DSTScreen.WORLD_RESET_PENDING,
        }
    )
    RECOVERABLE_WORLD_ENTRY_ACTIONS = frozenset(
        {
            ActionName.CLICK_HOST_GAME,
            ActionName.SELECT_EXISTING_WORLD,
            ActionName.START_EXISTING_WORLD,
            ActionName.CONFIRM_MODS_DISABLED,
            ActionName.SELECT_SURVIVOR,
            ActionName.START_SURVIVOR,
        }
    )
    WORLD_ENTRY_CONTINUATIONS: ClassVar[dict[ActionName, frozenset[DSTScreen]]] = {
        ActionName.CLICK_HOST_GAME: frozenset(
            {
                DSTScreen.HOST_GAME_WORLD_LIST,
                DSTScreen.HOST_GAME_WORLD_SELECTED,
                DSTScreen.CHARACTER_SELECTION,
                DSTScreen.CHARACTER_SELECTION_HOVERED,
                DSTScreen.CHARACTER_LOADOUT,
                DSTScreen.IN_WORLD_IDLE,
            }
        ),
        ActionName.SELECT_EXISTING_WORLD: frozenset(
            {
                DSTScreen.HOST_GAME_WORLD_SELECTED,
                DSTScreen.CHARACTER_SELECTION,
                DSTScreen.CHARACTER_SELECTION_HOVERED,
                DSTScreen.CHARACTER_LOADOUT,
                DSTScreen.IN_WORLD_IDLE,
            }
        ),
        ActionName.START_EXISTING_WORLD: frozenset(
            {
                DSTScreen.MODS_DISABLED_CONFIRMATION,
                DSTScreen.CHARACTER_SELECTION,
                DSTScreen.CHARACTER_SELECTION_HOVERED,
                DSTScreen.CHARACTER_LOADOUT,
                DSTScreen.IN_WORLD_IDLE,
            }
        ),
        ActionName.SELECT_SURVIVOR: frozenset(
            {DSTScreen.CHARACTER_LOADOUT, DSTScreen.IN_WORLD_IDLE}
        ),
        ActionName.START_SURVIVOR: frozenset({DSTScreen.IN_WORLD_IDLE}),
        ActionName.TURN_LEFT: frozenset({DSTScreen.IN_WORLD_IDLE}),
        ActionName.MOVE_FORWARD: frozenset({
            DSTScreen.IN_WORLD_IDLE, DSTScreen.LOADING, DSTScreen.DEAD,
            DSTScreen.WORLD_RESET_PENDING, DSTScreen.MAIN_MENU,
            DSTScreen.CHARACTER_SELECTION, DSTScreen.CHARACTER_LOADOUT,
        }),
        ActionName.MOVE_BACKWARD: frozenset({
            DSTScreen.IN_WORLD_IDLE, DSTScreen.LOADING, DSTScreen.DEAD,
            DSTScreen.WORLD_RESET_PENDING, DSTScreen.MAIN_MENU,
            DSTScreen.CHARACTER_SELECTION, DSTScreen.CHARACTER_LOADOUT,
        }),
    }

    def __init__(
        self,
        *,
        validation_flow_enabled: bool = False,
        validation_movement_enabled: bool = False,
        locomotion=None,
    ) -> None:
        self.validation_flow_enabled = validation_flow_enabled
        self.validation_movement_enabled = validation_movement_enabled
        self.production_actions_enabled = False
        self.gift_claim_ready = True
        self.claim_evidence = None
        self._production_world_entry_action_id = None
        self.locomotion = locomotion
        self._reward_next_attempts = 0
        self.validation_complete = False
        self._validation_step = 0
        self._validation_host_retry_count = 0
        self._validation_host_retry_pending = False
        self._validation_host_retry_no_effect_proven = False
        self._validation_host_source_sequence: int | None = None
        self._production_host_retry_pending = False
        self._production_host_retry_used = False
        self._production_host_source_sequence: int | None = None
        self._production_host_retry_after_sequence: int | None = None
        self._production_host_no_effect_proven = False
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
        self.daily_gift_state = DailyGiftState.UNKNOWN
        self.inworld_gift_state = InWorldGiftState.UNKNOWN
        self.daily_gift_confirmation: DailyGiftConfirmation | None = None
        self.inworld_gift_confirmation: dict | None = None
        self.inworld_close_evidence: dict | None = None
        self._inworld_received_evidence: dict | None = None
        self._inworld_close_attempts = 0
        self.gift_availability_evidence: dict | None = None
        self._gift_hover_attempted = False
        self._gift_icon_click_attempts = 0
        self._gift_station_approach_attempted = False
        self._gift_station_approach_step = 0
        self._reward_open_source_sequence: int | None = None
        self._last_gift_observation_sequence = 0
        self._last_confirmed_gift_sequence = 0
        self._confirmed_gift_action_ids: deque[str] = deque(maxlen=32)
        self._dry_run_seen = False
        self.intervention_required = False
        self._recoverable_intervention_action: ActionName | None = None
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

    def set_production_actions_enabled(self, enabled: bool) -> None:
        self.production_actions_enabled = bool(enabled)

    def begin_next_gift_cycle(self, evidence) -> None:
        """Re-arm the existing claim flow after a durable receipt."""
        self.claim_evidence = evidence
        self.gift_claim_ready = evidence.ready
        self.inworld_gift_state = InWorldGiftState.UNKNOWN
        self.inworld_close_evidence = None
        self._inworld_received_evidence = None
        self._inworld_close_attempts = 0
        self._gift_hover_attempted = False
        self._gift_icon_click_attempts = 0
        self._gift_station_approach_attempted = False
        self._gift_station_approach_step = 0
        self._awaiting_reward_transition = False
        self.intervention_required = False
        self._recoverable_intervention_action = None

    def _recoverable_action(self, action):
        return (action in {ActionName.TURN_RIGHT, ActionName.MOVE_BACKWARD}
                and self._gift_station_approach_attempted
                and self._gift_station_approach_step < 4) or action in self.RECOVERABLE_WORLD_ENTRY_ACTIONS or (
            self.locomotion is not None and self.locomotion.profile
            and action in {ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD}
        )

    @property
    def recoverable_intervention_pending(self) -> bool:
        return self._recoverable_intervention_action is not None

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
        if self.claim_evidence is not None:
            self.claim_evidence.observe(observation)
            self.gift_claim_ready = self.claim_evidence.ready
        if not observation.production_ready:
            self._validation_world_frames = 0
            self._validation_last_world_sequence = None
            self.on_unknown(observation)
            return None
        icon = next((d for d in observation.detections if d.kind == "gift_icon"), None)
        if observation.screen == DSTScreen.IN_WORLD_IDLE:
            if not observation.is_fresh(time.monotonic()):
                self.on_unknown(observation)
                return None
            evidence = dict(icon.metadata) if icon is not None else {}
            availability = evidence.get("availability", "GIFT_AVAILABILITY_UNKNOWN")
            world_states = {
                "IN_WORLD_GIFT_PENDING": InWorldGiftState.PENDING_STATION,
                "GIFT_AVAILABLE": InWorldGiftState.ACTIONABLE,
                "NO_REWARD_AVAILABLE": InWorldGiftState.NO_GIFT,
                "GIFT_AVAILABILITY_UNKNOWN": InWorldGiftState.AVAILABILITY_UNKNOWN,
            }
            self.inworld_gift_state = world_states.get(
                availability, InWorldGiftState.AVAILABILITY_UNKNOWN
            )
            self.gift_availability_evidence = {
                **evidence,
                "semantic": self.inworld_gift_state.value,
                "icon_present": bool(icon and icon.detected and icon.verified),
                "identity_confidence": icon.confidence if icon else 0.0,
                "evidence_frame_id": observation.source_frame_id,
                "evidence_sequence": observation.source_sequence,
                "observed_at": observation.timestamp,
                "calibration_profile": observation.calibration_profile_id,
                "icon_bounds": (
                    [
                        icon.bounds.left,
                        icon.bounds.top,
                        icon.bounds.right,
                        icon.bounds.bottom,
                    ]
                    if icon and icon.bounds
                    else None
                ),
            }
        elif observation.screen == DSTScreen.IN_WORLD_GIFT_OPENING:
            self.inworld_gift_state = InWorldGiftState.OPENING
        elif observation.screen == DSTScreen.IN_WORLD_GIFT_RECEIVED:
            self.inworld_gift_state = InWorldGiftState.RECEIVED
            self._inworld_received_evidence = {
                "runtime_id": observation.runtime_id,
                "runtime_generation": observation.runtime_generation,
                "received_worker_generation": observation.worker_generation,
                "received_frame_id": observation.source_frame_id,
                "received_sequence": observation.source_sequence,
                "received_at": observation.timestamp,
            }
        if observation.source_sequence > self._last_gift_observation_sequence:
            self._last_gift_observation_sequence = observation.source_sequence
            if observation.screen == DSTScreen.LOGIN_REWARD_AVAILABLE:
                self.daily_gift_state = DailyGiftState.GIFT_AVAILABLE
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
        if self.intervention_required:
            self._record(observation, "NONE", "intervention required")
            return None
        if (
            self.production_actions_enabled
            and observation.screen == DSTScreen.IN_WORLD_GIFT_RECEIVED
            and self._inworld_close_attempts < 2
        ):
            self._inworld_close_attempts += 1
            return ActionProposal(
                ActionName.CLICK_INWORLD_USE_LATER,
                reason="finish the verified in-world item receipt with Use Later",
            )
        if (
            self.production_actions_enabled
            and observation.screen == DSTScreen.IN_WORLD_IDLE
            and self.inworld_gift_state == InWorldGiftState.PENDING_STATION
            and self._gift_station_approach_step < 4
        ):
            self._gift_station_approach_attempted = True
            route = (ActionName.TURN_RIGHT, ActionName.MOVE_BACKWARD) * 2
            action = route[self._gift_station_approach_step]
            return ActionProposal(
                action,
                duration=1.0,
                reason="take a bounded canonical step along the prepared Science Machine approach",
            )
        if (
            self.production_actions_enabled
            and observation.screen == DSTScreen.IN_WORLD_IDLE
            and icon is not None
            and icon.detected
            and icon.verified
            and icon.bounds is not None
            and icon.confidence >= 0.94
            and (
                dict(icon.metadata).get("availability") == "GIFT_AVAILABLE"
                or (
                    dict(icon.metadata).get("availability") == "IN_WORLD_GIFT_PENDING"
                    and self._gift_station_approach_step >= 4
                )
            )
            and self.gift_claim_ready
            and self._gift_icon_click_attempts < 2
        ):
            self._gift_icon_click_attempts += 1
            reason = (
                "open the verified pending in-world gift after the bounded station approach"
                if dict(icon.metadata).get("availability") == "IN_WORLD_GIFT_PENDING"
                else "open the reward UI from the fresh active gift icon"
            )
            return ActionProposal(ActionName.CLICK_GIFT_ICON, reason=reason)
        if (
            self.production_actions_enabled
            and observation.screen == DSTScreen.IN_WORLD_IDLE
            and icon is not None
            and icon.detected
            and icon.verified
            and icon.bounds is not None
            and 0.85 <= icon.confidence < 0.94
            and not self._gift_hover_attempted
        ):
            self._gift_hover_attempted = True
            return ActionProposal(
                ActionName.HOVER_GIFT_ICON,
                reason="verify the localized gift HUD candidate once",
            )
        if self.validation_flow_enabled and self._validation_host_retry_pending:
            self._validation_host_retry_pending = False
            if (
                observation.screen != DSTScreen.MAIN_MENU
                or observation.screen_confidence < 0.94
                or observation.source_sequence
                <= (self._validation_host_source_sequence or 0)
                or (
                    not self._validation_host_retry_no_effect_proven
                    and (
                        observation.screen_change is None
                        or observation.screen_change >= 0.02
                    )
                )
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
            self._validation_host_retry_no_effect_proven = False
            self._record(observation, proposal.action.value, proposal.reason or "")
            return proposal
        if self.validation_flow_enabled and self._validation_survivor_retry_pending:
            self._validation_survivor_retry_pending = False
            if (
                observation.screen == DSTScreen.CHARACTER_LOADOUT
                and observation.screen_confidence >= 0.94
                and observation.source_sequence
                > (self._validation_survivor_source_sequence or 0)
            ):
                self._validation_step = 4
                self._record(
                    observation,
                    "NONE",
                    "loadout verified after survivor action deadline",
                )
                return None
            anchor_name = (
                "character_select_wilson_hover"
                if observation.screen == DSTScreen.CHARACTER_SELECTION_HOVERED
                else "character_select_wilson_icon"
            )
            anchor = next(
                (
                    item
                    for item in observation.detections
                    if item.kind == anchor_name
                    and item.detected
                    and item.verified
                    and item.bounds is not None
                    and item.confidence >= 0.94
                ),
                None,
            )
            if (
                observation.screen
                not in {
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
                    observation,
                    "NONE",
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
            next_button = next((
                item for item in observation.detections
                if item.kind == "login_reward_next_button" and item.detected
                and item.verified and item.bounds is not None and item.confidence >= .94
            ), None)
            if next_button is not None and self._reward_next_attempts < 2:
                self._reward_next_attempts += 1
                return ActionProposal(
                    ActionName.CLICK_REWARD_NEXT,
                    reason="advance the verified first-login promotional reward",
                )
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
        if not self.validation_flow_enabled and self.production_actions_enabled:
            if self._production_host_retry_pending:
                source_sequence = self._production_host_retry_after_sequence
                if (
                    observation.screen != DSTScreen.MAIN_MENU
                    or observation.screen_confidence < 0.94
                    or source_sequence is None
                    or observation.source_sequence <= source_sequence
                    or (
                        not self._production_host_no_effect_proven
                        and (
                            observation.screen_change is None
                            or observation.screen_change >= 0.02
                        )
                    )
                ):
                    self._production_host_retry_pending = False
                    self.intervention_required = True
                    self._recoverable_intervention_action = ActionName.CLICK_HOST_GAME
                    self._record(
                        observation,
                        "NONE",
                        "production Host Game retry withheld without fresh unchanged MAIN_MENU",
                    )
                    return None
                self._production_host_retry_pending = False
                self._production_host_retry_used = True
                self._production_host_no_effect_proven = False
                self._production_host_source_sequence = observation.source_sequence
                proposal = ActionProposal(
                    ActionName.CLICK_HOST_GAME,
                    reason="retry Host Game once at its fixed profile point after verified no effect",
                )
                self._record(observation, proposal.action.value, proposal.reason or "")
                return proposal
            proposal = self._propose_production_world_entry(observation)
            if proposal is not None:
                self._record(observation, proposal.action.value, proposal.reason or "")
                if proposal.action == ActionName.CLICK_HOST_GAME:
                    self._production_host_source_sequence = observation.source_sequence
                return proposal
        if self.production_actions_enabled and self.locomotion:
            movement = self.locomotion.proposal(observation)
            if movement is not None:
                return ActionProposal(movement[0], duration=movement[1],
                                      reason=f"{self.locomotion.profile} locomotion")
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
                    self._record(
                        observation, proposal.action.value, proposal.reason or ""
                    )
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
                (
                    DSTScreen.MAIN_MENU,
                    0,
                    ActionName.CLICK_HOST_GAME,
                    "open the detected Host Game menu",
                ),
                (
                    DSTScreen.HOST_GAME_WORLD_LIST,
                    1,
                    ActionName.SELECT_EXISTING_WORLD,
                    "select the detected existing saved world",
                ),
                (
                    DSTScreen.HOST_GAME_WORLD_SELECTED,
                    2,
                    ActionName.START_EXISTING_WORLD,
                    "start the selected existing world",
                ),
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
                    self._record(
                        observation, proposal.action.value, proposal.reason or ""
                    )
                    return proposal
                if self._validation_step == 6:
                    proposal = ActionProposal(
                        ActionName.PAUSE_WORLD,
                        reason="return to safe pause after bounded movement validation",
                    )
                    self._record(
                        observation, proposal.action.value, proposal.reason or ""
                    )
                    return proposal
                if self._validation_step == 7:
                    proposal = ActionProposal(
                        ActionName.PAUSE_WORLD,
                        reason="return to safe pause after bounded in-world actions",
                    )
                    self._record(
                        observation, proposal.action.value, proposal.reason or ""
                    )
                    return proposal
                if self._validation_step == 8:
                    proposal = ActionProposal(
                        ActionName.PAUSE_WORLD,
                        reason="leave DST in a verified safe pause after validation",
                    )
                    self._record(
                        observation, proposal.action.value, proposal.reason or ""
                    )
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
                        self._record(
                            observation, proposal.action.value, proposal.reason or ""
                        )
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
                        if item.detected and item.verified and item.confidence >= 0.94
                    }
                    if (
                        not {
                            "character_select_wilson_name",
                            icon,
                        }
                        <= required_anchors
                        or observation.screen_confidence < 0.94
                    ):
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
                if observation.screen_confidence < 0.94:
                    self._record(
                        observation, "NONE", "loadout screen confidence insufficient"
                    )
                    return None
                proposal = ActionProposal(
                    ActionName.START_SURVIVOR,
                    reason="start the verified Wilson loadout",
                )
                self._record(observation, proposal.action.value, proposal.reason)
                return proposal
            for source, step, action, reason in route:
                if self._validation_step == step and self.state == source:
                    if observation.screen_confidence < 0.94:
                        self._record(
                            observation,
                            "NONE",
                            "validation screen confidence insufficient",
                        )
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

    def resolve_recoverable_intervention(
        self,
        observation: GameObservation,
        *,
        no_action_in_flight: bool,
        input_released: bool,
    ) -> bool:
        """Adopt a fresh known continuation state after a world-entry timeout."""
        failed_action = self._recoverable_intervention_action
        if (
            not self.intervention_required
            or failed_action is None
            or self.validation_flow_enabled
            or not observation.production_ready
            or observation.fresh_until < time.monotonic()
            or (
                observation.screen != DSTScreen.IN_WORLD_IDLE
                and observation.screen_confidence < 0.94
            )
            or observation.screen not in self.RECOVERABLE_WORLD_ENTRY_STATES
            or observation.screen
            not in self.WORLD_ENTRY_CONTINUATIONS.get(failed_action, frozenset())
            or self.state != observation.screen
            or self._candidate_frames < 2
            or not no_action_in_flight
            or not input_released
        ):
            return False
        self.intervention_required = False
        self._recoverable_intervention_action = None
        self._record(
            observation,
            "NONE",
            f"recoverable {failed_action.value} timeout resolved by fresh "
            f"{observation.screen.value}",
        )
        return True

    @staticmethod
    def _propose_production_world_entry(
        observation: GameObservation,
    ) -> ActionProposal | None:
        if observation.screen_confidence < 0.94:
            return None
        actions = {
            DSTScreen.MAIN_MENU: (
                ActionName.CLICK_HOST_GAME,
                "open the verified Host Game menu using the fixed UI profile",
            ),
            DSTScreen.HOST_GAME_WORLD_LIST: (
                ActionName.SELECT_EXISTING_WORLD,
                "select Farm 01 using the fixed UI profile",
            ),
            DSTScreen.HOST_GAME_WORLD_SELECTED: (
                ActionName.START_EXISTING_WORLD,
                "resume Farm 01 using the fixed UI profile",
            ),
            DSTScreen.MODS_DISABLED_CONFIRMATION: (
                ActionName.CONFIRM_MODS_DISABLED,
                "confirm the verified missing-mod warning using the fixed UI profile",
            ),
            DSTScreen.CHARACTER_SELECTION: (
                ActionName.SELECT_SURVIVOR,
                "select Wilson using the fixed UI profile",
            ),
            DSTScreen.CHARACTER_SELECTION_HOVERED: (
                ActionName.SELECT_SURVIVOR,
                "select Wilson using the fixed UI profile",
            ),
            DSTScreen.CHARACTER_LOADOUT: (
                ActionName.START_SURVIVOR,
                "start the verified Wilson loadout using the fixed UI profile",
            ),
        }
        action = actions.get(observation.screen)
        return ActionProposal(action[0], reason=action[1]) if action else None

    def on_unknown(self, observation: GameObservation) -> None:
        if self.daily_gift_state in {
            DailyGiftState.UNKNOWN,
            DailyGiftState.GIFT_AVAILABILITY_UNKNOWN,
            DailyGiftState.NO_REWARD_AVAILABLE,
            DailyGiftState.GIFT_AVAILABLE,
        }:
            self.daily_gift_state = DailyGiftState.GIFT_AVAILABILITY_UNKNOWN
            self.gift_availability_evidence = None
        self.counters["unknown_frames"] += 1
        self._record(observation, "NONE", "unverified or unknown screen")

    def on_action_result(
        self, observation: GameObservation, result: ActionResult
    ) -> ActionResult:
        if self.locomotion:
            self.locomotion.sent(result)
        if result.action == ActionName.HOVER_GIFT_ICON:
            # Verification failure leaves availability unknown; never retry or click.
            self._record(
                observation,
                result.action.value,
                "bounded gift hover",
                result.status.value,
            )
            return result
        if result.action in {
            ActionName.CLICK_GIFT_ICON,
            ActionName.CLICK_INWORLD_USE_LATER,
            ActionName.OPEN_INVENTORY,
            ActionName.OPEN_CRAFTING_MENU,
            ActionName.CLICK_REWARD_OPEN,
            ActionName.CLICK_OPTIONS,
            ActionName.CLICK_REWARD_CLOSE,
            ActionName.CLICK_REWARD_NEXT,
            ActionName.CLICK_BACK,
            ActionName.DISCARD_OPTIONS,
            ActionName.CLICK_HOST_GAME,
            ActionName.SELECT_SURVIVAL,
            ActionName.SELECT_NO_CAVES,
            ActionName.SELECT_EXISTING_WORLD,
            ActionName.START_EXISTING_WORLD,
            ActionName.CONFIRM_MODS_DISABLED,
            ActionName.SELECT_SURVIVOR,
            ActionName.START_SURVIVOR,
            ActionName.MOVE_FORWARD,
            ActionName.MOVE_BACKWARD,
            ActionName.TURN_LEFT,
            ActionName.TURN_RIGHT,
            ActionName.CANCEL,
            ActionName.RESUME_WORLD,
            ActionName.PAUSE_WORLD,
            ActionName.INTERACT,
        }:
            if result.status == ActionStatus.VERIFYING:
                if (self.production_actions_enabled and not self.validation_flow_enabled
                        and self._recoverable_action(result.action)):
                    self._production_world_entry_action_id = result.action_id
                self.counters["active_actions"] += 1
                self._awaiting_reward_transition = True
                if result.action == ActionName.CLICK_REWARD_OPEN:
                    self._reward_open_source_sequence = observation.source_sequence
                    self._last_gift_observation_sequence = max(
                        self._last_gift_observation_sequence,
                        observation.source_sequence,
                    )
                    self.daily_gift_state = DailyGiftState.GIFT_INTERACTION_STARTED
            elif (
                result.action == ActionName.CLICK_GIFT_ICON
                and result.status == ActionStatus.TIMED_OUT
                and self.production_actions_enabled
                and observation.screen == DSTScreen.IN_WORLD_IDLE
                and self._gift_icon_click_attempts < 2
            ):
                # The verified-transition handler below permits one fresh
                # production retry when the world stayed in-world and the
                # actionable gift icon is still independently detected.
                self._awaiting_reward_transition = False
            elif result.status == ActionStatus.SUPPRESSED:
                self._dry_run_seen = True
            elif result.status not in {ActionStatus.PREEMPTED} and result.terminal:
                self.intervention_required = True
                self._recoverable_intervention_action = (
                    result.action
                    if (
                        not self.validation_flow_enabled
                        and self.production_actions_enabled
                        and result.status == ActionStatus.TIMED_OUT
                        and self._recoverable_action(result.action)
                    )
                    else None
                )
                self.counters["recovery_failures"] += 1
        self._record(
            observation, result.action.value, "action result", result.status.value
        )
        return result

    def on_verified(self, observation: GameObservation, result: ActionResult) -> None:
        if self.locomotion:
            self.locomotion.verified(result)
        if (result.status == ActionStatus.SUCCEEDED
                and self._gift_station_approach_step < 4
                and result.action == (ActionName.TURN_RIGHT, ActionName.MOVE_BACKWARD)[
                    self._gift_station_approach_step % 2
                ]):
            self._gift_station_approach_step += 1
        if result.action == ActionName.CLICK_INWORLD_USE_LATER:
            self._awaiting_reward_transition = False
            if result.status == ActionStatus.SUCCEEDED and self._inworld_received_evidence:
                self.inworld_gift_state = InWorldGiftState.UI_CLOSED
                self.inworld_close_evidence = {
                    **self._inworld_received_evidence,
                    "worker_generation": observation.worker_generation,
                    "evidence_frame_id": observation.source_frame_id,
                    "evidence_sequence": observation.source_sequence,
                    "observed_at": observation.timestamp,
                    "action_id": result.action_id,
                }
                self.counters["verified_actions"] += 1
            else:
                # Retry only closing the already received popup, from fresh perception.
                self.intervention_required = not (
                    result.status == ActionStatus.TIMED_OUT
                    and self._inworld_close_attempts < 2
                )
            self._record(observation, result.action.value, "in-world receipt close", result.status.value)
            return
        if result.action == ActionName.OPEN_CRAFTING_MENU:
            self._awaiting_reward_transition = False
            if result.status == ActionStatus.SUCCEEDED:
                self.counters["verified_actions"] += 1
            else:
                self.intervention_required = True
            self._record(
                observation,
                result.action.value,
                "opened the DST crafting menu to enable the prepared giftmachine",
                result.status.value,
            )
            return
        if result.action == ActionName.HOVER_GIFT_ICON:
            if result.status != ActionStatus.SUCCEEDED:
                self.on_unknown(observation)
            return
        if result.action == ActionName.CLICK_GIFT_ICON:
            self._awaiting_reward_transition = False
            if result.status == ActionStatus.SUCCEEDED:
                self.inworld_gift_state = (
                    InWorldGiftState.RECEIVED
                    if observation.screen == DSTScreen.IN_WORLD_GIFT_RECEIVED
                    else InWorldGiftState.OPENING
                )
                self._gift_icon_click_attempts = 2
                self.counters["verified_actions"] += 1
            elif (
                result.status == ActionStatus.TIMED_OUT
                and observation.screen == DSTScreen.IN_WORLD_IDLE
                and self._gift_icon_click_attempts < 2
            ):
                # Retry only through the normal planner on a later fresh frame;
                # it must still detect the active icon before proposing a click.
                self.inworld_gift_state = InWorldGiftState.ACTIONABLE
            else:
                self.intervention_required = True
            self._record(
                observation,
                result.action.value,
                "gift icon action outcome",
                result.status.value,
            )
            return
        self._awaiting_reward_transition = False
        if result.status == ActionStatus.SUCCEEDED:
            self.counters["verified_actions"] += 1
            self.state = observation.screen
            new_gift_observation = (
                observation.source_sequence > self._last_gift_observation_sequence
            )
            if new_gift_observation:
                self._last_gift_observation_sequence = observation.source_sequence
            if result.action == ActionName.CLICK_REWARD_OPEN and new_gift_observation:
                if observation.screen == DSTScreen.REWARD_RESULT:
                    if observation.source_sequence > self._last_confirmed_gift_sequence:
                        self.daily_gift_state = DailyGiftState.GIFT_UI_OPEN
                    result_title = any(
                        item.kind == "login_reward_result_title"
                        and item.detected
                        and item.verified
                        for item in observation.detections
                    )
                    close_anchor = any(
                        item.kind in {"login_reward_close_button", "login_reward_next_button"}
                        and item.detected
                        and item.verified
                        for item in observation.detections
                    )
                    if (
                        result_title
                        and close_anchor
                        and observation.production_ready
                        and self._reward_open_source_sequence is not None
                        and observation.source_sequence
                        > self._reward_open_source_sequence
                        and observation.source_sequence
                        > self._last_confirmed_gift_sequence
                        and result.action_id not in self._confirmed_gift_action_ids
                    ):
                        self.daily_gift_state = DailyGiftState.DAILY_GIFT_CONFIRMED
                        self.daily_gift_confirmation = DailyGiftConfirmation(
                            semantic=DailyGiftState.DAILY_GIFT_CONFIRMED.value,
                            evidence_frame_id=observation.source_frame_id,
                            evidence_sequence=observation.source_sequence,
                            observed_at=observation.timestamp,
                            action_id=result.action_id,
                        )
                        self._last_confirmed_gift_sequence = observation.source_sequence
                        self._confirmed_gift_action_ids.append(result.action_id)
                        self.counters["reward_claimed"] += 1
                        self.counters["gift_claimed"] += 1
                elif observation.screen == DSTScreen.MAIN_MENU:
                    self.daily_gift_state = DailyGiftState.GIFT_UI_CLOSED
                self._reward_open_source_sequence = None
            elif (
                result.action == ActionName.CLICK_REWARD_CLOSE and new_gift_observation
            ):
                self.daily_gift_state = DailyGiftState.GIFT_UI_CLOSED
            if self.validation_flow_enabled:
                validation_steps = {
                    ActionName.CLICK_HOST_GAME: 1,
                }
                expected = validation_steps.get(result.action)
                if expected is not None and self._validation_step == expected - 1:
                    self._validation_step = expected
                elif result.action == ActionName.SELECT_EXISTING_WORLD:
                    self._validation_step = (
                        3
                        if observation.screen
                        in {
                            DSTScreen.LOADING,
                            DSTScreen.IN_WORLD_IDLE,
                        }
                        else 2
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
                elif self.validation_flow_enabled and result.action in {
                    ActionName.CANCEL,
                    ActionName.PAUSE_WORLD,
                }:
                    self.validation_complete = True
                elif result.action == ActionName.RESUME_WORLD:
                    self._validation_step = 3
            if result.action == ActionName.CLICK_REWARD_OPEN:
                self.counters["reward_opened"] += 1
        else:
            if (
                not self.validation_flow_enabled
                and self.production_actions_enabled
                and result.action == ActionName.CLICK_HOST_GAME
                and result.status == ActionStatus.TIMED_OUT
                and not self._production_host_retry_used
                and result.reason
                in {
                    "verified transition deadline elapsed",
                    "fresh unchanged MAIN_MENU proves Host Game click had no effect",
                }
            ):
                self._production_host_retry_pending = True
                self._production_host_retry_after_sequence = (
                    observation.source_sequence
                    if result.reason
                    == "fresh unchanged MAIN_MENU proves Host Game click had no effect"
                    else self._production_host_source_sequence
                )
                self._production_host_no_effect_proven = (
                    result.reason
                    == "fresh unchanged MAIN_MENU proves Host Game click had no effect"
                )
                self._record(
                    observation,
                    result.action.value,
                    "production Host Game click had no effect; one guarded retry is available",
                    result.status.value,
                )
                return
            if (
                self.validation_flow_enabled
                and result.action == ActionName.CLICK_HOST_GAME
                and result.status == ActionStatus.TIMED_OUT
                and result.reason
                == "fresh unchanged MAIN_MENU proves Host Game click had no effect"
            ):
                self._validation_host_retry_no_effect_proven = True
            self.on_action_failure(result)

    def on_action_failure(self, result: ActionResult) -> None:
        if self.locomotion:
            self.locomotion.verified(result)
        self._awaiting_reward_transition = False
        if (
            not self.validation_flow_enabled
            and self.production_actions_enabled
            and result.action == ActionName.CLICK_HOST_GAME
            and result.status == ActionStatus.TIMED_OUT
            and result.reason == "verified transition deadline elapsed"
            and not self._production_host_retry_used
        ):
            self._production_host_retry_pending = True
            self._production_host_retry_after_sequence = (
                self._production_host_source_sequence
            )
            self._production_host_no_effect_proven = False
            return
        if (
            self.validation_flow_enabled
            and result.action == ActionName.CLICK_HOST_GAME
            and result.status == ActionStatus.TIMED_OUT
            and result.reason
            in {
                "verified transition deadline elapsed",
                "fresh unchanged MAIN_MENU proves Host Game click had no effect",
            }
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
        self._recoverable_intervention_action = (
            result.action
            if (
                not self.validation_flow_enabled
                and (self.production_actions_enabled
                     or result.action_id == self._production_world_entry_action_id)
                and result.status == ActionStatus.TIMED_OUT
                and self._recoverable_action(result.action)
            )
            else None
        )
        self.counters["recovery_failures"] += 1

    def next_action(self, observation: GameObservation) -> ActionName:
        proposal = self.propose(observation)
        return proposal.action if proposal else ActionName.NONE
