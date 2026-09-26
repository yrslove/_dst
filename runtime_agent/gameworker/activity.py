from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from runtime_agent.gameworker.actions import ActionName
from runtime_agent.gameworker.vision import GameObservation


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
    """Conservative placeholder policy; real strategy is outside Stage 3."""

    def propose(self, observation: GameObservation) -> ActionProposal | None:
        if not observation.production_ready:
            return None
        if observation.menu_visible.detected:
            return ActionProposal(ActionName.CANCEL, reason="verified menu")
        if observation.interaction_prompt_visible.detected:
            return ActionProposal(ActionName.INTERACT, reason="verified prompt")
        return None

    def next_action(self, observation: GameObservation) -> ActionName:
        proposal = self.propose(observation)
        return proposal.action if proposal else ActionName.NONE
