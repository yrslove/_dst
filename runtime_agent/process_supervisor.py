from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS
from app.subprocess_env import sanitized_subprocess_environment

Clock = Callable[[], float]
BeforeStart = Callable[[], None]


@dataclass(slots=True)
class ManagedProcess:
    name: str
    pid: int | None = None
    started_at: str | None = None
    exit_code: int | None = None
    restart_count: int = 0
    last_exit_at: str | None = None
    desired: bool = False
    exhausted: bool = False
    process_group: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


ProcessStatus = ManagedProcess  # compatibility with the prior agent contract


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    max_attempts: int = 3
    window_seconds: float = 300
    initial_backoff_seconds: float = 2
    max_backoff_seconds: float = 30
    reset_after_stable_seconds: float = 120


class ProcessSupervisor:
    def __init__(
        self,
        name: str,
        command: tuple[str, ...],
        *,
        max_restarts: int = 3,
        backoff_seconds: float = 2,
        restart_policy: RestartPolicy | None = None,
        clock: Clock = time.monotonic,
        popen=subprocess.Popen,
        environment: dict[str, str] | None = None,
        before_start: BeforeStart | None = None,
    ):
        if not command:
            raise ValueError("process command must not be empty")
        self.name = name
        self.command = command
        self.restart_policy = restart_policy or RestartPolicy(
            max_attempts=max_restarts,
            initial_backoff_seconds=backoff_seconds,
            max_backoff_seconds=max(backoff_seconds, 30),
        )
        self.max_restarts = self.restart_policy.max_attempts
        self.backoff_seconds = self.restart_policy.initial_backoff_seconds
        self.clock = clock
        self._popen = popen
        self.environment = dict(environment or {})
        self.before_start = before_start
        self._process: subprocess.Popen | None = None
        self._started_monotonic: float | None = None
        self._next_start_at = 0.0
        self.status = ManagedProcess(name=name)
        self._restart_times: list[float] = []

    @property
    def alive(self) -> bool:
        if self._process is None:
            return False
        return self._process.poll() is None or self._process_group_alive()

    def _process_group_alive(self) -> bool:
        if (
            os.name != "posix"
            or self.status.process_group is None
            or not isinstance(self._process, subprocess.Popen)
        ):
            return False
        try:
            os.killpg(self.status.process_group, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @property
    def running_for(self) -> float:
        if not self.alive or self._started_monotonic is None:
            return 0.0
        return max(0.0, self.clock() - self._started_monotonic)

    def request_start(self) -> None:
        if not self.status.desired:
            self._restart_times.clear()
            self._next_start_at = 0.0
            self.status.restart_count = 0
            self.status.exhausted = False
            self.status.started_at = None
        self.status.desired = True
        if (
            self._process is None
            and self.status.started_at is None
            and not self.status.exhausted
        ):
            self._spawn(is_restart=False)

    def _spawn(self, *, is_restart: bool) -> None:
        if is_restart:
            self.status.restart_count += 1
        try:
            if self.before_start:
                self.before_start()
            self._started_monotonic = self.clock()
            self.status.started_at = datetime.now(timezone.utc).isoformat()
            environment = sanitized_subprocess_environment()
            if self.environment:
                for name in GRAPHICAL_ENVIRONMENT_KEYS:
                    environment.pop(name, None)
            environment.update(self.environment)
            self._process = self._popen(
                list(self.command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                shell=False,
                env=environment,
            )
        except OSError:
            self._process = None
            self._started_monotonic = None
            self.status.started_at = None
            self.status.pid = None
            self.status.process_group = None
            self.status.exit_code = 127
            self.status.last_exit_at = datetime.now(timezone.utc).isoformat()
            self._register_failure()
            return
        self.status.pid = self._process.pid
        self.status.process_group = (
            self._process.pid
            if os.name == "posix" and isinstance(self._process, subprocess.Popen)
            else None
        )
        self.status.exit_code = None

    def _register_failure(self) -> None:
        now = self.clock()
        self._restart_times = [
            t
            for t in self._restart_times
            if now - t <= self.restart_policy.window_seconds
        ]
        self._restart_times.append(now)
        self.status.exhausted = (
            len(self._restart_times) > self.restart_policy.max_attempts
        )
        exponent = min(max(0, len(self._restart_times) - 1), 16)
        self._next_start_at = now + min(
            self.restart_policy.max_backoff_seconds,
            self.restart_policy.initial_backoff_seconds * (2**exponent),
        )

    def tick(self) -> ManagedProcess:
        if self._process is not None:
            exit_code = self._process.poll()
            if exit_code is None or self._process_group_alive():
                if exit_code is not None:
                    # A launcher may exit while descendants remain in the session.
                    # The process group is still the supervised workload and must
                    # not be duplicated by a restart.
                    self.status.exit_code = exit_code
                    self.status.pid = None
                if (
                    self._started_monotonic is not None
                    and self.clock() - self._started_monotonic
                    >= self.restart_policy.reset_after_stable_seconds
                ):
                    self._restart_times.clear()
                    self.status.restart_count = 0
                return self.status
            self.status.exit_code = exit_code
            self.status.pid = None
            self.status.last_exit_at = datetime.now(timezone.utc).isoformat()
            self._process = None
            self._started_monotonic = None
            self.status.process_group = None
            self._register_failure()
        if (
            self.status.desired
            and self._process is None
            and not self.status.exhausted
            and self.clock() >= self._next_start_at
        ):
            self._spawn(is_restart=True)
        return self.status

    def shutdown(self, timeout: float = 10, kill_timeout: float = 2) -> ManagedProcess:
        self.status.desired = False
        process = self._process
        if process is None:
            self.status.pid = None
            self.status.process_group = None
            self.status.started_at = None
            self._started_monotonic = None
            return self.status

        def send(sig: signal.Signals) -> None:
            try:
                if (
                    os.name == "posix"
                    and self.status.process_group is not None
                    and isinstance(process, subprocess.Popen)
                ):
                    os.killpg(self.status.process_group, sig)
                elif sig == signal.SIGTERM:
                    process.terminate()
                else:
                    process.kill()
            except ProcessLookupError:
                pass

        def wait_until_stopped(seconds: float) -> bool:
            deadline = time.monotonic() + max(0.0, seconds)
            while self.alive and time.monotonic() < deadline:
                time.sleep(0.02)
            process.poll()
            return not self.alive

        try:
            if self.alive:
                send(signal.SIGTERM)
                if not wait_until_stopped(timeout):
                    send(signal.SIGKILL)
                    if not wait_until_stopped(kill_timeout):
                        raise subprocess.TimeoutExpired(self.command, kill_timeout)
            else:
                process.poll()
        finally:
            self.status.exit_code = process.poll()
            if not self.alive:
                self.status.pid = None
                self.status.process_group = None
                self.status.started_at = None
                self._process = None
                self._started_monotonic = None
        return self.status
