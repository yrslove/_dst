from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from runtime_agent.process_supervisor import ProcessSupervisor


class SteamState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    READY = "READY"
    NEEDS_LOGIN = "NEEDS_LOGIN"
    CRASHED = "CRASHED"
    ERROR = "ERROR"


class SteamProcess:
    def __init__(
        self,
        supervisor: ProcessSupervisor,
        readiness_file: Path,
        *,
        needs_login_file: Path | None = None,
        readiness_timeout_seconds: float = 120,
        prerequisites_ready: Callable[[], bool] | None = None,
    ):
        self.supervisor, self.readiness_file, self._state = (
            supervisor,
            readiness_file,
            SteamState.STOPPED,
        )
        self.needs_login_file = needs_login_file
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._terminal_error = False
        self.prerequisites_ready = prerequisites_ready
        previous_before_start = supervisor.before_start

        def reset_markers() -> None:
            if previous_before_start:
                previous_before_start()
            for marker in (self.readiness_file, self.needs_login_file):
                if marker is not None:
                    marker.unlink(missing_ok=True)

        supervisor.before_start = reset_markers

    def prepare(self):
        return self._state

    def start(self):
        if self._terminal_error:
            return SteamState.ERROR
        if self.prerequisites_ready and not self.prerequisites_ready():
            return SteamState.STOPPED
        self.supervisor.request_start()
        self._state = SteamState.STARTING
        return self._state

    def status(self):
        if self._terminal_error:
            return SteamState.ERROR
        if self.supervisor.status.exhausted:
            return SteamState.ERROR
        if self.supervisor.alive:
            if self._fresh_marker(self.needs_login_file):
                return SteamState.NEEDS_LOGIN
            if self._ready_marker() and self.supervisor.alive:
                return SteamState.READY
            if self.supervisor.running_for >= self.readiness_timeout_seconds:
                self.supervisor.shutdown(timeout=3)
                self._terminal_error = True
                self._state = SteamState.ERROR
                return self._state
            return SteamState.RUNNING
        return self._state

    def wait_ready(self):
        return self.status() == SteamState.READY

    def stop(self):
        self.supervisor.shutdown()
        self._state = SteamState.STOPPED
        return self._state

    def diagnostics(self):
        return self.supervisor.status.as_dict()

    def _ready_marker(self):
        return self._fresh_marker(self.readiness_file)

    def _fresh_marker(self, marker: Path | None):
        if marker is None:
            return False
        if not self.supervisor.status.started_at:
            return False
        try:
            # before_start unlinks every marker synchronously for each spawn.
            # Existence is therefore generation-bound and avoids false negatives
            # on filesystems with coarse timestamp precision.
            return marker.is_file()
        except OSError:
            return False
