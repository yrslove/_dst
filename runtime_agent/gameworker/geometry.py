from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


def _unit(value: float) -> float:
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("normalized coordinate must be finite and in 0.0..1.0")
    return value


class CoordinateSpace(StrEnum):
    NORMALIZED = "NORMALIZED"
    FRAME_PIXELS = "FRAME_PIXELS"
    VIEWPORT_PIXELS = "VIEWPORT_PIXELS"


@dataclass(frozen=True, slots=True)
class NormalizedPoint:
    x: float
    y: float

    def __post_init__(self) -> None:
        _unit(self.x)
        _unit(self.y)


@dataclass(frozen=True, slots=True)
class NormalizedRegion:
    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self) -> None:
        for value in (self.left, self.top, self.right, self.bottom):
            _unit(value)
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("normalized region must have positive area")


@dataclass(frozen=True, slots=True)
class PixelPoint:
    x: int
    y: int

    def __post_init__(self) -> None:
        if self.x < 0 or self.y < 0:
            raise ValueError("pixel coordinates must be non-negative")


@dataclass(frozen=True, slots=True)
class PixelRegion:
    left: int
    top: int
    right: int
    bottom: int

    def __post_init__(self) -> None:
        if self.left < 0 or self.top < 0:
            raise ValueError("pixel region origin must be non-negative")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("pixel region must have positive area")

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


@dataclass(frozen=True, slots=True)
class Viewport:
    width: int
    height: int
    left: int = 0
    top: int = 0

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("viewport dimensions must be positive")
        if self.left < 0 or self.top < 0:
            raise ValueError("viewport origin must be non-negative")

    def point(self, value: NormalizedPoint) -> tuple[int, int]:
        return (
            self.left + round(value.x * (self.width - 1)),
            self.top + round(value.y * (self.height - 1)),
        )

    def region(self, value: NormalizedRegion) -> tuple[int, int, int, int]:
        return (
            self.left + round(value.left * self.width),
            self.top + round(value.top * self.height),
            self.left + round(value.right * self.width),
            self.top + round(value.bottom * self.height),
        )

    def contains(self, frame_width: int, frame_height: int) -> bool:
        return (
            self.left + self.width <= frame_width
            and self.top + self.height <= frame_height
        )


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    profile_id: str
    version: int
    expected_width: int
    expected_height: int
    viewport_region: NormalizedRegion = field(
        default_factory=lambda: NormalizedRegion(0.0, 0.0, 1.0, 1.0)
    )
    ui_scale: float | None = None
    verified: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id or len(self.profile_id) > 128:
            raise ValueError("calibration profile ID is invalid")
        if self.version < 1 or self.expected_width <= 0 or self.expected_height <= 0:
            raise ValueError("calibration dimensions/version are invalid")
        if self.ui_scale is not None and (
            not math.isfinite(self.ui_scale) or self.ui_scale <= 0
        ):
            raise ValueError("calibration UI scale must be positive")

    def viewport(self, width: int, height: int) -> Viewport:
        if width <= 0 or height <= 0:
            raise ValueError("frame dimensions must be positive")
        left, top, right, bottom = Viewport(width, height).region(self.viewport_region)
        return Viewport(right - left, bottom - top, left, top)

    def matches(self, width: int, height: int) -> bool:
        return width == self.expected_width and height == self.expected_height
