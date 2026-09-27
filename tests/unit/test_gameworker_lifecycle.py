from __future__ import annotations

import pickle
import queue
import threading
import time

import pytest

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.ipc import (
    WorkerCommandResult,
    WorkerIPCAcknowledgement,
    WorkerIPCCommand,
    WorkerIPCReport,
)
from runtime_agent.gameworker.process import (
    WorkerProcessHost,
    WorkerProcessState,
    _worker_main,
)


class FakeQueue(queue.Queue):
    def cancel_join_thread(self):
        return None

    def close(self):
        return None


class FakeProcess:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.alive = False
        self.terminate_called = False
        self.kill_called = False

    def start(self):
        self.alive = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        return None

    def terminate(self):
        self.terminate_called = True
        self.alive = False

    def kill(self):
        self.kill_called = True
        self.alive = False


class FakeMPContext:
    def __init__(self):
        self.processes: list[FakeProcess] = []

    def Queue(self, maxsize=0):
        return FakeQueue(maxsize=maxsize)

    def Process(self, **kwargs):
        process = FakeProcess(**kwargs)
        self.processes.append(process)
        return process


def context(*, verified=True, runtime_generation=4):
    return WorkerContext(
        1,
        2,
        DisplayEnvironment(":99"),
        runtime_verified=verified,
        runtime_generation=runtime_generation,
    )


def report_payload(state="PAUSED"):
    return WorkerReport("noop", "1.0", 1, "DISABLED", state, True).as_dict()


def fake_host(monkeypatch, **kwargs):
    host = WorkerProcessHost(WorkerConfig(plugin="dst"), context(), **kwargs)
    host._ctx = FakeMPContext()
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: None,
    )
    return host


def test_ipc_contract_and_worker_context_are_pickle_safe():
    command = WorkerIPCCommand(3, "SET_MODE", 7, {"mode": "OBSERVE"})
    status = WorkerIPCReport(3, report_payload())
    ack = WorkerIPCAcknowledgement(3, 7, "OK", report_payload())

    assert pickle.loads(pickle.dumps((context(), command, status, ack))) == (
        context(),
        command,
        status,
        ack,
    )


def test_worker_generation_is_monotonic_and_stale_ack_is_ignored(monkeypatch):
    host = fake_host(monkeypatch, max_restarts=1)
    host.start()
    first_generation = host.worker_generation
    first_process = host._process
    first_process.alive = False

    host.tick()

    assert host.worker_generation == first_generation + 1
    assert len(host._ctx.processes) == 2
    host.command("PAUSE", command_id=22)
    host._ack_reports.put_nowait(
        WorkerIPCAcknowledgement(first_generation, 22, "OK", report_payload())
    )
    assert host.acknowledgements() == []
    assert 22 in host._pending_command_ids

    host._ack_reports.put_nowait(
        WorkerIPCAcknowledgement(host.worker_generation, 22, "OK", report_payload())
    )
    assert host.acknowledgements() == [{"id": 22, "result": "OK"}]


def test_crash_release_finishes_before_replacement_generation_starts(monkeypatch):
    events = []

    class OrderedContext(FakeMPContext):
        def Process(self, **kwargs):
            events.append("spawn")
            return super().Process(**kwargs)

    host = WorkerProcessHost(
        WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE),
        context(), max_restarts=1,
    )
    host._ctx = OrderedContext()
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: events.append("release"),
    )
    host.start()
    events.clear()
    host._process.alive = False

    host.tick()

    assert events == ["release", "spawn"]
    assert host.worker_generation == 2


@pytest.mark.parametrize("mode", [WorkerMode.DISABLED, WorkerMode.OBSERVE])
def test_input_free_worker_shutdown_never_opens_emergency_channel(monkeypatch, mode):
    host = WorkerProcessHost(WorkerConfig(plugin="dst", mode=mode), context())
    host._ctx = FakeMPContext()
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args: pytest.fail("input-free mode opened live input"),
    )
    host.start()
    host._process.alive = False
    host.shutdown()


