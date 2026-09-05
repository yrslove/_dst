from __future__ import annotations

from abc import ABC, abstractmethod
from threading import Lock

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.geometry import NormalizedRegion, Viewport


class CaptureError(RuntimeError):
    code = "WORKER_CAPTURE_FAILED"


class ScreenCapture(ABC):
    @abstractmethod
    def capture(self): ...

    @abstractmethod
    def capture_region(self, region: NormalizedRegion): ...

    @abstractmethod
    def resolution(self) -> tuple[int, int]: ...

    def close(self) -> None:
        return None


class X11ScreenCapture(ScreenCapture):
    """Captures only the root window of the runtime's canonical X display."""

    def __init__(
        self, environment: DisplayEnvironment, *, max_width: int, max_height: int
    ):
        self.environment = environment
        self.max_width = max_width
        self.max_height = max_height
        self._last_resolution = (max_width, max_height)
        self._lock = Lock()

    def capture(self):
        try:
            from PIL import ImageGrab
        except ImportError as exc:
            raise CaptureError("Pillow runtime dependency is unavailable") from exc
        try:
            with self._lock:
                frame = ImageGrab.grab(xdisplay=self.environment.display)
        except Exception as exc:
            raise CaptureError(
                f"display {self.environment.display} capture failed"
            ) from exc
        self._last_resolution = frame.size
        frame.thumbnail((self.max_width, self.max_height))
        return frame.convert("RGB")

    def capture_region(self, region: NormalizedRegion):
        frame = self.capture()
        return frame.crop(Viewport(*frame.size).region(region))

    def resolution(self) -> tuple[int, int]:
        return self._last_resolution
