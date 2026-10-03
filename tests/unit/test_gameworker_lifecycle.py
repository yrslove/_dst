from __future__ import annotations

import json
import logging
import multiprocessing as mp
import pickle
import queue
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.actions import ActionName
from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.ipc import (
    WorkerCommandResult,
    WorkerIPCAcknowledgement,
    WorkerIPCCommand,
    WorkerIPCReport,
)
from runtime_agent.gameworker.process import (
    WorkerProcessHost,
    WorkerProcessState,
    _trace_command_pipe_writer,
    _worker_main,
)
from runtime_agent.gameworker.state import WorkerState
from runtime_agent.gameworker.xpra_input import InputError
from runtime_agent.heartbeat import (
    RuntimeVerification,
    VerificationState,
    send_heartbeat,
)
from runtime_agent.worker_bridge import WorkerBridge


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


def test_worker_generation_is_monotonic_and_stale_ack_is_ignored(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    host = fake_host(monkeypatch, max_restarts=1)
    host.start()
    first_generation = host.worker_generation
    host.command("STATUS", command_id=21)
    host._ack_reports.put_nowait(
        WorkerIPCAcknowledgement(first_generation, 21, "OK", report_payload())
    )
    assert host.acknowledgements() == [{"id": 21, "result": "OK"}]
    assert 21 not in host._pending_command_ids
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
    assert "reason=stale_generation" in caplog.text

    host._ack_reports.put_nowait(
        WorkerIPCAcknowledgement(host.worker_generation, 22, "OK", report_payload())
    )
    assert host.acknowledgements() == [{"id": 22, "result": "OK"}]
    assert 22 not in host._pending_command_ids
    assert "worker_ack_host_accepted" in caplog.text
    assert "pending_after=[]" in caplog.text

    host.command("STATUS", command_id=24)
    assert 24 in host._pending_command_ids


def test_worker_bridge_forwards_terminal_ack_and_allows_next_command(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    host = fake_host(monkeypatch)
    monkeypatch.setattr(
        "runtime_agent.worker_bridge.WorkerProcessHost",
        lambda *_args, **_kwargs: host,
    )
    bridge = WorkerBridge(1, 2, DisplayEnvironment(":99"), WorkerConfig(plugin="dst"))
    bridge.apply_commands([{"id": 60, "command": "STATUS"}])
    host._ack_reports.put_nowait(
        WorkerIPCAcknowledgement(host.worker_generation, 60, "OK", report_payload())
    )

    assert bridge.acknowledgements() == [{"id": 60, "result": "OK"}]
    assert bridge._pending_commands == set()
    assert "worker_ack_bridge_received" in caplog.text
    assert "worker_ack_bridge_forward" in caplog.text

    bridge.apply_commands([{"id": 61, "command": "STATUS"}])
    assert 61 in bridge._pending_commands
    assert 61 in host._pending_command_ids


def test_control_plane_submission_logs_terminal_ack_response(monkeypatch, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    sent = []

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "commands": []}

    def post(_url, *, json, headers, timeout):
        sent.append(json)
        return Response()

    monkeypatch.setattr("runtime_agent.heartbeat.httpx.post", post)
    settings = SimpleNamespace(
        runtime_id=2,
        control_plane_url="http://127.0.0.1:8080",
        runtime_token="test-token",
        request_timeout_seconds=1,
        agent_version="test",
        protocol_version=1,
        xdg_runtime_dir=str(tmp_path),
    )

    response = send_heartbeat(
        settings,
        phase="GAME_READY",
        steam_running=True,
        dst_running=True,
        healthy=True,
        details={
            "worker": report_payload(),
            "worker_command_results": [{"id": 60, "result": "OK"}],
        },
    )

    assert response["ok"] is True
    assert sent[0]["worker_command_results"] == [{"id": 60, "result": "OK"}]
    assert "runtime_heartbeat_accepted runtime_id=2 phase=GAME_READY healthy=True" in caplog.text
    heartbeat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert heartbeat["runtime_id"] == 2
    assert heartbeat["phase"] == "GAME_READY"
    assert heartbeat["healthy"] is True
    assert isinstance(heartbeat["revision"], str)
    assert "worker_ack_control_plane_submit" in caplog.text
    assert "worker_ack_control_plane_response" in caplog.text


def test_control_plane_submission_logs_terminal_ack_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO)

    def post(_url, *, json, headers, timeout):
        raise httpx.ConnectError("control plane unavailable")

    monkeypatch.setattr("runtime_agent.heartbeat.httpx.post", post)
    settings = SimpleNamespace(
        runtime_id=2,
        control_plane_url="http://127.0.0.1:8080",
        runtime_token="test-token",
        request_timeout_seconds=1,
        agent_version="test",
        protocol_version=1,
    )

    response = send_heartbeat(
        settings,
        phase="GAME_READY",
        steam_running=True,
        dst_running=True,
        healthy=True,
        details={
            "worker": report_payload(),
            "worker_command_results": [{"id": 60, "result": "OK"}],
        },
    )

    assert response == {"ok": False, "commands": []}
    assert "worker_ack_control_plane_failed" in caplog.text
    assert "test-token" not in caplog.text


def test_runtime_verification_transport_failure_has_bounded_grace_and_recovers():
    verification = RuntimeVerification(grace_seconds=15)
    assert verification.observe({"runtime_verified": True}, now=100) is True
    assert verification.observe({"ok": False}, now=101) is True
    assert verification.state == VerificationState.VERIFICATION_UNKNOWN
    assert verification.observe({"ok": False}, now=116) is False
    assert verification.state == VerificationState.VERIFICATION_UNKNOWN
    assert verification.observe({"runtime_verified": True}, now=117) is True
    assert verification.state == VerificationState.VERIFIED_TRUE


def test_explicit_verification_false_bypasses_transport_grace():
    verification = RuntimeVerification(grace_seconds=15)
    assert verification.observe({"runtime_verified": True}, now=100) is True
    assert verification.observe({"runtime_verified": False}, now=101) is False
    assert verification.state == VerificationState.VERIFIED_FALSE
    assert verification.observe({"ok": False}, now=102) is False


def test_transport_failure_does_not_invent_runtime_verification():
    verification = RuntimeVerification(grace_seconds=15)
    assert verification.observe({"ok": False, "commands": []}, now=100) is False
    assert verification.state == VerificationState.VERIFICATION_UNKNOWN


def test_crash_release_finishes_before_replacement_generation_starts(monkeypatch):
    events = []

    class OrderedContext(FakeMPContext):
        def Process(self, **kwargs):
            events.append("spawn")
            return super().Process(**kwargs)

    host = WorkerProcessHost(
        WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE),
        context(),
        max_restarts=1,
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


def test_active_worker_shutdown_survives_broken_emergency_input(monkeypatch):
    host = WorkerProcessHost(
        WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE), context()
    )
    host._ctx = FakeMPContext()
    host.start()
    host._process.alive = False

    def unavailable(_environment):
        raise InputError("xpra input server connection lost: BlockingIOError")

    monkeypatch.setattr("runtime_agent.gameworker.input.XpraInputDriver", unavailable)
    report = host.shutdown()

    assert host._process is None
    assert report.state == "STOPPED"
    assert host.shutdown().state == "STOPPED"