def test_duplicate_ack_for_completed_command_is_ignored(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()
    host.command("STATUS", command_id=23)
    acknowledgement = WorkerIPCAcknowledgement(
        host.worker_generation, 23, "OK", report_payload()
    )
    host._ack_reports.put_nowait(acknowledgement)
    host._ack_reports.put_nowait(acknowledgement)

    assert host.acknowledgements() == [{"id": 23, "result": "OK"}]


def test_command_envelope_is_bound_to_current_worker_generation(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()
    while not host._commands.empty():
        host._commands.get_nowait()

    host.command("STATUS", command_id=11)

    message = host._commands.get_nowait()
    assert message.worker_generation == host.worker_generation
    assert message.command_id == 11
    assert message.command == "STATUS"


def test_periodic_status_saturation_cannot_evict_command_ack():
    commands = FakeQueue(maxsize=4)
    reports = FakeQueue(maxsize=1)
    acknowledgements = FakeQueue(maxsize=2)
    reports.put_nowait("occupied-by-replaceable-status")
    thread = threading.Thread(
        target=_worker_main,
        args=(
            WorkerConfig(plugin="noop"),
            context(verified=False),
            9,
            commands,
            reports,
            acknowledgements,
        ),
    )
    thread.start()
    commands.put_nowait(WorkerIPCCommand(9, "STATUS", 41))

    ack = acknowledgements.get(timeout=1)
    assert ack.command_id == 41
    assert ack.result == "OK"

    commands.put_nowait(WorkerIPCCommand(9, "STOP", 42))
    assert acknowledgements.get(timeout=1).command_id == 42
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_noop_runs_through_real_process_lifecycle_without_game():
    host = WorkerProcessHost(
        WorkerConfig(plugin="noop"),
        context(verified=False),
        ready_timeout=5,
        stop_timeout=2,
    )
    try:
        host.start()
        deadline = time.monotonic() + 5
        report = host.tick()
        while host.process_state == WorkerProcessState.STARTING:
            if time.monotonic() >= deadline:
                raise AssertionError("NOOP worker did not become ready")
            threading.Event().wait(0.01)
            report = host.tick()

        assert host.worker_generation == 1
        assert host.process_state == WorkerProcessState.RUNNING
        assert report.state == "WORKER_IDLE"

        host.command("STATUS", command_id=43)
        acknowledgements = []
        while not acknowledgements:
            if time.monotonic() >= deadline:
                raise AssertionError("NOOP worker did not acknowledge STATUS")
            threading.Event().wait(0.01)
            acknowledgements = host.acknowledgements()
        assert acknowledgements == [{"id": 43, "result": "OK"}]
    finally:
        result = host.shutdown()
    assert result.state == "STOPPED"
    assert host.process_state == WorkerProcessState.STOPPED


def test_duplicate_concurrent_start_creates_one_process(monkeypatch):
    host = fake_host(monkeypatch)
    threads = [threading.Thread(target=host.start) for _ in range(20)]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(host._ctx.processes) == 1
    assert host.worker_generation == 1


def test_stop_during_restart_backoff_cancels_future_spawn(monkeypatch):
    now = [100.0]
    host = fake_host(
        monkeypatch,
        max_restarts=2,
        restart_backoff_seconds=5,
        clock=lambda: now[0],
    )
    host.start()
    host._process.alive = False
    host.tick()

    assert host.process_state == WorkerProcessState.BACKOFF
    assert len(host._ctx.processes) == 1
    host.request_stop(command_id=9)
    now[0] += 100
    host.tick()

    assert len(host._ctx.processes) == 1
    assert host.process_state == WorkerProcessState.STOPPED
    assert host.acknowledgements() == [{"id": 9, "result": "OK"}]


def test_command_observes_crash_budget_before_attempting_restart(monkeypatch):
    now = [100.0]
    host = fake_host(
        monkeypatch,
        max_restarts=2,
        restart_backoff_seconds=5,
        clock=lambda: now[0],
    )
    host.start()
    host.command("STATUS", command_id=70)
    host._process.alive = False

    host.command("STATUS", command_id=71)

    assert len(host._ctx.processes) == 1
    assert host.restart_count == 1
    assert host.acknowledgements() == [
        {"id": 70, "result": WorkerCommandResult.WORKER_CRASHED},
        {"id": 71, "result": WorkerCommandResult.WORKER_RESTARTING},
    ]


def test_worker_ready_timeout_is_bounded_and_terminal_without_retry(monkeypatch):
    now = [10.0]
    host = fake_host(
        monkeypatch,
        max_restarts=0,
        ready_timeout=2,
        clock=lambda: now[0],
    )
    host.start()
    process = host._process
    now[0] += 3

    report = host.tick()

    assert process.terminate_called
    assert host.process_state == WorkerProcessState.FAILED
    assert report.error_code == "WORKER_CRASHED"


def test_invalid_mode_is_rejected_without_mutating_host_config(monkeypatch):
    host = WorkerProcessHost(WorkerConfig(plugin="noop"), context(verified=False))
    host._ctx = FakeMPContext()
    host.start()

    host.command("SET_MODE", command_id=55, mode=WorkerMode.ACTIVE)

    assert host.config.mode == WorkerMode.DISABLED
    assert host.acknowledgements() == [
        {"id": 55, "result": WorkerCommandResult.INVALID_COMMAND}
    ]


def test_explicit_stop_prevents_automatic_restart(monkeypatch):
    host = fake_host(monkeypatch, max_restarts=3)
    host.start()
    host.request_stop()
    host._process.alive = False

    host.tick()
    host.start()

    assert len(host._ctx.processes) == 1
    assert host.process_state == WorkerProcessState.STOPPED


def test_resume_while_stop_is_in_flight_is_rejected(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()
    host.request_stop(command_id=80)

    host.command("RESUME", command_id=81)

    assert host.acknowledgements() == [
        {"id": 81, "result": WorkerCommandResult.WORKER_STOPPING}
    ]
    assert host.process_state == WorkerProcessState.STOPPING


def test_non_serializable_command_value_is_rejected(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()

    host.command("STATUS", command_id=91, unsafe=object())

    assert host.acknowledgements() == [
        {"id": 91, "result": WorkerCommandResult.INVALID_COMMAND}
    ]


def test_full_queue_forces_fail_closed_pause_and_restarts_paused(monkeypatch):
    host = fake_host(monkeypatch, max_restarts=1)
    host.start()
    while not host._commands.empty():
        host._commands.get_nowait()
    while True:
        try:
            host._commands.put_nowait(WorkerIPCCommand(1, "STATUS"))
        except queue.Full:
            break

    host.command("PAUSE", command_id=92)

    assert len(host._ctx.processes) == 2
    assert not host._ctx.processes[0].is_alive()
    assert host.acknowledgements() == [
        {"id": 92, "result": WorkerCommandResult.FORCED_STOP}
    ]
    restarted_commands = []
    while not host._commands.empty():
        restarted_commands.append(host._commands.get_nowait().command)
    assert "PAUSE" in restarted_commands


def test_active_mode_command_is_rejected_until_worker_gate_is_ready(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()

    host.command("SET_MODE", command_id=93, mode="ACTIVE")

    assert host.config.mode == WorkerMode.DISABLED
    assert host.acknowledgements() == [
        {"id": 93, "result": WorkerCommandResult.ACTIVE_GATE_CLOSED}
    ]


def test_configured_active_mode_reports_safe_effective_mode_without_gate():
    from runtime_agent.gameworker.dst.worker import DSTGameWorker

    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE))
    result = worker.prepare(context(verified=False))

    assert result.mode == WorkerMode.OBSERVE
    assert result.details["requested_mode"] == WorkerMode.ACTIVE
    assert result.error_code == "WORKER_DISABLED"


def test_worker_context_rejects_unbounded_metadata():
    with pytest.raises(ValueError, match="metadata"):
        WorkerContext(1, 2, DisplayEnvironment(":99"), metadata={"token": "secret"})


def test_host_rejects_context_from_another_runtime_generation(monkeypatch):
    host = fake_host(monkeypatch)

    with pytest.raises(ValueError, match="identity"):
        host.update_context(context(runtime_generation=5))


def test_replay_mode_fails_closed_without_a_session():
    config = WorkerConfig(plugin="dst", mode=WorkerMode.REPLAY)

    with pytest.raises(ValueError, match="requires replay_session_path"):
        config.validate()


def test_dst_shutdown_is_valid_before_prepare():
    from runtime_agent.gameworker.dst.worker import DSTGameWorker

    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.OBSERVE))

    result = worker.shutdown()

    assert result.state == "STOPPED"
    assert result.healthy


def test_dst_cleanup_failure_does_not_skip_remaining_resources():
    from runtime_agent.gameworker.dst.worker import DSTGameWorker

    calls = []

    class FailingActions:
        def cancel(self):
            calls.append("actions")
            raise RuntimeError("action cleanup failed")

    class FailingDeadman:
        def close(self):
            calls.append("deadman")
            raise RuntimeError("deadman cleanup failed")

    class Input:
        def close(self):
            calls.append("input")

    class Capture:
        def close(self):
            calls.append("capture")

    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.OBSERVE))
    worker.actions = FailingActions()
    worker.deadman = FailingDeadman()
    worker.input = Input()
    worker.capture = Capture()

    result = worker.shutdown()

    assert calls == ["actions", "deadman", "input", "capture"]
    assert result.state == "STOPPED"
    assert not result.healthy
    assert result.error_code == "WORKER_CLEANUP_FAILED"
    assert worker.actions is None
    assert worker.deadman is None
    assert worker.input is None
    assert worker.capture is None


def test_noop_alias_preserves_disabled_wire_value(monkeypatch):
    monkeypatch.setenv("WORKER_MODE", "NOOP")
    config = WorkerConfig.from_env(plugin="noop")

    assert config.mode is WorkerMode.DISABLED
    assert config.mode.value == "DISABLED"
