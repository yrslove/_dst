from __future__ import annotations

from datetime import datetime
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
    def __init__(self, supervisor: ProcessSupervisor, readiness_file: Path):
        self.supervisor, self.readiness_file, self._state = (
            supervisor,
            readiness_file,
            SteamState.STOPPED,
        )

    def prepare(self):
        return self._state

    def start(self):
        self.supervisor.request_start()
        self._state = SteamState.STARTING
        return self._state

    def status(self):
        if self.supervisor.status.exhausted:
            return SteamState.ERROR
        if self.supervisor.alive:
            return SteamState.READY if self._ready_marker() else SteamState.RUNNING
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
        started_at = self.supervisor.status.started_at
        if not started_at:
            return False
        try:
            return (
                self.readiness_file.is_file()
                and self.readiness_file.stat().st_mtime
                >= datetime.fromisoformat(started_at).timestamp()
            )
        except OSError:
            return False
