from __future__ import annotations

import ctypes
import logging
import math
import multiprocessing as mp
import os
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from threading import Lock
from typing import Protocol

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS, DisplayEnvironment
from app.subprocess_env import purge_sensitive_environment
from runtime_agent.gameworker.geometry import (
    CoordinateSpace,
    NormalizedRegion,
    Viewport,
)

logger = logging.getLogger("runtime_agent.gameworker.capture")
MAX_FRAME_BYTES = 3840 * 2160 * 3


class CaptureFailure(StrEnum):
    TIMEOUT = "TIMEOUT"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    INVALID_FRAME = "INVALID_FRAME"
    STALE_FRAME = "STALE_FRAME"
    STALE_GENERATION = "STALE_GENERATION"
    CLOSED = "CLOSED"


class CaptureError(RuntimeError):
    code = "WORKER_CAPTURE_FAILED"

    def __init__(
        self,
        message: str,
        *,
        failure: CaptureFailure = CaptureFailure.SOURCE_UNAVAILABLE,
    ):
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class Frame:
    frame_id: str
    sequence: int
    captured_at: str
    captured_monotonic: float
    runtime_id: int
    runtime_generation: int
    worker_generation: int
    width: int
    height: int
    pixels: bytes
    coordinate_space: CoordinateSpace = CoordinateSpace.FRAME_PIXELS
    source: str = "synthetic"
    metadata: tuple[tuple[str, bool | int | float | str | None], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.frame_id, str)
            or not self.frame_id
            or len(self.frame_id) > 128
            or self.sequence < 1
        ):
            raise ValueError("frame identity is invalid")
        if (
            not isinstance(self.captured_at, str)
            or not self.captured_at
            or len(self.captured_at) > 64
            or not math.isfinite(self.captured_monotonic)
            or self.captured_monotonic < 0
        ):
            raise ValueError("frame timestamps are invalid")
        if (
            self.runtime_id < 1
            or self.runtime_generation < 1
            or self.worker_generation < 1
        ):
            raise ValueError("frame generation identity is invalid")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("frame dimensions must be positive")
        expected = self.width * self.height * 3
        if expected > MAX_FRAME_BYTES or len(self.pixels) != expected:
            raise ValueError("RGB frame payload size is invalid or exceeds bound")
        if not isinstance(self.pixels, bytes):
            raise TypeError("frame pixels must be immutable bytes")
        if not isinstance(self.coordinate_space, CoordinateSpace):
            raise TypeError("frame coordinate space is invalid")
        if (
            not isinstance(self.source, str)
            or not self.source
            or len(self.source) > 128
        ):
            raise ValueError("frame source is invalid")
        if (
            not isinstance(self.metadata, tuple)
            or len(self.metadata) > 32
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or len(item[0]) > 64
                or not isinstance(item[1], (bool, int, float, str, type(None)))
                or (isinstance(item[1], str) and len(item[1]) > 256)
                or (isinstance(item[1], float) and not math.isfinite(item[1]))
                for item in self.metadata
            )
        ):
            raise ValueError("frame metadata must be a small immutable tuple")

    def age(self, now_monotonic: float | None = None) -> float:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return max(0.0, now - self.captured_monotonic)

    def is_fresh(self, max_age: float, now_monotonic: float | None = None) -> bool:
        return max_age > 0 and self.age(now_monotonic) <= max_age

    def image(self):
        from PIL import Image

        return Image.frombytes("RGB", (self.width, self.height), self.pixels)


class CaptureSource(Protocol):
    runtime_generation: int
    worker_generation: int

    def capture(self) -> Frame: ...
    def close(self) -> None: ...


class LatestFrameSlot:
    """Capacity-one handoff: publishing newer data drops the previous reference."""

    def __init__(self):
        self._lock = Lock()
        self._frame: Frame | None = None

    def publish(self, frame: Frame) -> None:
        with self._lock:
            if self._frame is None or frame.sequence > self._frame.sequence:
                self._frame = frame

    def take(self) -> Frame | None:
        with self._lock:
            frame, self._frame = self._frame, None
            return frame

    @property
    def retained(self) -> int:
        with self._lock:
            return int(self._frame is not None)


class FakeCaptureSource:
    """Bounded deterministic source for synthetic frames and failures."""

    def __init__(
        self,
        frames: list[Frame | BaseException],
        *,
        runtime_generation: int,
        worker_generation: int,
        repeat_last: bool = False,
    ):
        if len(frames) > 256:
            raise ValueError("fake capture fixture exceeds bound")
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self._items = deque(frames)
        self._last: Frame | None = None
        self._repeat_last = repeat_last
        self._closed = False
        self._lock = Lock()

    def capture(self) -> Frame:
        with self._lock:
            if self._closed:
                raise CaptureError(
                    "capture source is closed", failure=CaptureFailure.CLOSED
                )
            if self._items:
                item = self._items.popleft()
            elif self._repeat_last and self._last is not None:
                item = self._last
            else:
                raise CaptureError("synthetic stream exhausted")
            if isinstance(item, BaseException):
                raise item
            self._last = item
            return item

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._items.clear()
            self._last = None


def _set_parent_death_signal() -> None:
    if not sys.platform.startswith("linux"):
        return
    parent = os.getppid()
    try:
        libc = ctypes.CDLL(None)
        if libc.prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGKILL)
    except (AttributeError, OSError):
        return