def test_autostart_off_pause_keeps_disabled_worker_disabled():
    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.DISABLED))

    worker.on_game_ready(context())
    report = worker.pause()

    assert report.mode == WorkerMode.DISABLED
    assert report.state == WorkerState.DISABLED
    assert worker.input is None
    assert worker.actions is None
    assert worker.capture is None


def test_worker_uses_slow_idle_and_burst_verification_cadence():
    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.OBSERVE,
            tick_interval=1.0,
            observation_interval=2.0,
        )
    )

    assert worker._observation_interval() == 4.0
    assert worker.next_tick_interval == 1.0

    worker.pipeline = SimpleNamespace(verification_pending=True)

    assert worker._observation_interval() == 0.15
    assert worker.next_tick_interval == 0.1


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


def test_parent_drain_logs_report_and_ack_boundaries(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    host = fake_host(monkeypatch)
    host.start()
    command_id = 51
    host._pending_command_ids.add(command_id)
    host._command_transport_state[command_id] = "pipe_write_complete"
    host._reports.put_nowait(WorkerIPCReport(host.worker_generation, report_payload()))
    host._ack_reports.put_nowait(
        WorkerIPCAcknowledgement(
            host.worker_generation,
            command_id,
            "OK",
            report_payload(),
        )
    )

    assert host.acknowledgements() == [{"id": command_id, "result": "OK"}]
    assert "worker_ipc_report_drain_begin" in caplog.text
    assert "worker_ipc_report_drain_end" in caplog.text
    assert "messages_drained=1" in caplog.text
    assert "worker_ipc_ack_drain_begin" in caplog.text
    assert "worker_ipc_ack_drain_end" in caplog.text
    assert "pending_after=[]" in caplog.text


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


def test_command_queue_logs_the_actual_pipe_write(caplog):
    caplog.set_level(logging.INFO)
    channel = mp.get_context("spawn").Queue(maxsize=2)
    _trace_command_pipe_writer(
        channel,
        runtime_id=2,
        generation=5,
        transport_state={},
        transport_lock=threading.Lock(),
    )
    command = WorkerIPCCommand(5, "STATUS", 44)
    try:
        channel.put_nowait(command)
        assert channel.get(timeout=2) == command

        deadline = time.monotonic() + 1
        while "worker_command_pipe_write_complete" not in caplog.text:
            if time.monotonic() >= deadline:
                raise AssertionError("command pipe write was not logged")
            threading.Event().wait(0.01)

        assert "worker_command_pipe_write_begin" in caplog.text
        assert "command_id=44" in caplog.text
        assert "payload_bytes=" in caplog.text
    finally:
        channel.close()
        channel.join_thread()


def test_periodic_status_saturation_cannot_evict_command_ack(caplog):
    caplog.set_level(logging.INFO)
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
    assert "worker_ack_child_enqueued" in caplog.text
    assert "worker_command_child_received" in caplog.text
    assert "command_id=41" in caplog.text


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


def test_production_action_permissions_preserve_entry_and_gifts_but_deny_world_activity():
    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.ACTIVE,
            validation_flow_enabled=False,
        )
    )

    permitted = worker._permitted_active_actions()

    assert {
        ActionName.CLICK_HOST_GAME,
        ActionName.SELECT_EXISTING_WORLD,
        ActionName.CLICK_GIFT_ICON,
        ActionName.CLICK_INWORLD_USE_LATER,
    } <= permitted
    assert not {
        ActionName.MOVE_FORWARD,
        ActionName.MOVE_BACKWARD,
        ActionName.TURN_LEFT,
        ActionName.TURN_RIGHT,
        ActionName.CLICK_LOCAL_TARGET,
        ActionName.INTERACT,
        ActionName.PAUSE_WORLD,
        ActionName.RESUME_WORLD,
    } & permitted


