from __future__ import annotations

import os
import subprocess
import time
from collections import deque
from contextlib import contextmanager
from threading import Event, Lock, Thread

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport


class InputError(RuntimeError):
    code = "WORKER_INPUT_FAILED"


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
    def __init__(self, rate: float):
        self.rate = max(0.1, rate)
        self.capacity = max(1, int(rate))
        self._events: deque[float] = deque()
        self._lock = Lock()

    def allow(self) -> bool:
        now = time.monotonic()
        with self._lock:
            while self._events and now - self._events[0] >= 1.0:
                self._events.popleft()
            if len(self._events) >= self.capacity:
                return False
            self._events.append(now)
            return True


class InputController:
    def __init__(
        self,
        environment: DisplayEnvironment,
        *,
        lease: InputLease,
        max_actions_per_second: float,
        max_key_presses_per_second: float,
    ):
        self.environment = environment
        self.lease = lease
        self.action_limiter = RateLimiter(max_actions_per_second)
        self.key_limiter = RateLimiter(max_key_presses_per_second)
        self._keys: set[str] = set()
        self._buttons: set[int] = set()
        self._state_lock = Lock()

    @property
    def has_held_inputs(self) -> bool:
        with self._state_lock:
            return bool(self._keys or self._buttons)

    def _run(self, *args: str) -> None:
        environment = os.environ.copy()
        environment.update(self.environment.as_environ())
        try:
            result = subprocess.run(
                ["xdotool", *args],
                env=environment,
                timeout=2,
                capture_output=True,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InputError("xdotool input operation failed") from exc
        if result.returncode != 0:
            raise InputError("xdotool rejected input operation")

    def key_down(self, key: str) -> None:
        if not self.key_limiter.allow():
            raise InputError("key input rate limit exceeded")
        with self._state_lock:
            self._keys.add(key)
        self._run("keydown", "--clearmodifiers", key)

    def key_up(self, key: str) -> None:
        self._run("keyup", key)
        with self._state_lock:
            self._keys.discard(key)

    def key_press(self, key: str) -> None:
        if not self.key_limiter.allow():
            raise InputError("key input rate limit exceeded")
        self._run("key", "--clearmodifiers", key)

    def mouse_move(self, point: NormalizedPoint, viewport: Viewport) -> None:
        x, y = viewport.point(point)
        self._run("mousemove", str(x), str(y))

    def mouse_down(self, button: int = 1) -> None:
        with self._state_lock:
            self._buttons.add(button)
        self._run("mousedown", str(button))

    def mouse_up(self, button: int = 1) -> None:
        self._run("mouseup", str(button))
        with self._state_lock:
            self._buttons.discard(button)

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
        self._run("click", str(button))

    def release_all(self) -> None:
        with self._state_lock:
            keys, buttons = tuple(self._keys), tuple(self._buttons)
        for key in keys:
            try:
                self._run("keyup", key)
            except InputError:
                continue
            with self._state_lock:
                self._keys.discard(key)
        for button in buttons:
            try:
                self._run("mouseup", str(button))
            except InputError:
                continue
            with self._state_lock:
                self._buttons.discard(button)


class DeadmanSafety:
    def __init__(self, controller: InputController, timeout: float):
        self.controller = controller
        self.timeout = timeout
        self._last_touch = time.monotonic()
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
        self._last_touch = time.monotonic()
        self._tripped.clear()

    def _run(self) -> None:
        while not self._stop.wait(min(0.25, self.timeout / 4)):
            if (
                self.controller.has_held_inputs
                and time.monotonic() - self._last_touch >= self.timeout
            ):
                self.controller.release_all()
                self._tripped.set()

    def close(self) -> None:
        self._stop.set()
        self.controller.release_all()
        if self._thread.is_alive():
            self._thread.join(timeout=1)


def emergency_release_all(environment: DisplayEnvironment, bindings) -> None:
    """Best-effort parent-side release if the isolated worker process dies."""
    values = {
        bindings.move_up,
        bindings.move_down,
        bindings.move_left,
        bindings.move_right,
        bindings.interact,
        bindings.inventory,
        bindings.cancel,
    }
    process_environment = os.environ.copy()
    process_environment.update(environment.as_environ())
    command = ["xdotool"]
    for key in values:
        command.extend(("keyup", key))
    for button in range(1, 6):
        command.extend(("mouseup", str(button)))
    try:
        subprocess.run(
            command,
            env=process_environment,
            timeout=2,
            capture_output=True,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
