from __future__ import annotations

from datetime import datetime, timezone

from runtime_agent.gameworker.base import WorkerReport
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode


class NoopGameWorker:
    plugin = "noop"
    version = "1.0.0"

    def __init__(self, config: WorkerConfig | None = None):
        self.config = config or WorkerConfig()
        self.context = None
        self._state = "WORKER_IDLE"

    def _report(self) -> WorkerReport:
        return WorkerReport(
            self.plugin,
            self.version,
            self.config.profile_version,
            WorkerMode.DISABLED,
            self._state,
            True,
            last_tick_at=datetime.now(timezone.utc).isoformat(),
            error_code="WORKER_DISABLED",
            details={"message": "No gameplay worker installed"},
        )

    def prepare(self, context):
        self.context = context
        return self._report()

    def on_game_ready(self, context):
        self.context = context
        return self._report()

    def tick(self, context):
        self.context = context
        return self._report()

    def on_game_lost(self):
        return self.pause()

    def pause(self):
        self._state = "PAUSED"
        return self._report()

    def resume(self):
        self._state = "WORKER_IDLE"
        return self._report()

    def set_runtime_verified(self, _verified: bool) -> None:
        return None

    def set_mode(self, mode: WorkerMode) -> WorkerReport:
        if mode != WorkerMode.DISABLED:
            raise ValueError("noop worker supports only DISABLED/NOOP mode")
        return self._report()

    def status(self, *_args):
        return self._report()

    def get_status(self, *_args):
        return self._report()

    def shutdown(self, *_args):
        self._state = "STOPPED"
        return self._report()