def test_worker_only_enables_production_world_entry_when_configured_and_effective_active():
    worker = DSTGameWorker(WorkerConfig(
        plugin="dst",
        mode=WorkerMode.ACTIVE,
        validation_flow_enabled=False,
    ))
    worker.context = context()
    worker._game_ready = True
    worker.capture = object()
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test runtime ready")
    worker.machine.transition(WorkerState.OBSERVING, "test worker ready")
    worker._unknown_since = None

    class Actions:
        safety = None

        def set_safety(self, **values):
            self.safety = values

    worker.actions = Actions()
    worker._sync_action_mode()

    assert worker.activity.production_actions_enabled
    assert worker.actions.safety["configured_mode"] == WorkerMode.ACTIVE
    assert worker.actions.safety["effective_mode"] == WorkerMode.ACTIVE

    worker.mode = WorkerMode.OBSERVE
    worker._sync_action_mode()
    assert not worker.activity.production_actions_enabled

    worker.mode = WorkerMode.DISABLED
    worker._sync_action_mode()
    assert not worker.activity.production_actions_enabled


def test_recoverable_unknown_suppresses_effective_mode_without_disabling_intent():
    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE))
    worker.context = context()
    worker._game_ready = True
    worker.capture = object()
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test ready")
    worker.machine.transition(WorkerState.OBSERVING, "test observation")
    worker._unknown_since = time.monotonic() - 21

    class Actions:
        safety = None

        def set_safety(self, **values):
            self.safety = values

    worker.actions = Actions()

    assert worker.mode == WorkerMode.ACTIVE
    assert worker._effective_mode() == WorkerMode.OBSERVE

    worker._unknown_since = None  # fresh production-ready observation

    assert worker._effective_mode() == WorkerMode.ACTIVE
    worker._sync_action_mode()
    assert worker.actions.safety["effective_mode"] == WorkerMode.ACTIVE


