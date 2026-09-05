from __future__ import annotations

from runtime_agent.gameworker.actions import ActionName
from runtime_agent.gameworker.vision import GameObservation


class ActivityController:
    """Bounded policy, intentionally independent from visual detection."""

    def next_action(self, observation: GameObservation) -> ActionName:
        if (
            "UNKNOWN" in observation.diagnostic_flags
            or "UNCONFIGURED" in observation.diagnostic_flags
        ):
            return ActionName.NONE
        if observation.menu_visible.detected:
            return ActionName.CANCEL
        if observation.interaction_prompt_visible.detected:
            return ActionName.INTERACT
        if observation.game_visible.detected and observation.player_visible.detected:
            return ActionName.MOVE_FORWARD
        return ActionName.NONE
