from __future__ import annotations

import logging
import multiprocessing as mp
import queue
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from enum import StrEnum
from multiprocessing.reduction import ForkingPickler

from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.input import emergency_release_all
from runtime_agent.gameworker.ipc import (
    WorkerCommandName,
    WorkerCommandResult,
    WorkerIPCAcknowledgement,
    WorkerIPCCommand,
    WorkerIPCReport,
)
from runtime_agent.gameworker.noop import NoopGameWorker

logger = logging.getLogger("runtime_agent.gameworker.process")


def _trace_command_pipe_writer(
    channel,
    *,
    runtime_id: int,
    generation: int,
    transport_state: dict[int, str],
    transport_lock: threading.Lock,
) -> None:
    """Trace the existing QueueFeederThread's actual command-pipe writes."""
    send_bytes = getattr(channel, "_send_bytes", None)
    if not callable(send_bytes):
        logger.debug(
            "worker_command_pipe_trace_unavailable runtime_id=%s generation=%s "
            "queue_type=%s",
            runtime_id,
            generation,
            type(channel).__name__,
        )
        return

    def traced_send_bytes(data) -> None:
        try:
            message = ForkingPickler.loads(data)
        except Exception:  # noqa: BLE001 - diagnostics must not affect delivery
            message = None
        if not isinstance(message, WorkerIPCCommand):
            send_bytes(data)
            return

        command_id = message.command_id
        started = time.monotonic()
        if command_id is not None:
            with transport_lock:
                if int(command_id) in transport_state:
                    transport_state[int(command_id)] = "pipe_write_in_progress"
        logger.info(
            "worker_command_pipe_write_begin runtime_id=%s command_id=%s "
            "command=%s producing_generation=%s expected_generation=%s "
            "payload_bytes=%s feeder_thread=%s",
            runtime_id,
            command_id,
            message.command,
            message.worker_generation,
            generation,
            len(data),
            threading.current_thread().name,
        )
        try:
            send_bytes(data)
        except Exception as exc:
            if command_id is not None:
                with transport_lock:
                    if int(command_id) in transport_state:
                        transport_state[int(command_id)] = "pipe_write_failed"
            logger.exception(
                "worker_command_pipe_write_failed runtime_id=%s command_id=%s "
                "command=%s producing_generation=%s expected_generation=%s "
                "payload_bytes=%s reason=%s",
                runtime_id,
                command_id,
                message.command,
                message.worker_generation,
                generation,
                len(data),
                type(exc).__name__,
            )
            raise
        if command_id is not None:
            with transport_lock:
                if int(command_id) in transport_state:
                    transport_state[int(command_id)] = "pipe_write_complete"
        logger.info(
            "worker_command_pipe_write_complete runtime_id=%s command_id=%s "
            "command=%s producing_generation=%s expected_generation=%s "
            "payload_bytes=%s elapsed_ms=%.3f",
            runtime_id,
            command_id,
            message.command,
            message.worker_generation,
            generation,
            len(data),
            (time.monotonic() - started) * 1000,
        )

    channel._send_bytes = traced_send_bytes