def test_unknown_intervention_releases_input_and_retains_active_intent():
    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE))
    worker.context = context()
    worker._game_ready = True
    worker.capture = None
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test ready")
    worker.machine.transition(WorkerState.OBSERVING, "test observation")
    worker._unknown_since = time.monotonic() - 21

    class Actions:
        released = False
        safety = None

        def release_all(self):
            self.released = True

        def set_safety(self, **values):
            self.safety = values

    actions = Actions()
    worker.actions = actions

    report = worker._handle_unknown_timeout()

    assert actions.released
    assert actions.safety["effective_mode"] == WorkerMode.OBSERVE
    assert worker.mode == WorkerMode.ACTIVE
    assert report.mode == WorkerMode.OBSERVE
    assert report.error_code == "WORKER_INTERVENTION_REQUIRED"


def test_explicit_disable_is_not_overridden_by_recovery():
    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.DISABLED))
    worker._unknown_since = None

    report = worker.set_mode(WorkerMode.DISABLED)

    assert worker.mode == WorkerMode.DISABLED
    assert report.mode == WorkerMode.DISABLED


def test_explicit_intervention_resume_preserves_configured_active_intent():
    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE))
    worker.context = context()
    worker._game_ready = True
    worker.capture = object()
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test ready")
    worker.machine.transition(WorkerState.OBSERVING, "test observation")
    worker.machine.transition(WorkerState.NEEDS_ATTENTION, "test intervention")
    worker.activity.intervention_required = True

    class Actions:
        def reset_cancel(self):
            pass

        def set_safety(self, **_values):
            pass

    worker.actions = Actions()

    report = worker.resume()

    assert worker.mode == WorkerMode.ACTIVE
    assert not worker.activity.intervention_required
    assert report.state == WorkerState.OBSERVING
    assert report.mode == WorkerMode.OBSERVE
    worker._unknown_since = None  # fresh verified observation after resume
    worker._sync_action_mode()
    assert worker.status().mode == WorkerMode.ACTIVE


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


def test_noop_game_ready_ack_with_autostart_disabled():
    commands, reports, acknowledgements = FakeQueue(), FakeQueue(), FakeQueue()
    thread = threading.Thread(target=_worker_main, args=(
        WorkerConfig(plugin="noop", autostart=False), context(), 9,
        commands, reports, acknowledgements,
    ))
    thread.start()
    try:
        commands.put_nowait(WorkerIPCCommand(9, "GAME_READY", 51))
        assert acknowledgements.get(timeout=2).result == "OK"
    finally:
        commands.put_nowait(WorkerIPCCommand(9, "STOP", 52))
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_clean_shutdown_clears_prior_gameplay_intervention_health():
    host = WorkerProcessHost(WorkerConfig(plugin='dst', mode=WorkerMode.DISABLED), context())
    host._last_report.state = 'NEEDS_ATTENTION'
    host._last_report.healthy = False
    report = host.shutdown()
    assert report.state == 'STOPPED'
    assert report.healthy
    assert host._process is None


def test_worker_stop_does_not_wait_for_undrained_large_status_queue(monkeypatch):
    from runtime_agent.gameworker.noop import NoopGameWorker

    class LargeStatusWorker(NoopGameWorker):
        def prepare(self, context):
            report = super().prepare(context)
            report.telemetry = {'padding': 'x' * 262144}
            return report

    monkeypatch.setattr('runtime_agent.gameworker.process.NoopGameWorker', LargeStatusWorker)
    ctx = mp.get_context('fork')
    commands, reports, acknowledgements = (ctx.Queue(maxsize=64) for _ in range(3))
    # Fill the pipe with replaceable status before STOP; the parent deliberately
    # does not drain it while joining, matching Runtime Agent reload.
    process = ctx.Process(target=_worker_main, args=(
        WorkerConfig(plugin='noop', autostart=False), context(), 1,
        commands, reports, acknowledgements,
    ))
    process.start()
    try:
        commands.put(WorkerIPCCommand(1, 'STOP', 77))
        ack = acknowledgements.get(timeout=5)
        assert ack.command_id == 77 and ack.result == 'OK'
        process.join(timeout=5)
        assert not process.is_alive()
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(timeout=2)
        for channel in (commands, reports, acknowledgements):
            channel.cancel_join_thread()
            channel.close()


