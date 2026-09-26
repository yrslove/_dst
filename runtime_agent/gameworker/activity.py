from __future__ import annotations

import json
import logging
import math
from collections import Counter, deque
from dataclasses import dataclass, replace
from typing import Protocol

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport
from runtime_agent.gameworker.vision import DSTScreen, GameObservation

logger = logging.getLogger("runtime_agent.gameworker.behavior")


@dataclass(frozen=True, slots=True)
class ActionProposal:
    action: ActionName
    duration: float | None = None
    reason: str | None = None
    target: NormalizedPoint | None = None
    viewport: Viewport | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.action, ActionName) or self.action is ActionName.NONE:
            raise ValueError("proposal action is invalid")
        if self.duration is not None and (
            not math.isfinite(self.duration) or self.duration < 0
        ):
            raise ValueError("proposal duration is invalid")
        if self.reason is not None and len(self.reason) > 256:
            raise ValueError("proposal reason exceeds bound")
        if self.action == ActionName.CLICK_REWARD_OPEN and (
            self.target is None or self.viewport is None
        ):
            raise ValueError("reward proposal requires a visual anchor")


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
        self._pending_result: ActionResult | None = None
        self._pending_since: float | None = None
        self._pending_state = DSTScreen.UNKNOWN
        self._pending_state_frames = 0
        self._pending_proposal: ActionProposal | None = None
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
            bounds = button.bounds
            assert bounds is not None
            proposal = ActionProposal(
                ActionName.CLICK_REWARD_OPEN,
                reason="verified Thanks for playing and Open Now anchors",
                target=NormalizedPoint(
                    (bounds.left + bounds.right) / 2, (bounds.top + bounds.bottom) / 2
                ),
                viewport=Viewport(observation.frame_width, observation.frame_height),
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
        if result.action == ActionName.CLICK_REWARD_OPEN:
            if result.status == ActionStatus.COMPLETED:
                self.counters["active_actions"] += 1
                self._reward_click_attempts += 1
                self._awaiting_reward_transition = True
                self._pending_result = result
                self._pending_since = observation.observed_monotonic
                self._pending_state = DSTScreen.UNKNOWN
                self._pending_state_frames = 0
                button = next(
                    (
                        item
                        for item in observation.detections
                        if item.kind
                        in {"login_reward_open_button", "login_reward_open_hover"}
                        and item.detected
                        and item.bounds is not None
                    ),
                    None,
                )
                if button is not None and button.bounds is not None:
                    box = button.bounds
                    point = NormalizedPoint(
                        (box.left + box.right) / 2, (box.top + box.bottom) / 2
                    )
                else:
                    point = NormalizedPoint(0.5, 0.85)
                self._pending_proposal = ActionProposal(
                    ActionName.CLICK_REWARD_OPEN,
                    reason="perception verification pending",
                    target=point,
                    viewport=Viewport(
                        observation.frame_width, observation.frame_height
                    ),
                )
                result = replace(
                    result,
                    status=ActionStatus.PENDING_VERIFICATION,
                    reason="input injected; awaiting a verified screen transition",
                )
            elif result.status == ActionStatus.SUPPRESSED:
                self._dry_run_seen = True
            elif result.status not in {ActionStatus.SUPPRESSED, ActionStatus.PREEMPTED}:
                self.intervention_required = True
                self.counters["recovery_failures"] += 1
        self._record(
            observation, result.action.value, "action result", result.status.value
        )
        return result

    def verify_observation(self, observation: GameObservation) -> ActionResult | None:
        pending = self._pending_result
        if pending is None or self._pending_since is None:
            return None
        if observation.production_ready and observation.screen in {
            DSTScreen.REWARD_RESULT,
            DSTScreen.MAIN_MENU,
        }:
            if observation.screen == self._pending_state:
                self._pending_state_frames += 1
            else:
                self._pending_state = observation.screen
                self._pending_state_frames = 1
            if self._pending_state_frames >= 2:
                self.state = observation.screen
                self._pending_result = None
                self._pending_since = None
                self._pending_proposal = None
                self._awaiting_reward_transition = False
                self._reward_click_attempts = 0
                self.counters["reward_opened"] += 1
                result = replace(
                    pending,
                    status=ActionStatus.COMPLETED,
                    reason=f"perception verified transition to {observation.screen.value}",
                )
                self._record(
                    observation,
                    result.action.value,
                    "visual transition verified",
                    result.status.value,
                )
                return result
        else:
            self._pending_state = DSTScreen.UNKNOWN
            self._pending_state_frames = 0
        if observation.observed_monotonic - self._pending_since >= 12:
            self._pending_result = None
            self._pending_since = None
            self._pending_proposal = None
            self._awaiting_reward_transition = False
            if self._reward_click_attempts >= 2:
                self.intervention_required = True
            result = replace(
                pending,
                status=ActionStatus.FAILED,
                reason="no verified reward screen transition within 12 seconds",
            )
            self.counters["recovery_failures"] += int(self.intervention_required)
            self.counters["reward_retry_ready"] += int(not self.intervention_required)
            self._record(
                observation,
                result.action.value,
                "visual transition timed out",
                result.status.value,
            )
            return result
        return None

    @property
    def pending_proposal(self) -> ActionProposal | None:
        return self._pending_proposal

    def next_action(self, observation: GameObservation) -> ActionName:
        proposal = self.propose(observation)
        return proposal.action if proposal else ActionName.NONE