def _configure_child_command_logging() -> None:
    if mp.current_process().name == "MainProcess":
        return
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(levelname)s:%(name)s:%(message)s")
        )
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _worker_main(
    config: WorkerConfig,
    context: WorkerContext,
    worker_generation: int,
    commands,
    reports,
    acknowledgements,
) -> None:
    _configure_child_command_logging()
    worker = (
        DSTGameWorker(config, worker_generation=worker_generation)
        if config.plugin == "dst"
        else NoopGameWorker(config)
    )
    current_context = context
    stop = threading.Event()
    shutdown_complete = threading.Event()
    game_ready = threading.Event()
    worker_lock = threading.RLock()

    def publish_report(report: WorkerReport) -> None:
        try:
            reports.put_nowait(WorkerIPCReport(worker_generation, report.as_dict()))
        except (queue.Full, OSError, ValueError):
            # Periodic status is replaceable. Command outcomes have a dedicated
            # bounded channel and can therefore never be evicted by status traffic.
            return

    def publish_ack(report: WorkerReport, command_id: int | None, result: str) -> bool:
        if command_id is None:
            publish_report(report)
            return True
        payload = WorkerIPCAcknowledgement(
            worker_generation, int(command_id), result, report.as_dict()
        )
        try:
            acknowledgements.put(payload, timeout=0.2)
            logger.info(
                "worker_ack_child_enqueued runtime_id=%s command_id=%s "
                "producing_generation=%s result=%s timestamp_utc=%s",
                context.runtime_id,
                command_id,
                worker_generation,
                result,
                datetime.now(timezone.utc).isoformat(),
            )
            return True
        except (queue.Full, OSError, ValueError):
            # The parent will deterministically fail every still-pending command
            # when it observes this child exit. Continuing would silently lose ACKs.
            logger.error(
                "worker ACK channel full runtime_id=%s worker_generation=%s "
                "command_id=%s",
                context.runtime_id,
                worker_generation,
                command_id,
            )
            stop.set()
            return False

    def current_status() -> WorkerReport:
        try:
            return worker.status()
        except Exception:  # noqa: BLE001 - last-resort IPC boundary status
            return WorkerReport(
                plugin=config.plugin,
                version=config.plugin_version,
                config_version=config.profile_version,
                mode=config.mode,
                state="ERROR",
                healthy=False,
                error_code="WORKER_STATUS_FAILED",
            )

    try:
        with worker_lock:
            publish_report(worker.prepare(current_context))
    except Exception:
        logger.exception(
            "worker initialization failed runtime_id=%s worker_generation=%s",
            context.runtime_id,
            worker_generation,
        )
        try:
            worker.shutdown()
        except Exception:
            logger.exception(
                "worker initialization cleanup failed runtime_id=%s "
                "worker_generation=%s",
                context.runtime_id,
                worker_generation,
            )
        return

    def control_loop() -> None:
        nonlocal current_context
        while not stop.is_set():
            try:
                message: WorkerIPCCommand = commands.get(timeout=0.2)
            except queue.Empty:
                continue
            except (OSError, ValueError):
                stop.set()
                return
            if not isinstance(message, WorkerIPCCommand):
                logger.warning(
                    "invalid worker IPC envelope runtime_id=%s worker_generation=%s",
                    context.runtime_id,
                    worker_generation,
                )
                continue
            command = message.command
            command_id = message.command_id
            logger.info(
                "worker_command_child_received runtime_id=%s command_id=%s "
                "command=%s producing_generation=%s expected_generation=%s",
                context.runtime_id,
                command_id,
                command,
                message.worker_generation,
                worker_generation,
            )
            if message.worker_generation != worker_generation:
                if not publish_ack(
                    current_status(), command_id, WorkerCommandResult.STALE_GENERATION
                ):
                    return
                continue
            try:
                # The Event-based cancellation path stays concurrent so PAUSE/STOP
                # can interrupt a bounded action. State and resource mutation is
                # otherwise serialized with the tick loop.
                if command in {
                    WorkerCommandName.GAME_LOST,
                    WorkerCommandName.PAUSE,
                    WorkerCommandName.STOP,
                } or (
                    command == WorkerCommandName.RUNTIME_VERIFIED
                    and not message.values.get("value")
                ):
                    actions = getattr(worker, "actions", None)
                    if actions is not None:
                        actions.cancel()
                with worker_lock:
                    if command == WorkerCommandName.GAME_READY:
                        report = worker.on_game_ready(current_context)
                        if not config.autostart and getattr(worker, "mode", WorkerMode.DISABLED) != WorkerMode.ACTIVE:
                            report = worker.pause()
                        game_ready.set()
                    elif command == WorkerCommandName.GAME_LOST:
                        game_ready.clear()
                        report = worker.on_game_lost()
                    elif command == WorkerCommandName.RUNTIME_VERIFIED:
                        current_context = replace(
                            current_context,
                            runtime_verified=bool(message.values.get("value")),
                        )
                        if hasattr(worker, "set_runtime_verified"):
                            worker.set_runtime_verified(
                                current_context.runtime_verified
                            )
                        report = worker.status()
                    elif command == WorkerCommandName.PAUSE:
                        report = worker.pause()
                    elif command == WorkerCommandName.RESUME:
                        report = worker.resume()
                    elif command == WorkerCommandName.SET_MODE:
                        if message.values.get("locomotion_profile"):
                            worker.configure_experiment(
                                message.values["locomotion_profile"],
                                message.values["experiment_session_id"],
                                message.values["experiment_seconds"],
                                message.values.get("experiment_until_gift", False),
                                message.values.get("experiment_target_valid_seconds"),
                                message.values.get("experiment_continue_after_claim", False),
                            )
                        report = worker.set_mode(WorkerMode(message.values["mode"]))
                    elif command == WorkerCommandName.STATUS:
                        report = worker.status()
                    elif command == WorkerCommandName.STOP:
                        report = worker.shutdown()
                        shutdown_complete.set()
                        result = (
                            WorkerCommandResult.OK
                            if report.healthy
                            else WorkerCommandResult.FAILED
                        )
                        publish_ack(report, command_id, result)
                        stop.set()
                        return
                    else:
                        publish_ack(
                            worker.status(),
                            command_id,
                            WorkerCommandResult.UNKNOWN_COMMAND,
                        )
                        continue
                if not publish_ack(report, command_id, WorkerCommandResult.OK):
                    return
            except Exception:
                logger.exception(
                    "worker command failed runtime_id=%s worker_generation=%s "
                    "command_id=%s command=%s",
                    context.runtime_id,
                    worker_generation,
                    command_id,
                    command,
                )
                if not publish_ack(
                    current_status(), command_id, WorkerCommandResult.FAILED
                ):
                    return

    control = threading.Thread(target=control_loop, name="worker-ipc", daemon=True)
    control.start()
    try:
        tick_interval = config.tick_interval
        while not stop.wait(tick_interval):
            with worker_lock:
                report = (worker.tick(current_context)
                          if game_ready.is_set() else worker.status())
                report.telemetry["worker_loop_monotonic"] = time.monotonic()
                publish_report(report)
                recommended_interval = getattr(
                    worker, "next_tick_interval", config.tick_interval
                )
                tick_interval = min(
                    config.tick_interval,
                    max(0.01, float(recommended_interval)),
                )
    finally:
        if not shutdown_complete.is_set():
            with worker_lock:
                try:
                    publish_report(worker.shutdown())
                finally:
                    shutdown_complete.set()
        stop.set()
        control.join(timeout=1)
        # The parent joins this process before draining status. A large queued
        # telemetry frame can otherwise keep the Queue feeder alive forever and
        # turn a clean STOP into a forced stop/full runtime restart. Status is
        # replaceable; the independent command ACK queue must still flush.
        cancel_status_join = getattr(reports, "cancel_join_thread", None)
        if cancel_status_join is not None:
            cancel_status_join()


