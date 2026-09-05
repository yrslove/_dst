from __future__ import annotations

from datetime import datetime, timezone

from runtime_agent.gameworker.base import WorkerReport


class NoopGameWorker:
    plugin = "noop"
    version = "1.0.0"

    def __init__(self):
        self._state = "WORKER_IDLE"

    def _report(self) -> WorkerReport:
        return WorkerReport(
            self.plugin,
            self.version,
            1,
            "DISABLED",
            self._state,
            True,
            last_tick_at=datetime.now(timezone.utc).isoformat(),
            error_code="WORKER_DISABLED",
            details={"message": "No gameplay worker installed"},
        )

    def prepare(self, context):
        return self._report()

    def on_game_ready(self, context):
        return self._report()

    def tick(self, context):
        return self._report()

    def pause(self):
        self._state = "PAUSED"
        return self._report()

    def resume(self):
        self._state = "WORKER_IDLE"
        return self._report()

    def status(self, *_args):
        return self._report()

    def get_status(self, *_args):
        return self._report()

    def shutdown(self, *_args):
        self._state = "STOPPED"
        return self._report()
