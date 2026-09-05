from __future__ import annotations

import multiprocessing as mp
import queue
import threading
from dataclasses import replace

from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.input import emergency_release_all
from runtime_agent.gameworker.noop import NoopGameWorker


def _worker_main(
    config: WorkerConfig, context: WorkerContext, commands, reports
) -> None:
    worker = DSTGameWorker(config) if config.plugin == "dst" else NoopGameWorker()
    current_context = context
    stop = threading.Event()
    game_ready = threading.Event()

    def publish(
        report: WorkerReport, command_id: int | None = None, result: str = "OK"
    ) -> None:
        payload = {
            "report": report.as_dict(),
            "command_id": command_id,
            "result": result,
        }
        try:
            reports.put_nowait(payload)
        except queue.Full:
            try:
                reports.get_nowait()
            except queue.Empty:
                pass
            try:
                reports.put_nowait(payload)
            except queue.Full:
                pass

    publish(worker.prepare(current_context))

    def control_loop() -> None:
        nonlocal current_context
        while not stop.is_set():
            try:
                message = commands.get(timeout=0.2)
            except queue.Empty:
                continue
            command = message.get("command")
            command_id = message.get("command_id")
            try:
                if command == "GAME_READY":
                    report = worker.on_game_ready(current_context)
                    if not config.autostart:
                        report = worker.pause()
                    game_ready.set()
                elif command == "RUNTIME_VERIFIED":
                    current_context = replace(
                        current_context, runtime_verified=bool(message.get("value"))
                    )
                    if hasattr(worker, "set_runtime_verified"):
                        worker.set_runtime_verified(current_context.runtime_verified)
                    report = worker.status()
                elif command == "PAUSE":
                    report = worker.pause()
                elif command == "RESUME":
                    report = worker.resume()
                elif command == "SET_MODE":
                    report = worker.set_mode(WorkerMode(message["mode"]))
                elif command == "STATUS":
                    report = worker.status()
                elif command == "STOP":
                    report = worker.shutdown()
                    publish(report, command_id)
                    stop.set()
                    return
                else:
                    publish(worker.status(), command_id, "UNKNOWN_COMMAND")
                    continue
                publish(report, command_id)
            except Exception:  # noqa: BLE001 - IPC boundary reports plugin failures
                publish(worker.status(), command_id, "FAILED")

    control = threading.Thread(target=control_loop, name="worker-ipc", daemon=True)
    control.start()
    try:
        while not stop.wait(config.tick_interval):
            publish(
                worker.tick(current_context) if game_ready.is_set() else worker.status()
            )
    finally:
        if not stop.is_set():
            publish(worker.shutdown())
        stop.set()
        control.join(timeout=1)


class WorkerProcessHost:
    """Isolates a GameWorker behind OS-local multiprocessing queues."""

    def __init__(
        self, config: WorkerConfig, context: WorkerContext, *, max_restarts: int = 3
    ):
        self.config = config
        self.context = context
        self.max_restarts = max_restarts
        self.restart_count = 0
        self._ctx = mp.get_context("spawn")
        self._commands = self._ctx.Queue(maxsize=32)
        self._reports = self._ctx.Queue(maxsize=8)
        self._process = None
        self._stopped = False
        self._game_ready = False
        self._paused = False
        self._last_report = WorkerReport(
            plugin="DSTGameWorker" if config.plugin == "dst" else "noop",
            version=config.plugin_version,
            config_version=config.profile_version,
            mode=config.mode,
            state="INITIALIZING",
            healthy=True,
        )
        self._acks: list[dict] = []

    def start(self) -> None:
        if self._process is not None and self._process.is_alive():
            return
        self._stopped = False
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(self.config, self.context, self._commands, self._reports),
            name=f"gameworker-{self.context.runtime_id}",
            daemon=True,
        )
        self._process.start()
        try:
            if self.context.runtime_verified:
                self._commands.put_nowait(
                    {"command": "RUNTIME_VERIFIED", "value": True}
                )
            if self._game_ready:
                self._commands.put_nowait({"command": "GAME_READY"})
            if self._paused:
                self._commands.put_nowait({"command": "PAUSE"})
        except queue.Full:
            self._last_report.state = "NEEDS_ATTENTION"
            self._last_report.healthy = False
            self._last_report.error_code = "WORKER_CRASHED"

    def _drain(self) -> None:
        while True:
            try:
                payload = self._reports.get_nowait()
            except queue.Empty:
                break
            self._last_report = WorkerReport(**payload["report"])
            self._last_report.restart_count = self.restart_count
            if payload.get("command_id") is not None:
                self._acks.append(
                    {"id": payload["command_id"], "result": payload.get("result", "OK")}
                )

    def tick(self) -> WorkerReport:
        self._drain()
        if (
            self._process is not None
            and not self._process.is_alive()
            and not self._stopped
        ):
            emergency_release_all(self.context.display, self.config.bindings)
            self._process.join(timeout=0.1)
            self._process = None
            if self.restart_count < self.max_restarts:
                self.restart_count += 1
                self.start()
            else:
                self._last_report.state = "NEEDS_ATTENTION"
                self._last_report.healthy = False
                self._last_report.error_code = "WORKER_CRASHED"
                self._last_report.restart_count = self.restart_count
        return self._last_report

    def command(self, command: str, *, command_id: int | None = None, **values) -> None:
        if command == "GAME_READY":
            self._game_ready = True
        elif command == "PAUSE":
            self._paused = True
        elif command == "RESUME":
            self._paused = False
        elif command == "SET_MODE":
            self.config = replace(self.config, mode=WorkerMode(values["mode"]))
        self.start()
        try:
            self._commands.put_nowait(
                {"command": command, "command_id": command_id, **values}
            )
        except queue.Full:
            if command_id is not None:
                self._acks.append({"id": command_id, "result": "IPC_QUEUE_FULL"})

    def request_stop(self, command_id: int | None = None) -> None:
        self._stopped = True
        self._paused = True
        try:
            self._commands.put_nowait({"command": "STOP", "command_id": command_id})
        except queue.Full:
            if command_id is not None:
                self._acks.append({"id": command_id, "result": "IPC_QUEUE_FULL"})

    def set_game_ready(self, ready: bool) -> None:
        self._game_ready = ready

    def acknowledgements(self) -> list[dict]:
        self._drain()
        values, self._acks = self._acks, []
        return values

    def shutdown(self) -> WorkerReport:
        self._stopped = True
        if self._process is not None and self._process.is_alive():
            self.request_stop()
            self._process.join(timeout=3)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1)
                emergency_release_all(self.context.display, self.config.bindings)
        self._drain()
        self._process = None
        return self._last_report
