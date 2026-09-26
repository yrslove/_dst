from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from runtime_agent.process_supervisor import ProcessSupervisor


class DSTState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    READY = "READY"
    CRASHED = "CRASHED"
    ERROR = "ERROR"


class DSTProcess:
    def __init__(
        self,
        supervisor: ProcessSupervisor,
        readiness_file: Path,
        *,
        readiness_timeout_seconds: float = 300,
    ):
        self.supervisor, self.readiness_file, self._state = (
            supervisor,
            readiness_file,
            DSTState.STOPPED,
        )
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._terminal_error = False
        previous_before_start = supervisor.before_start

        def reset_marker() -> None:
            if previous_before_start:
                previous_before_start()
            self.readiness_file.unlink(missing_ok=True)

        supervisor.before_start = reset_marker

    def prepare(self):
        return self._state

    def start(self):
        if self._terminal_error:
            return DSTState.ERROR
        self.supervisor.request_start()
        self._state = DSTState.STARTING
        return self._state

    def status(self):
        if self._terminal_error:
            return DSTState.ERROR
        if self.supervisor.status.exhausted:
            return DSTState.ERROR
        if self.supervisor.alive:
            if self._ready_marker() and self.supervisor.alive:
                return DSTState.READY
            if self.supervisor.running_for >= self.readiness_timeout_seconds:
                self.supervisor.shutdown(timeout=3)
                self._terminal_error = True
                self._state = DSTState.ERROR
                return self._state
            return DSTState.RUNNING
        return self._state

    def wait_ready(self):
        return self.status() == DSTState.READY

    def stop(self):
        self.supervisor.shutdown()
        self._state = DSTState.STOPPED
        return self._state

    def diagnostics(self):
        return self.supervisor.status.as_dict()

    def _ready_marker(self):
        if not self.supervisor.status.started_at:
            return False
        try:
            # before_start unlinks the marker for every generation; do not depend
            # on filesystem timestamp precision after that generation barrier.
            return self.readiness_file.is_file()
        except OSError:
            return False
