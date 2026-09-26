from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Protocol

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS, DisplayEnvironment
from app.subprocess_env import sanitized_subprocess_environment
from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport

logger = logging.getLogger("runtime_agent.gameworker.input")


class InputError(RuntimeError):
    code = "WORKER_INPUT_FAILED"


class InputDriver(Protocol):
    """The only boundary allowed to perform platform input operations."""

    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def key_press(self, key: str) -> None: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_down(self, button: int) -> None: ...
    def mouse_up(self, button: int) -> None: ...
    def click(self, button: int) -> None: ...


class XdotoolInputDriver:
    """Bounded, shell-free production adapter for X11 input."""

    def __init__(self, environment: DisplayEnvironment, *, timeout: float = 2.0):
        if timeout <= 0:
            raise ValueError("input subprocess timeout must be positive")
        self.environment = environment
        self.timeout = timeout

    def _run(self, *args: str) -> None:
        environment = sanitized_subprocess_environment()
        for name in GRAPHICAL_ENVIRONMENT_KEYS:
            environment.pop(name, None)
        environment.update(self.environment.as_environ())
        try:
            result = subprocess.run(
                ["xdotool", *args],
                env=environment,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InputError("xdotool input operation failed") from exc
        if result.returncode != 0:
            raise InputError("xdotool rejected input operation")

    def key_down(self, key: str) -> None:
        self._run("keydown", "--clearmodifiers", key)

    def key_up(self, key: str) -> None:
        self._run("keyup", key)

    def key_press(self, key: str) -> None:
        self._run("key", "--clearmodifiers", key)

    def mouse_move(self, x: int, y: int) -> None:
        self._run("mousemove", str(x), str(y))

    def mouse_down(self, button: int) -> None:
        self._run("mousedown", str(button))

    def mouse_up(self, button: int) -> None:
        self._run("mouseup", str(button))

    def click(self, button: int) -> None:
        self._run("click", str(button))


@dataclass(frozen=True, slots=True)
class InputEvent:
    sequence: int
    operation: str
    value: str | int | tuple[int, int]
    timestamp: float


class FakeInputDriver:
    """Deterministic input backend used to prove safety without X11."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        fail_operations: set[str] | None = None,
        before_operation: Callable[[str], None] | None = None,
    ):
        self._clock = clock
        self._fail_operations = set(fail_operations or ())
        self._before_operation = before_operation
        self._sequence = 0
        self._lock = Lock()
        self.events: list[InputEvent] = []
        self.pressed_keys: set[str] = set()
        self.pressed_mouse_buttons: set[int] = set()

    def fail_next(self, operation: str) -> None:
        with self._lock:
            self._fail_operations.add(operation)

    def _record(self, operation: str, value, mutation: Callable[[], None]) -> None:
        if self._before_operation is not None:
            self._before_operation(operation)
        with self._lock:
            if operation in self._fail_operations:
                self._fail_operations.remove(operation)
                raise InputError(f"forced fake input failure: {operation}")
            self._sequence += 1
            mutation()
            self.events.append(
                InputEvent(self._sequence, operation, value, self._clock())
            )

    def key_down(self, key: str) -> None:
        self._record("key_down", key, lambda: self.pressed_keys.add(key))

    def key_up(self, key: str) -> None:
        self._record("key_up", key, lambda: self.pressed_keys.discard(key))

    def key_press(self, key: str) -> None:
        self._record("key_press", key, lambda: None)

    def mouse_move(self, x: int, y: int) -> None:
        self._record("mouse_move", (x, y), lambda: None)

    def mouse_down(self, button: int) -> None:
        self._record(
            "mouse_down", button, lambda: self.pressed_mouse_buttons.add(button)
        )

    def mouse_up(self, button: int) -> None:
        self._record(
            "mouse_up", button, lambda: self.pressed_mouse_buttons.discard(button)
        )

    def click(self, button: int) -> None:
        self._record("click", button, lambda: None)


class InputLease:
    def __init__(self):
        self._lock = Lock()

    @contextmanager
    def acquire(self, timeout: float):
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            raise InputError("worker input lease unavailable")
        try:
            yield
        finally:
            self._lock.release()


class RateLimiter:
    def __init__(self, rate: float, *, clock: Callable[[], float] = time.monotonic):
        self.rate = max(0.1, rate)
        self.capacity = max(1, int(rate))
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = Lock()

    def allow(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._events and now - self._events[0] >= 1.0:
                self._events.popleft()
            if len(self._events) >= self.capacity:
                return False
            self._events.append(now)
            return True


class InputController:
    """Canonical logical input owner for one worker generation."""

    def __init__(
        self,
        environment: DisplayEnvironment | None = None,
        *,
        lease: InputLease,
        max_actions_per_second: float,
        max_key_presses_per_second: float,
        driver: InputDriver | None = None,
        subprocess_timeout: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if driver is None:
            if environment is None:
                raise ValueError("display environment is required for xdotool input")
            driver = XdotoolInputDriver(environment, timeout=subprocess_timeout)
        self.environment = environment
        self.driver = driver
        self.lease = lease
        self.action_limiter = RateLimiter(max_actions_per_second, clock=clock)
        self.key_limiter = RateLimiter(max_key_presses_per_second, clock=clock)
        self._keys: set[str] = set()
        self._buttons: set[int] = set()
        self._uncertain_keys: set[str] = set()
        self._uncertain_buttons: set[int] = set()
        self._state_lock = Lock()
        self._io_lock = Lock()
        self._revoked = False

    @property
    def has_held_inputs(self) -> bool:
        with self._state_lock:
            return bool(self._keys or self._buttons)

    @property
    def pressed_keys(self) -> frozenset[str]:
        with self._state_lock:
            return frozenset(self._keys)

    @property
    def pressed_mouse_buttons(self) -> frozenset[int]:
        with self._state_lock:
            return frozenset(self._buttons)

    @property
    def revoked(self) -> bool:
        with self._state_lock:
            return self._revoked

    @property
    def uncertain_inputs(self) -> bool:
        with self._state_lock:
            return bool(self._uncertain_keys or self._uncertain_buttons)

    def activate(self) -> None:
        with self._state_lock:
            self._revoked = False

    def revoke(self) -> None:
        with self._state_lock:
            self._revoked = True

    def _require_active(self) -> None:
        with self._state_lock:
            if self._revoked:
                raise InputError("input ownership is revoked")

    def key_down(self, key: str) -> None:
        if not self.key_limiter.allow():
            raise InputError("key input rate limit exceeded")
        with self._io_lock:
            self._require_active()
            with self._state_lock:
                self._keys.add(key)
            try:
                self.driver.key_down(key)
            except Exception:
                self.release_all(reason="key_down_failure", _io_locked=True)
                raise

    def key_up(self, key: str) -> None:
        with self._io_lock:
            try:
                self.driver.key_up(key)
            except Exception:
                with self._state_lock:
                    self._uncertain_keys.add(key)
                raise
            finally:
                with self._state_lock:
                    self._keys.discard(key)
            with self._state_lock:
                self._uncertain_keys.discard(key)

    def key_press(self, key: str) -> None:
        if not self.key_limiter.allow():
            raise InputError("key input rate limit exceeded")
        with self._io_lock:
            self._require_active()
            self.driver.key_press(key)

    def mouse_move(self, point: NormalizedPoint, viewport: Viewport) -> None:
        x, y = viewport.point(point)
        with self._io_lock:
            self._require_active()
            self.driver.mouse_move(x, y)

    def mouse_down(self, button: int = 1) -> None:
        with self._io_lock:
            self._require_active()
            with self._state_lock:
                self._buttons.add(button)
            try:
                self.driver.mouse_down(button)
            except Exception:
                self.release_all(reason="mouse_down_failure", _io_locked=True)
                raise

    def mouse_up(self, button: int = 1) -> None:
        with self._io_lock:
            try:
                self.driver.mouse_up(button)
            except Exception:
                with self._state_lock:
                    self._uncertain_buttons.add(button)
                raise
            finally:
                with self._state_lock:
                    self._buttons.discard(button)
            with self._state_lock:
                self._uncertain_buttons.discard(button)

    def click(
        self,
        point: NormalizedPoint | None = None,
        viewport: Viewport | None = None,
        button: int = 1,
    ) -> None:
        if not self.action_limiter.allow():
            raise InputError("action rate limit exceeded")
        if point is not None:
            if viewport is None:
                raise ValueError("viewport is required for normalized mouse input")
            self.mouse_move(point, viewport)
        with self._io_lock:
            self._require_active()
            self.driver.click(button)

    def release_all(self, *, reason: str = "release_all", _io_locked=False) -> bool:
        """Best-effort independent release; logical state is always made safe."""
        if not _io_locked:
            self._io_lock.acquire()
        try:
            with self._state_lock:
                keys = tuple(sorted(self._keys | self._uncertain_keys))
                buttons = tuple(sorted(self._buttons | self._uncertain_buttons))
                self._keys.clear()
                self._buttons.clear()
            failed = False
            for key in keys:
                try:
                    self.driver.key_up(key)
                except Exception:
                    failed = True
                    with self._state_lock:
                        self._uncertain_keys.add(key)
                    logger.exception("key release failed key=%s reason=%s", key, reason)
                else:
                    with self._state_lock:
                        self._uncertain_keys.discard(key)
            for button in buttons:
                try:
                    self.driver.mouse_up(button)
                except Exception:
                    failed = True
                    with self._state_lock:
                        self._uncertain_buttons.add(button)
                    logger.exception(
                        "mouse release failed button=%s reason=%s", button, reason
                    )
                else:
                    with self._state_lock:
                        self._uncertain_buttons.discard(button)
            return not failed
        finally:
            if not _io_locked:
                self._io_lock.release()


class DeadmanSafety:
    def __init__(
        self,
        controller: InputController,
        timeout: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.controller = controller
        self.timeout = timeout
        self._clock = clock
        self._last_touch = clock()
        self._touch_lock = Lock()
        self._stop = Event()
        self._tripped = Event()
        self._thread = Thread(
            target=self._run, name="worker-input-deadman", daemon=True
        )

    @property
    def tripped(self) -> bool:
        return self._tripped.is_set()

    def start(self) -> None:
        self._thread.start()

    def touch(self) -> None:
        with self._touch_lock:
            self._last_touch = self._clock()

    def reset(self) -> None:
        with self._touch_lock:
            self._last_touch = self._clock()
        self._tripped.clear()

    def _run(self) -> None:
        while not self._stop.wait(min(0.25, self.timeout / 4)):
            with self._touch_lock:
                elapsed = self._clock() - self._last_touch
            if self.controller.has_held_inputs and elapsed >= self.timeout:
                self.controller.revoke()
                self.controller.release_all(reason="deadman")
                self._tripped.set()
                logger.error("input deadman tripped")

    def close(self) -> None:
        self._stop.set()
        self.controller.revoke()
        self.controller.release_all(reason="deadman_close")
        if self._thread.is_alive():
            self._thread.join(timeout=1)


def emergency_release_all(environment: DisplayEnvironment, bindings) -> None:
    """Best-effort parent-side reset after isolated worker process death."""
    driver = XdotoolInputDriver(environment)
    keys = sorted(
        {
            bindings.move_up,
            bindings.move_down,
            bindings.move_left,
            bindings.move_right,
            bindings.interact,
            bindings.inventory,
            bindings.cancel,
        }
    )
    for key in keys:
        try:
            driver.key_up(key)
        except Exception:  # noqa: BLE001 - parent safety boundary must continue
            logger.warning("parent emergency key release failed key=%s", key)
    for button in range(1, 6):
        try:
            driver.mouse_up(button)
        except Exception:  # noqa: BLE001 - parent safety boundary must continue
            logger.warning("parent emergency mouse release failed button=%s", button)
