from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS
from app.subprocess_env import sanitized_subprocess_environment

Clock = Callable[[], float]
BeforeStart = Callable[[], None]
logger = logging.getLogger("runtime_agent.process_supervisor")


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
        self._adopted_pid: int | None = None
        self._started_monotonic: float | None = None
        self._next_start_at = 0.0
        self.status = ManagedProcess(name=name)
        self._restart_times: list[float] = []
        self._outage_started_monotonic: float | None = None

    @property
    def alive(self) -> bool:
        if self._adopted_pid is not None:
            return self._adopted_alive() or self._adopted_group_alive(
                self._adopted_pid
            )
        if self._process is None:
            return False
        return self._process.poll() is None or self._process_group_alive()

    def _adopted_alive(self) -> bool:
        try:
            stat = Path(f"/proc/{self._adopted_pid}/stat").read_text(encoding="ascii")
            if stat.rsplit(") ", 1)[1].split()[0] != "Z":
                return True
        except OSError:
            return False
        try:
            os.waitpid(self._adopted_pid, os.WNOHANG)
        except (ChildProcessError, ProcessLookupError):
            pass
        return False

    @staticmethod
    def _adopted_group_alive(pid: int) -> bool:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def adopt(self, pid: int, start_ticks: int) -> None:
        """Reconnect to a verified launcher after an agent-only replacement."""
        if self._process is not None or self._adopted_pid is not None:
            raise RuntimeError(f"{self.name} already supervised")
        if not self.matches_adoption_identity(pid, start_ticks, self.command):
            raise RuntimeError(f"cannot safely adopt {self.name} pid {pid}")
        self._adopted_pid = pid
        self._started_monotonic = self.clock()
        self.status.pid = pid
        self.status.process_group = pid
        self.status.started_at = self.process_started_at(pid, start_ticks)
        self.status.desired = True

    @staticmethod
    def process_started_at(pid: int, start_ticks: int) -> str:
        """Return the original process start time rather than the adoption time."""
        try:
            uptime = float(Path("/proc/uptime").read_text().split()[0])
            ticks_per_second = os.sysconf("SC_CLK_TCK")
            elapsed = max(0.0, uptime - start_ticks / ticks_per_second)
            started_at = datetime.now(timezone.utc) - timedelta(seconds=elapsed)
        except (OSError, ValueError, IndexError):
            started_at = datetime.now(timezone.utc)
        return started_at.isoformat()

    @staticmethod
    def process_identity(pid: int) -> tuple[int, int, int] | None:
        """Return start ticks, process group, and session for a live Linux pid."""
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields = stat.rsplit(") ", 1)[1].split()
            if fields[0] == "Z":
                return None
            return int(fields[19]), int(fields[2]), int(fields[3])
        except (OSError, IndexError, ValueError):
            return None

    @staticmethod
    def matches_adoption_identity(
        pid: int, start_ticks: int, command: tuple[str, ...]
    ) -> bool:
        """Require pid reuse protection, exact argv, uid, and isolated session."""
        identity = ProcessSupervisor.process_identity(pid)
        if identity is None:
            return False
        actual_start, process_group, session = identity
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            uid = Path(f"/proc/{pid}").stat().st_uid
        except OSError:
            return False
        if ProcessSupervisor.process_identity(pid) != identity:
            return False
        return (
            pid > 1
            and start_ticks > 0
            and actual_start == start_ticks
            and process_group == pid
            and session == pid
            and [arg for arg in cmdline if arg] == [arg.encode() for arg in command]
            and uid == os.getuid()
        )

    def adoption_identity(self) -> tuple[int, int] | None:
        """Describe only the exact isolated launcher owned by this supervisor."""
        pid = self.status.pid
        if (
            pid is None
            or not self.status.desired
            or not self.alive
        ):
            return None
        identity = self.process_identity(pid)
        if identity is None or not self.matches_adoption_identity(
            pid, identity[0], self.command
        ):
            return None
        return pid, identity[0]

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
            logger.warning(
                "managed_process_restart_attempt name=%s restart_count=%s pid=%s",
                self.name,
                self.status.restart_count,
                self.status.pid,
            )
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
        if is_restart and self._outage_started_monotonic is not None:
            logger.warning(
                "managed_process_restart_started name=%s pid=%s restart_count=%s "
                "process_downtime_seconds=%.3f",
                self.name,
                self.status.pid,
                self.status.restart_count,
                max(0.0, self.clock() - self._outage_started_monotonic),
            )
            self._outage_started_monotonic = None

    def _register_failure(self) -> None:
        now = self.clock()
        self._restart_times = [
            t
            for t in self._restart_times
            if now - t <= self.restart_policy.window_seconds
        ]
        self._restart_times.append(now)
        if self._outage_started_monotonic is None:
            self._outage_started_monotonic = now
        self.status.exhausted = (
            len(self._restart_times) > self.restart_policy.max_attempts
        )
        exponent = min(max(0, len(self._restart_times) - 1), 16)
        self._next_start_at = now + min(
            self.restart_policy.max_backoff_seconds,
            self.restart_policy.initial_backoff_seconds * (2**exponent),
        )
        logger.error(
            "managed_process_crash name=%s exit_code=%s restart_count=%s "
            "restart_attempts_in_window=%s restart_exhausted=%s "
            "backoff_seconds=%.3f",
            self.name,
            self.status.exit_code,
            self.status.restart_count,
            len(self._restart_times),
            self.status.exhausted,
            max(0.0, self._next_start_at - now),
        )
        if self.status.exhausted:
            logger.critical(
                "managed_process_restart_exhausted name=%s restart_count=%s "
                "attempts_in_window=%s",
                self.name,
                self.status.restart_count,
                len(self._restart_times),
            )

    def tick(self) -> ManagedProcess:
        if self._adopted_pid is not None:
            if self._adopted_alive():
                return self.status
            if self._adopted_group_alive(self._adopted_pid):
                self.status.pid = None
                return self.status
            self.status.pid = None
            self.status.process_group = None
            self.status.last_exit_at = datetime.now(timezone.utc).isoformat()
            self._adopted_pid = None
            self._started_monotonic = None
            self._register_failure()
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
            and self._adopted_pid is None
            and not self.status.exhausted
            and self.clock() >= self._next_start_at
        ):
            self._spawn(is_restart=True)
        return self.status

    def shutdown(self, timeout: float = 10, kill_timeout: float = 2) -> ManagedProcess:
        self.status.desired = False
        if self._adopted_pid is not None:
            pid = self._adopted_pid
            for sig, seconds in ((signal.SIGTERM, timeout), (signal.SIGKILL, kill_timeout)):
                if not self.alive:
                    break
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                deadline = time.monotonic() + seconds
                while self.alive and time.monotonic() < deadline:
                    time.sleep(0.02)
            if self.alive:
                raise subprocess.TimeoutExpired(self.command, kill_timeout)
            self._adopted_pid = None
            self.status.pid = None
            self.status.process_group = None
            self.status.started_at = None
            self._started_monotonic = None
            return self.status
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
