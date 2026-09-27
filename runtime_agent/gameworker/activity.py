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
    """Conservative visual policy: only a verified login reward is actionable."""

    def __init__(self) -> None:
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
            self.counters["unknown_frames"] += 1
            self._record(observation, "NONE", "unverified observation")
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
            ActionName.CLICK_BACK, ActionName.DISCARD_OPTIONS,
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
            self.state = observation.screen
            if result.action == ActionName.CLICK_REWARD_OPEN:
                self.counters["reward_opened"] += 1
        else:
            self.on_action_failure(result)

    def on_action_failure(self, _result: ActionResult) -> None:
        self._awaiting_reward_transition = False
        self.intervention_required = True
        self.counters["recovery_failures"] += 1

    def next_action(self, observation: GameObservation) -> ActionName:
        proposal = self.propose(observation)
        return proposal.action if proposal else ActionName.NONE
