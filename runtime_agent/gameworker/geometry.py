from __future__ import annotations

from dataclasses import dataclass


def _unit(value: float) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError("normalized coordinate must be in 0.0..1.0")
    return value


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
class Viewport:
    width: int
    height: int

    def point(self, value: NormalizedPoint) -> tuple[int, int]:
        return round(value.x * (self.width - 1)), round(value.y * (self.height - 1))

    def region(self, value: NormalizedRegion) -> tuple[int, int, int, int]:
        return (
            round(value.left * self.width),
            round(value.top * self.height),
            round(value.right * self.width),
            round(value.bottom * self.height),
        )