def _grab_x11_frame(
    sender,
    environment: DisplayEnvironment,
    max_width: int,
    max_height: int,
) -> None:
    _set_parent_death_signal()
    purge_sensitive_environment()
    for name in GRAPHICAL_ENVIRONMENT_KEYS:
        os.environ.pop(name, None)
    os.environ.update(environment.as_environ())
    try:
        from PIL import ImageGrab

        image = ImageGrab.grab(xdisplay=environment.display)
        image.thumbnail((max_width, max_height))
        image = image.convert("RGB")
        captured_at = datetime.now(timezone.utc).isoformat()
        captured_monotonic = time.monotonic()
        sender.send(
            (
                True,
                image.width,
                image.height,
                image.tobytes(),
                captured_at,
                captured_monotonic,
                None,
            )
        )
    except Exception as exc:  # noqa: BLE001 - isolated backend error boundary
        sender.send((False, 0, 0, b"", "", 0.0, type(exc).__name__))
    finally:
        sender.close()


class X11ScreenCapture:
    """Killable, generation-owned Pillow/X11 CaptureSource."""

    def __init__(
        self,
        environment: DisplayEnvironment,
        *,
        max_width: int,
        max_height: int,
        runtime_id: int = 1,
        runtime_generation: int = 1,
        worker_generation: int = 1,
        timeout: float = 2.0,
        process_context=None,
    ):
        if max_width <= 0 or max_height <= 0 or timeout <= 0:
            raise ValueError("capture bounds and timeout must be positive")
        self.environment = environment
        self.max_width = max_width
        self.max_height = max_height
        self.runtime_id = runtime_id
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self.timeout = timeout
        self._ctx = process_context or mp.get_context("spawn")
        self._last_resolution = (max_width, max_height)
        self._sequence = 0
        self._closed = False
        self._active = None
        self._lock = Lock()
        self._capture_lock = Lock()

    @staticmethod
    def _stop_process(process) -> None:
        if process is None:
            return
        try:
            alive = process.is_alive()
        except (AssertionError, ValueError):
            return
        if not alive:
            process.join(timeout=0)
            return
        process.terminate()
        process.join(timeout=0.5)
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=0.5)

    def capture(self) -> Frame:
        with self._capture_lock:
            with self._lock:
                if self._closed:
                    raise CaptureError(
                        "capture source is closed", failure=CaptureFailure.CLOSED
                    )
                receiver, sender = self._ctx.Pipe(duplex=False)
                process = self._ctx.Process(
                    target=_grab_x11_frame,
                    args=(sender, self.environment, self.max_width, self.max_height),
                    name=f"x11-capture-{self.runtime_id}-g{self.worker_generation}",
                    daemon=True,
                )
                self._sequence += 1
                sequence = self._sequence
                try:
                    # Starting under this lock makes close() unable to miss a
                    # helper between construction and publication.
                    process.start()
                except Exception as exc:
                    receiver.close()
                    sender.close()
                    raise CaptureError("X11 capture helper failed to start") from exc
                self._active = process
            sender.close()
            try:
                if not receiver.poll(self.timeout):
                    raise CaptureError(
                        "X11 capture timed out", failure=CaptureFailure.TIMEOUT
                    )
                try:
                    (
                        ok,
                        width,
                        height,
                        pixels,
                        captured_at,
                        captured_monotonic,
                        error,
                    ) = receiver.recv()
                except EOFError as exc:
                    raise CaptureError("X11 capture helper exited") from exc
                if not ok:
                    raise CaptureError(f"X11 capture failed ({error})")
                try:
                    frame = Frame(
                        frame_id=(
                            f"r{self.runtime_generation}-w{self.worker_generation}"
                            f"-f{sequence}"
                        ),
                        sequence=sequence,
                        captured_at=captured_at,
                        captured_monotonic=captured_monotonic,
                        runtime_id=self.runtime_id,
                        runtime_generation=self.runtime_generation,
                        worker_generation=self.worker_generation,
                        width=width,
                        height=height,
                        pixels=pixels,
                        source="x11-pillow",
                    )
                except (TypeError, ValueError) as exc:
                    raise CaptureError(
                        "X11 capture returned an invalid frame",
                        failure=CaptureFailure.INVALID_FRAME,
                    ) from exc
                self._last_resolution = (width, height)
                return frame
            finally:
                receiver.close()
                self._stop_process(process)
                with self._lock:
                    if self._active is process:
                        self._active = None

    def capture_region(self, region: NormalizedRegion) -> Frame:
        source = self.capture()
        image = source.image()
        left, top, right, bottom = Viewport(source.width, source.height).region(region)
        cropped = image.crop((left, top, right, bottom))
        return Frame(
            frame_id=f"{source.frame_id}:region",
            sequence=source.sequence,
            captured_at=source.captured_at,
            captured_monotonic=source.captured_monotonic,
            runtime_id=source.runtime_id,
            runtime_generation=source.runtime_generation,
            worker_generation=source.worker_generation,
            width=cropped.width,
            height=cropped.height,
            pixels=cropped.tobytes(),
            source=source.source,
            metadata=(("parent_frame_id", source.frame_id),),
        )

    def resolution(self) -> tuple[int, int]:
        return self._last_resolution

    def close(self) -> None:
        with self._lock:
            self._closed = True
            process = self._active
        self._stop_process(process)
