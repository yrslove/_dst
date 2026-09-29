"""Supported normalized-coordinate profile for deterministic DST UI controls."""
from __future__ import annotations

from dataclasses import dataclass

from runtime_agent.gameworker.geometry import NormalizedPoint


@dataclass(frozen=True, slots=True)
class FixedUiProfile:
    profile_id: str
    expected_width: int
    expected_height: int
    points: tuple[tuple[str, NormalizedPoint], ...]

    def __post_init__(self) -> None:
        if not self.profile_id or self.expected_width <= 0 or self.expected_height <= 0:
            raise ValueError("fixed UI profile metadata is invalid")
        names = [name for name, _ in self.points]
        if len(names) != len(set(names)) or not names:
            raise ValueError("fixed UI profile targets must be unique")

    def point(self, target: str, width: int, height: int) -> NormalizedPoint:
        if (width, height) != (self.expected_width, self.expected_height):
            raise ValueError(
                f"fixed UI profile {self.profile_id} requires "
                f"{self.expected_width}x{self.expected_height}; got {width}x{height}"
            )
        try:
            return dict(self.points)[target]
        except KeyError as exc:
            raise ValueError(f"fixed UI target {target} is not in the profile") from exc


# Coordinates are derived from the current live-proven click formulas applied to
# the retained 1280x720 real-screen fixtures. The Host Game point preserves its
# existing 40%-into-template offset; the other points use verified template
# centers.
DST_FIXED_1280X720 = FixedUiProfile(
    profile_id="dst-1280x720-linux-v1",
    expected_width=1280,
    expected_height=720,
    points=(
        ("HOST_GAME", NormalizedPoint(0.0953125, 0.5465277777777777)),
        ("FARM_01", NormalizedPoint(0.299609375, 0.27847222222222223)),
        ("RESUME_WORLD", NormalizedPoint(0.828515625, 0.9527777777777777)),
        ("MODS_DISABLED_CONTINUE", NormalizedPoint(0.4109375, 0.7944444444444444)),
        ("WILSON", NormalizedPoint(0.3265625, 0.2520833333333333)),
        ("START_SURVIVOR", NormalizedPoint(0.890234375, 0.9375)),
    ),
)

FIXED_UI_ACTION_TARGETS = {
    "CLICK_HOST_GAME": "HOST_GAME",
    "SELECT_EXISTING_WORLD": "FARM_01",
    "START_EXISTING_WORLD": "RESUME_WORLD",
    "CONFIRM_MODS_DISABLED": "MODS_DISABLED_CONTINUE",
    "SELECT_SURVIVOR": "WILSON",
    "START_SURVIVOR": "START_SURVIVOR",
}