class WorkerProcessState(StrEnum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    READY = "READY"
    RUNNING = "RUNNING"
    BACKOFF = "BACKOFF"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class WorkerProcessHost:
    """Isolates a GameWorker behind OS-local multiprocessing queues."""

    def __init__(
        self,
        config: WorkerConfig,
        context: WorkerContext,
        *,
        max_restarts: int = 3,
        restart_backoff_seconds: float = 0.0,
        ready_timeout: float = 10.0,
        stop_timeout: float = 10.0,
        stable_run_seconds: float = 30.0,
        clock=time.monotonic,
    ):
        config.validate()
        self.config = config
        self.context = context
        if max_restarts < 0:
            raise ValueError("worker max_restarts must not be negative")
        if restart_backoff_seconds < 0:
            raise ValueError("worker restart backoff must not be negative")
        if min(ready_timeout, stop_timeout, stable_run_seconds) <= 0:
            raise ValueError("worker lifecycle timeouts must be positive")
        self.max_restarts = max_restarts
        self.restart_backoff_seconds = restart_backoff_seconds
        self.ready_timeout = ready_timeout
        self.stop_timeout = stop_timeout
        self.stable_run_seconds = stable_run_seconds
        self.restart_count = 0
        self._clock = clock
        self._ctx = mp.get_context("spawn")
        self._commands = None
        self._reports = None
        self._ack_reports = None
        self._process = None
        self._stopped = False
        self._restart_exhausted = False
        self._game_ready = False
        self._paused = False
        self._worker_generation = 0
        self._started_at: float | None = None
        self._ready_at: float | None = None
        self._last_loop_report_at: float | None = None
        self._next_restart_at: float | None = None
        self._state = WorkerProcessState.CREATED
        self._lock = threading.RLock()
        self._stale_messages = 0
        self._last_report = WorkerReport(
            plugin="DSTGameWorker" if config.plugin == "dst" else "noop",
            version=config.plugin_version,
            config_version=config.profile_version,
            mode=(
                WorkerMode.DISABLED if config.mode == WorkerMode.ACTIVE else config.mode
            ),
            state="INITIALIZING",
            healthy=True,
        )
        self._acks: list[dict] = []
        self._pending_command_ids: set[int] = set()
        self._command_transport_state: dict[int, str] = {}
        self._command_transport_lock = threading.Lock()

    @property
    def worker_generation(self) -> int:
        with self._lock:
            return self._worker_generation

    @property
    def process_state(self) -> WorkerProcessState:
        with self._lock:
            return self._state

    def update_context(self, context: WorkerContext) -> None:
        """Update mutable runtime facts without crossing runtime generations."""
        with self._lock:
            if (
                context.account_id != self.context.account_id
                or context.runtime_id != self.context.runtime_id
                or context.runtime_generation != self.context.runtime_generation
                or context.display != self.context.display
            ):
                raise ValueError("worker context identity cannot change")
            self.context = context

    def _replace_queues(self) -> None:
        self._close_queues()
        self._commands = self._ctx.Queue(maxsize=32)
        # Status is replaceable; terminal ACKs have their own bounded channel.
        self._reports = self._ctx.Queue(maxsize=64)
        self._ack_reports = self._ctx.Queue(maxsize=64)

    def _close_queues(self) -> None:
        for channel in (self._commands, self._reports, self._ack_reports):
            if channel is None:
                continue
            try:
                channel.cancel_join_thread()
                channel.close()
            except (OSError, ValueError):
                pass
        self._commands = None
        self._reports = None
        self._ack_reports = None

    def _fail_pending_commands(self, result: str) -> None:
        for command_id in sorted(self._pending_command_ids):
            self._acks.append({"id": command_id, "result": result})
            with self._command_transport_lock:
                self._command_transport_state.pop(command_id, None)
        self._pending_command_ids.clear()

    def _mark_crashed(self) -> None:
        self._last_report.state = "NEEDS_ATTENTION"
        self._last_report.healthy = False
        self._last_report.error_code = "WORKER_CRASHED"
        self._last_report.restart_count = self.restart_count
        self._state = WorkerProcessState.FAILED

    def _enqueue(
        self,
        command: str,
        *,
        command_id: int | None = None,
        report_failure: bool = True,
        **values,
    ) -> bool:
        if self._commands is None:
            if command_id is not None:
                logger.warning(
                    "worker_command_host_not_written runtime_id=%s command_id=%s "
                    "command=%s generation=%s reason=command_queue_unavailable "
                    "pending=%s",
                    self.context.runtime_id,
                    command_id,
                    command,
                    self._worker_generation,
                    sorted(self._pending_command_ids),
                )
            return False
        if command_id is not None and command_id in self._pending_command_ids:
            logger.warning(
                "worker_command_host_not_written runtime_id=%s command_id=%s "
                "command=%s generation=%s reason=command_id_already_pending "
                "pending=%s",
                self.context.runtime_id,
                command_id,
                command,
                self._worker_generation,
                sorted(self._pending_command_ids),
            )
            return False
        payload = WorkerIPCCommand(
            worker_generation=self._worker_generation,
            command=command,
            command_id=command_id,
            values=values,
        )
        pending_before = sorted(self._pending_command_ids)
        if command_id is not None:
            with self._command_transport_lock:
                self._command_transport_state[int(command_id)] = "parent_queue_put"
            logger.info(
                "worker_command_host_queue_put_begin runtime_id=%s command_id=%s "
                "command=%s generation=%s pending_before=%s",
                self.context.runtime_id,
                command_id,
                command,
                self._worker_generation,
                pending_before,
            )
        try:
            self._commands.put_nowait(payload)
            if command_id is not None:
                self._pending_command_ids.add(int(command_id))
                logger.info(
                    "worker_command_host_queued runtime_id=%s command_id=%s "
                    "command=%s generation=%s queue_stage=parent_local_buffer "
                    "pending_after=%s",
                    self.context.runtime_id,
                    command_id,
                    command,
                    self._worker_generation,
                    sorted(self._pending_command_ids),
                )
            return True
        except (queue.Full, OSError, ValueError) as exc:
            if command_id is not None:
                with self._command_transport_lock:
                    self._command_transport_state[int(command_id)] = "queue_put_failed"
            if command_id is not None and report_failure:
                self._acks.append(
                    {
                        "id": int(command_id),
                        "result": WorkerCommandResult.IPC_QUEUE_FULL,
                    }
                )
            if command_id is not None:
                logger.warning(
                    "worker_command_host_not_written runtime_id=%s command_id=%s "
                    "command=%s generation=%s reason=%s pending=%s",
                    self.context.runtime_id,
                    command_id,
                    command,
                    self._worker_generation,
                    type(exc).__name__,
                    sorted(self._pending_command_ids),
                )
            return False

    def _force_stop_child(self) -> bool:
        if self._process is None or not self._process.is_alive():
            return True
        try:
            self._process.terminate()
        except (OSError, ValueError):
            logger.exception(
                "worker terminate failed runtime_id=%s worker_generation=%s",
                self.context.runtime_id,
                self._worker_generation,
            )
        try:
            self._process.join(timeout=self.stop_timeout)
        except (OSError, ValueError):
            logger.exception(
                "worker join after terminate failed runtime_id=%s worker_generation=%s",
                self.context.runtime_id,
                self._worker_generation,
            )
        if self._process.is_alive() and hasattr(self._process, "kill"):
            try:
                self._process.kill()
            except (OSError, ValueError):
                logger.exception(
                    "worker kill failed runtime_id=%s worker_generation=%s",
                    self.context.runtime_id,
                    self._worker_generation,
                )
            try:
                self._process.join(timeout=min(1.0, self.stop_timeout))
            except (OSError, ValueError):
                logger.exception(
                    "worker join after kill failed runtime_id=%s worker_generation=%s",
                    self.context.runtime_id,
                    self._worker_generation,
                )
        return not self._process.is_alive()

    def _schedule_restart(self) -> None:
        if self._stopped:
            self._state = WorkerProcessState.STOPPED
            return
        if self.restart_count >= self.max_restarts:
            self._restart_exhausted = True
            self._mark_crashed()
            return
        self.restart_count += 1
        delay = self.restart_backoff_seconds * (2 ** (self.restart_count - 1))
        self._next_restart_at = self._clock() + delay
        self._state = WorkerProcessState.BACKOFF

    def _sync_parent_state(self) -> None:
        if self.context.runtime_verified:
            self._enqueue(WorkerCommandName.RUNTIME_VERIFIED, value=True)
        if self._game_ready:
            self._enqueue(WorkerCommandName.GAME_READY)
        if self._paused:
            self._enqueue(WorkerCommandName.PAUSE)

    def _spawn(self) -> bool:
        self._replace_queues()
        self._worker_generation += 1
        self._started_at = self._clock()
        self._ready_at = None
        self._last_loop_report_at = None
        self._next_restart_at = None
        self._state = WorkerProcessState.STARTING
        assert self._commands is not None
        assert self._reports is not None
        assert self._ack_reports is not None
        generation = self._worker_generation
        assert self._commands is not None
        _trace_command_pipe_writer(
            self._commands,
            runtime_id=self.context.runtime_id,
            generation=generation,
            transport_state=self._command_transport_state,
            transport_lock=self._command_transport_lock,
        )
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(
                self.config,
                self.context,
                generation,
                self._commands,
                self._reports,
                self._ack_reports,
            ),
            name=f"gameworker-{self.context.runtime_id}-g{generation}",
            # Capture uses a killable helper process so Pillow/X11 cannot wedge the
            # GameWorker. The helper installs Linux parent-death protection.
            daemon=False,
        )
        try:
            self._process.start()
        except Exception:
            logger.exception(
                "failed to start isolated GameWorker runtime_id=%s "
                "worker_generation=%s",
                self.context.runtime_id,
                generation,
            )
            self._process = None
            self._close_queues()
            self._schedule_restart()
            return False
        self._sync_parent_state()
        return True

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> bool:
        if self._stopped:
            return False
        if self._process is not None and self._process.is_alive():
            return True
        if self._restart_exhausted:
            self._mark_crashed()
            return False
        if self._process is not None:
            self._handle_dead_process()
            if self._process is not None and self._process.is_alive():
                return True
            if self._stopped or self._restart_exhausted:
                return False
        if self._next_restart_at is not None and self._clock() < self._next_restart_at:
            self._state = WorkerProcessState.BACKOFF
            return False
        return self._spawn()

    def _drain(self) -> None:
        # A multiprocessing Queue can block in recv_bytes on a partial frame after
        # its producer exits because the parent still owns the writer endpoint.
        # Treat unacknowledged IPC as lost once the worker process has exited.
        if self._process is not None and not self._process.is_alive():
            logger.warning(
                "worker_ipc_drain_skipped runtime_id=%s generation=%s "
                "reason=worker_exited",
                self.context.runtime_id,
                self._worker_generation,
            )
            return
        pending_at_start = sorted(self._pending_command_ids)
        with self._command_transport_lock:
            transport_at_start = sorted(self._command_transport_state.items())
        trace_drain = bool(pending_at_start)
        report_count = 0
        ack_count = 0
        if trace_drain:
            logger.info(
                "worker_ipc_report_drain_begin runtime_id=%s generation=%s "
                "pending=%s transport_state=%s",
                self.context.runtime_id,
                self._worker_generation,
                pending_at_start,
                transport_at_start,
            )
        if self._reports is not None:
            while True:
                try:
                    payload = self._reports.get_nowait()
                except (queue.Empty, OSError, ValueError):
                    break
                report_count += 1
                if not isinstance(payload, WorkerIPCReport):
                    self._stale_messages += 1
                    if self._stale_messages == 1 or self._stale_messages % 64 == 0:
                        logger.warning(
                            "worker_ipc_discard runtime_id=%s message_type=report "
                            "reason=invalid_type received_type=%s expected_generation=%s "
                            "dropped_count=%s",
                            self.context.runtime_id,
                            type(payload).__name__,
                            self._worker_generation,
                            self._stale_messages,
                        )
                    continue
                if payload.worker_generation != self._worker_generation:
                    self._stale_messages += 1
                    if self._stale_messages == 1 or self._stale_messages % 64 == 0:
                        logger.warning(
                            "worker_ipc_discard runtime_id=%s message_type=report "
                            "reason=stale_generation received_generation=%s "
                            "expected_generation=%s dropped_count=%s",
                            self.context.runtime_id,
                            payload.worker_generation,
                            self._worker_generation,
                            self._stale_messages,
                        )
                    continue
                self._last_report = WorkerReport(**payload.report)
                self._last_report.restart_count = self.restart_count
                self._last_loop_report_at = payload.report.get("telemetry", {}).get(
                    "worker_loop_monotonic", self._clock()
                )
                if self._ready_at is None:
                    self._ready_at = self._clock()
                    if self._state == WorkerProcessState.STARTING:
                        self._state = WorkerProcessState.READY
                elif self._state in {
                    WorkerProcessState.READY,
                    WorkerProcessState.RUNNING,
                }:
                    self._state = WorkerProcessState.RUNNING
        if trace_drain:
            logger.info(
                "worker_ipc_report_drain_end runtime_id=%s generation=%s "
                "messages_drained=%s pending=%s",
                self.context.runtime_id,
                self._worker_generation,
                report_count,
                sorted(self._pending_command_ids),
            )
            logger.info(
                "worker_ipc_ack_drain_begin runtime_id=%s generation=%s "
                "pending=%s",
                self.context.runtime_id,
                self._worker_generation,
                sorted(self._pending_command_ids),
            )
        if self._ack_reports is not None:
            while True:
                try:
                    payload = self._ack_reports.get_nowait()
                except (queue.Empty, OSError, ValueError):
                    break
                ack_count += 1
                if not isinstance(payload, WorkerIPCAcknowledgement):
                    self._stale_messages += 1
                    logger.warning(
                        "worker_ipc_discard runtime_id=%s message_type=ack "
                        "reason=invalid_type received_type=%s expected_generation=%s "
                        "dropped_count=%s",
                        self.context.runtime_id,
                        type(payload).__name__,
                        self._worker_generation,
                        self._stale_messages,
                    )
                    continue
                pending_before = sorted(self._pending_command_ids)
                logger.info(
                    "worker_ack_host_received runtime_id=%s command_id=%s "
                    "producing_generation=%s expected_generation=%s result=%s "
                    "pending_before=%s",
                    self.context.runtime_id,
                    payload.command_id,
                    payload.worker_generation,
                    self._worker_generation,
                    payload.result,
                    pending_before,
                )
                if payload.worker_generation != self._worker_generation:
                    self._stale_messages += 1
                    logger.warning(
                        "worker_ipc_discard runtime_id=%s message_type=ack "
                        "command_id=%s reason=stale_generation received_generation=%s "
                        "expected_generation=%s dropped_count=%s pending_after=%s",
                        self.context.runtime_id,
                        payload.command_id,
                        payload.worker_generation,
                        self._worker_generation,
                        self._stale_messages,
                        pending_before,
                    )
                    continue
                self._last_report = WorkerReport(**payload.report)
                self._last_report.restart_count = self.restart_count
                command_id = int(payload.command_id)
                if command_id not in self._pending_command_ids:
                    self._stale_messages += 1
                    logger.warning(
                        "worker_ipc_discard runtime_id=%s message_type=ack "
                        "command_id=%s reason=no_pending_command "
                        "received_generation=%s expected_generation=%s "
                        "dropped_count=%s pending_after=%s",
                        self.context.runtime_id,
                        command_id,
                        payload.worker_generation,
                        self._worker_generation,
                        self._stale_messages,
                        pending_before,
                    )
                    continue
                self._pending_command_ids.discard(command_id)
                with self._command_transport_lock:
                    self._command_transport_state.pop(command_id, None)
                self._acks.append({"id": command_id, "result": payload.result})
                logger.info(
                    "worker_ack_host_accepted runtime_id=%s command_id=%s "
                    "generation=%s result=%s pending_before=%s pending_after=%s",
                    self.context.runtime_id,
                    command_id,
                    self._worker_generation,
                    payload.result,
                    pending_before,
                    sorted(self._pending_command_ids),
                )
                if self._ready_at is None:
                    self._ready_at = self._clock()
                    if self._state == WorkerProcessState.STARTING:
                        self._state = WorkerProcessState.READY
                elif self._state in {
                    WorkerProcessState.READY,
                    WorkerProcessState.RUNNING,
                }:
                    self._state = WorkerProcessState.RUNNING
        if trace_drain:
            with self._command_transport_lock:
                transport_at_end = sorted(self._command_transport_state.items())
            logger.info(
                "worker_ipc_ack_drain_end runtime_id=%s generation=%s "
                "messages_drained=%s pending_before=%s pending_after=%s "
                "transport_state=%s",
                self.context.runtime_id,
                self._worker_generation,
                ack_count,
                pending_at_start,
                sorted(self._pending_command_ids),
                transport_at_end,
            )

    def _may_have_live_input(self) -> bool:
        return self.config.plugin == "dst" and (
            self.config.mode == WorkerMode.ACTIVE
            or self._last_report.mode == WorkerMode.ACTIVE.value
        )

    def _handle_dead_process(self) -> None:
        assert self._process is not None and not self._process.is_alive()
        self._process.join(timeout=min(0.1, self.stop_timeout))
        self._drain()
        if self._stopped:
            self._fail_pending_commands(WorkerCommandResult.WORKER_STOPPED)
            self._process = None
            self._close_queues()
            if self._last_report.state != "NEEDS_ATTENTION":
                self._last_report.state = "STOPPED"
                self._last_report.healthy = True
            self._state = WorkerProcessState.STOPPED
            return
        if self._may_have_live_input():
            emergency_release_all(self.context.display, self.config.bindings)
        self._fail_pending_commands(WorkerCommandResult.WORKER_CRASHED)
        self._last_report.healthy = False
        self._last_report.error_code = "WORKER_CRASHED"
        self._process = None
        self._close_queues()
        self._schedule_restart()
        if (
            self._state == WorkerProcessState.BACKOFF
            and self._next_restart_at is not None
            and self._clock() >= self._next_restart_at
        ):
            self._start_locked()

    def tick(self) -> WorkerReport:
        with self._lock:
            self._drain()
            if self._process is not None and not self._process.is_alive():
                self._handle_dead_process()
            elif (
                self._process is not None
                and self._ready_at is None
                and self._started_at is not None
                and self._clock() - self._started_at >= self.ready_timeout
            ):
                logger.error(
                    "worker readiness timed out runtime_id=%s worker_generation=%s",
                    self.context.runtime_id,
                    self._worker_generation,
                )
                self._force_stop_child()
                if self._process is not None and not self._process.is_alive():
                    self._handle_dead_process()
                else:
                    self._mark_crashed()
            elif (
                self._process is None
                and self._state == WorkerProcessState.BACKOFF
                and self._next_restart_at is not None
                and self._clock() >= self._next_restart_at
            ):
                self._start_locked()
            if (
                self.restart_count
                and self._ready_at is not None
                and self._clock() - self._ready_at >= self.stable_run_seconds
            ):
                self.restart_count = 0
                self._last_report.restart_count = 0
            if (
                self._state == WorkerProcessState.READY
                and self._process is not None
                and self._process.is_alive()
            ):
                self._state = WorkerProcessState.RUNNING
            self._last_report.details = {
                **self._last_report.details,
                "process_state": self._state,
                "worker_generation": self._worker_generation,
                "runtime_generation": self.context.runtime_generation,
                "stale_ipc_messages": self._stale_messages,
            }
            if (self._process is not None and self._process.is_alive()
                    and self._last_loop_report_at is not None
                    and not self._stopped):
                age = max(0.0, self._clock() - self._last_loop_report_at)
                self._last_report.details["worker_report_age_seconds"] = age
                self._last_report.details["worker_report_timeout_seconds"] = 30.0
                if age > 30.0:
                    self._last_report.healthy = False
                    self._last_report.error_code = "WORKER_HEARTBEAT_STALE"
            return self._last_report

    def command(self, command: str, *, command_id: int | None = None, **values) -> None:
        with self._lock:
            normalized = str(command).upper()
            if command_id is not None and (
                not isinstance(command_id, int)
                or isinstance(command_id, bool)
                or command_id < 1
            ):
                raise ValueError("worker command_id must be a positive integer")
            if any(
                not isinstance(key, str)
                or not isinstance(value, (bool, int, float, str, type(None)))
                for key, value in values.items()
            ):
                if command_id is not None:
                    self._acks.append(
                        {
                            "id": command_id,
                            "result": WorkerCommandResult.INVALID_COMMAND,
                        }
                    )
                return
            if (
                normalized == WorkerCommandName.RESUME
                and self._state == WorkerProcessState.STOPPING
                and self._process is not None
                and self._process.is_alive()
            ):
                if command_id is not None:
                    self._acks.append(
                        {
                            "id": command_id,
                            "result": WorkerCommandResult.WORKER_STOPPING,
                        }
                    )
                return
            if (
                normalized == WorkerCommandName.RESUME
                and self._process is not None
                and not self._process.is_alive()
            ):
                self._handle_dead_process()
            if normalized == WorkerCommandName.SET_MODE:
                try:
                    mode = WorkerMode(values["mode"])
                    candidate = replace(self.config, mode=mode)
                    candidate.validate()
                except (KeyError, TypeError, ValueError):
                    if command_id is not None:
                        self._acks.append(
                            {
                                "id": command_id,
                                "result": WorkerCommandResult.INVALID_COMMAND,
                            }
                        )
                    return
                if mode == WorkerMode.ACTIVE and not (
                    self.context.runtime_verified
                    and self._game_ready
                    and self._ready_at is not None
                    and self._last_report.healthy
                    and self._last_report.state
                    not in {"ERROR", "NEEDS_ATTENTION", "STOPPED"}
                ):
                    if command_id is not None:
                        self._acks.append(
                            {
                                "id": command_id,
                                "result": WorkerCommandResult.ACTIVE_GATE_CLOSED,
                            }
                        )
                    return
                self.config = candidate
                values = {**values, "mode": mode.value}
            if normalized == WorkerCommandName.GAME_READY:
                self._game_ready = True
            elif normalized == WorkerCommandName.PAUSE:
                self._paused = True
            elif normalized == WorkerCommandName.RESUME:
                self._paused = False
                self._stopped = False
                self._restart_exhausted = False
                self._next_restart_at = None
                self.restart_count = 0
            if self._stopped:
                if command_id is not None:
                    self._acks.append(
                        {
                            "id": command_id,
                            "result": WorkerCommandResult.WORKER_STOPPED,
                        }
                    )
                return
            was_alive = self._process is not None and self._process.is_alive()
            self._start_locked()
            if self._process is None or not self._process.is_alive():
                if command_id is not None:
                    result = (
                        WorkerCommandResult.WORKER_RESTARTING
                        if self._state == WorkerProcessState.BACKOFF
                        else WorkerCommandResult.WORKER_CRASHED
                    )
                    self._acks.append({"id": command_id, "result": result})
                return
            # A newly spawned worker has already received these parent-owned states.
            if (
                not was_alive
                and command_id is None
                and normalized
                in {
                    WorkerCommandName.GAME_READY,
                    WorkerCommandName.RUNTIME_VERIFIED,
                    WorkerCommandName.PAUSE,
                }
            ):
                return
            safety_command = (
                normalized in {WorkerCommandName.GAME_LOST, WorkerCommandName.PAUSE}
                or (
                    normalized == WorkerCommandName.RUNTIME_VERIFIED
                    and not values.get("value")
                )
                or (
                    normalized == WorkerCommandName.SET_MODE
                    and values.get("mode") != WorkerMode.ACTIVE
                )
            )
            if self._enqueue(
                normalized,
                command_id=command_id,
                report_failure=not safety_command,
                **values,
            ):
                return
            if not safety_command:
                return
            stopped = self._force_stop_child()
            if stopped and self._process is not None:
                self._handle_dead_process()
            elif not stopped:
                self._mark_crashed()
            if command_id is not None:
                self._acks.append(
                    {
                        "id": command_id,
                        "result": WorkerCommandResult.FORCED_STOP
                        if stopped
                        else WorkerCommandResult.WORKER_CRASHED,
                    }
                )

    def request_stop(self, command_id: int | None = None) -> None:
        with self._lock:
            self._stopped = True
            self._paused = True
            self._next_restart_at = None
            self._state = WorkerProcessState.STOPPING
            if self._process is None or not self._process.is_alive():
                if self._process is not None:
                    self._process.join(timeout=min(0.1, self.stop_timeout))
                    self._drain()
                    self._fail_pending_commands(WorkerCommandResult.WORKER_CRASHED)
                    self._process = None
                self._close_queues()
                self._state = WorkerProcessState.STOPPED
                if command_id is not None:
                    self._acks.append(
                        {"id": command_id, "result": WorkerCommandResult.OK}
                    )
                return
            if self._enqueue(
                WorkerCommandName.STOP,
                command_id=command_id,
                report_failure=False,
            ):
                return
            stopped = self._force_stop_child()
            self._drain()
            self._fail_pending_commands(
                WorkerCommandResult.WORKER_STOPPED
                if stopped
                else WorkerCommandResult.WORKER_CRASHED
            )
            if command_id is not None:
                self._acks.append(
                    {
                        "id": command_id,
                        "result": WorkerCommandResult.FORCED_STOP
                        if stopped
                        else WorkerCommandResult.WORKER_CRASHED,
                    }
                )
            if not stopped:
                self._mark_crashed()
            else:
                self._state = WorkerProcessState.STOPPED

    def set_game_ready(self, ready: bool) -> None:
        with self._lock:
            self._game_ready = ready
            if not ready and self._process is not None and self._process.is_alive():
                self._paused = True
                if not self._enqueue(WorkerCommandName.GAME_LOST, report_failure=False):
                    stopped = self._force_stop_child()
                    if stopped and self._process is not None:
                        self._handle_dead_process()
                    elif not stopped:
                        self._mark_crashed()
            elif not ready:
                self._paused = True

    def acknowledgements(self) -> list[dict]:
        with self._lock:
            self._drain()
            values, self._acks = self._acks, []
            return values

    def shutdown(self) -> WorkerReport:
        with self._lock:
            self._stopped = True
            self._next_restart_at = None
            forced = False
            had_process = self._process is not None
            if self._process is not None and self._process.is_alive():
                self.request_stop()
                self._process.join(timeout=self.stop_timeout)
                if self._process.is_alive():
                    forced = True
                    logger.error(
                        "worker_stop_timeout runtime_id=%s generation=%s timeout_seconds=%s",
                        self.context.runtime_id, self._worker_generation, self.stop_timeout,
                    )
                    self._force_stop_child()
                if self._process.is_alive():
                    self._mark_crashed()
                    self._fail_pending_commands(WorkerCommandResult.WORKER_CRASHED)
                    # Retain the handle and IPC ownership. Callers must not be told
                    # cleanup completed while an unkillable child is still alive.
                    return self._last_report
            if had_process and self._may_have_live_input():
                emergency_release_all(self.context.display, self.config.bindings)
            if self._process is not None and not self._process.is_alive():
                self._process.join(timeout=min(0.1, self.stop_timeout))
                forced = forced or getattr(self._process, "exitcode", 0) not in {0, None}
            self._drain()
            self._fail_pending_commands(
                WorkerCommandResult.WORKER_CRASHED
                if forced
                else WorkerCommandResult.WORKER_STOPPED
            )
            self._process = None
            self._close_queues()
            if forced:
                self._mark_crashed()
            else:
                # A joined child and successful input cleanup prove shutdown.
                # A prior gameplay intervention must not invalidate adoption.
                self._last_report.state = "STOPPED"
                self._last_report.healthy = True
            self._state = (
                WorkerProcessState.FAILED if forced else WorkerProcessState.STOPPED
            )
            return self._last_report