def stationary_health_worker(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('runtime_agent.gameworker.dst.worker.time.monotonic', lambda: now[0])
    worker = DSTGameWorker(WorkerConfig(plugin='dst', mode=WorkerMode.ACTIVE))
    worker.machine._state = WorkerState.WAITING
    worker._game_ready = True
    worker._liveness_started_at = now[0]
    worker.stationary.state = 'STATIONARY_WAIT'
    health = {'last_capture_success_monotonic': now[0],
              'last_valid_observation_monotonic': now[0]}
    worker.pipeline = SimpleNamespace(health=lambda: dict(health))
    return worker, now, health


def test_stationary_eight_hours_fresh_hud_without_actions_is_healthy(monkeypatch):
    worker, now, health = stationary_health_worker(monkeypatch)
    for seconds in range(0, 8 * 3600, 4):
        now[0] = 100 + seconds
        health.update(last_capture_success_monotonic=now[0],
                      last_valid_observation_monotonic=now[0])
        report = worker.status()
        assert report.healthy
        assert report.telemetry['actions_count'] == 0
        assert report.telemetry['stationary']['movement_count'] == 0
        assert report.telemetry['stationary']['state'] == 'STATIONARY_WAIT'


@pytest.mark.parametrize('stale_key,reason', [
    ('last_capture_success_monotonic', 'CAPTURE_STALE'),
    ('last_valid_observation_monotonic', 'OBSERVATION_STALE'),
])
def test_stationary_stale_capture_or_hud_is_unhealthy(monkeypatch, stale_key, reason):
    worker, now, health = stationary_health_worker(monkeypatch)
    now[0] += worker.LIVENESS_TIMEOUT_SECONDS + .01
    for key in health:
        if key != stale_key:
            health[key] = now[0]
    report = worker.status()
    assert not report.healthy
    assert reason in report.telemetry['liveness']['reasons']


def test_worker_loop_reports_timeout_without_action_activity(monkeypatch):
    now = [100.0]
    host = fake_host(monkeypatch, clock=lambda: now[0])
    host.start()
    for seconds in range(0, 8 * 3600, 4):
        now[0] = 100 + seconds
        report = report_payload('WAITING')
        report['telemetry'] = {'worker_loop_monotonic': now[0], 'actions_count': 0}
        host._reports.put(WorkerIPCReport(host.worker_generation, report))
        assert host.tick().healthy
    now[0] += 30.01
    result = host.tick()
    assert not result.healthy
    assert result.error_code == 'WORKER_HEARTBEAT_STALE'


def test_cooperative_stop_has_bounded_grace_without_masking_hang(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()
    child = host._process
    def cooperative_join(timeout=None):
        if timeout >= 4:
            child.alive = False
    child.join = cooperative_join
    result = host.shutdown()
    assert result.healthy
    assert not child.terminate_called
    stuck = fake_host(monkeypatch)
    stuck.start()
    child = stuck._process
    result = stuck.shutdown()
    assert child.terminate_called
    assert not result.healthy
    assert result.error_code == 'WORKER_CRASHED'


def test_unexpected_exit_during_stop_is_not_healthy(monkeypatch):
    host = fake_host(monkeypatch)
    host.start()
    host._process.alive = False
    host._process.exitcode = 1
    assert not host.shutdown().healthy


def test_dead_worker_is_unhealthy_during_restart_backoff(monkeypatch):
    host = fake_host(monkeypatch, restart_backoff_seconds=10)
    host.start()
    host._process.alive = False
    report = host.tick()
    assert not report.healthy
    assert report.error_code == 'WORKER_CRASHED'
