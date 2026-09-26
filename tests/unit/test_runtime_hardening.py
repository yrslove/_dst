from __future__ import annotations

import os
import queue
import subprocess

import pytest

from app.providers.view.base import ViewUnavailable
from app.providers.view.xpra import XpraRuntimeViewProvider
from app.runtime.bootstrap_models import RuntimeAgentConfig
from app.runtime.display import DisplayEnvironment
from node_agent.config import NodeAgentSettings
from runtime_agent.config import RuntimeAgentSettings
from runtime_agent.gameworker.base import WorkerContext
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.process import WorkerProcessHost
from runtime_agent.process_supervisor import ProcessSupervisor, RestartPolicy
from runtime_agent.processes.steam import SteamProcess, SteamState


class FakeProcess:
    next_pid = 1000

    def __init__(self):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.exit_code = None
        self.terminate_called = False
        self.kill_called = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminate_called = True
        self.exit_code = 0

    def kill(self):
        self.kill_called = True
        self.exit_code = -9


def test_supervisor_stop_during_backoff_allows_fresh_start():
    now = [0.0]
    processes: list[FakeProcess] = []

    def factory(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    supervisor = ProcessSupervisor(
        "dst", ("dst",), backoff_seconds=10, clock=lambda: now[0], popen=factory
    )
    supervisor.request_start()
    processes[-1].exit_code = 1
    supervisor.tick()
    assert len(processes) == 1

    supervisor.shutdown()
    supervisor.request_start()

    assert len(processes) == 2
    assert supervisor.status.desired is True
    assert supervisor.status.restart_count == 0


def test_supervisor_stable_run_resets_failure_budget():
    now = [0.0]
    processes: list[FakeProcess] = []

    def factory(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    supervisor = ProcessSupervisor(
        "dst",
        ("dst",),
        clock=lambda: now[0],
        popen=factory,
        restart_policy=RestartPolicy(
            max_attempts=1,
            initial_backoff_seconds=0,
            max_backoff_seconds=0,
            reset_after_stable_seconds=5,
        ),
    )
    supervisor.request_start()
    processes[-1].exit_code = 1
    supervisor.tick()
    assert supervisor.status.restart_count == 1

    now[0] = 6
    supervisor.tick()
    assert supervisor.status.restart_count == 0
    processes[-1].exit_code = 1
    supervisor.tick()

    assert supervisor.status.restart_count == 1
    assert not supervisor.status.exhausted


def test_duplicate_supervisor_start_is_idempotent():
    processes: list[FakeProcess] = []

    def factory(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    supervisor = ProcessSupervisor("dst", ("dst",), popen=factory)
    supervisor.request_start()
    supervisor.request_start()
    supervisor.tick()

    assert len(processes) == 1


def test_supervisor_uses_only_canonical_graphical_environment(monkeypatch):
    captured = {}

    def factory(*_args, **kwargs):
        captured.update(kwargs["env"])
        return FakeProcess()

    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setenv("XAUTHORITY", "/tmp/unrelated-auth")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/unrelated-runtime")
    monkeypatch.setenv("RUNTIME_TOKEN", "must-not-inherit")
    supervisor = ProcessSupervisor(
        "dst", ("dst",), popen=factory, environment={"DISPLAY": ":99"}
    )

    supervisor.request_start()

    assert captured["DISPLAY"] == ":99"
    assert "XAUTHORITY" not in captured
    assert "XDG_RUNTIME_DIR" not in captured
    assert "RUNTIME_TOKEN" not in captured
    supervisor.shutdown()


def test_stale_readiness_marker_is_removed_before_spawn(tmp_path):
    marker = tmp_path / "steam.ready"
    login_marker = tmp_path / "steam.needs-login"
    marker.write_text("old", encoding="utf-8")
    login_marker.write_text("old", encoding="utf-8")
    future = 4_000_000_000
    os.utime(marker, (future, future))
    process = FakeProcess()
    supervisor = ProcessSupervisor("steam", ("steam",), popen=lambda *_a, **_k: process)
    steam = SteamProcess(
        supervisor,
        marker,
        needs_login_file=login_marker,
        readiness_timeout_seconds=60,
    )

    steam.start()
    assert not marker.exists()
    assert not login_marker.exists()
    assert steam.status() == SteamState.RUNNING

    marker.touch()
    assert steam.status() == SteamState.READY
    process.exit_code = 1
    supervisor.tick()
    assert steam.status() != SteamState.READY


class FakeQueue(queue.Queue):
    def cancel_join_thread(self):
        return None

    def close(self):
        return None


class FakeWorkerProcess:
    def __init__(self, **_kwargs):
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

    def kill(self):
        self.kill_called = True
        self.alive = False


class FakeMPContext:
    def __init__(self):
        self.processes: list[FakeWorkerProcess] = []

    def Queue(self, maxsize=0):
        return FakeQueue(maxsize=maxsize)

    def Process(self, **kwargs):
        process = FakeWorkerProcess(**kwargs)
        self.processes.append(process)
        return process


def test_worker_crash_fails_pending_command_and_replaces_ipc(monkeypatch):
    context = WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    host = WorkerProcessHost(WorkerConfig(plugin="dst"), context, max_restarts=1)
    fake_context = FakeMPContext()
    host._ctx = fake_context
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: None,
    )

    host.command("PAUSE", command_id=77)
    first_commands = host._commands
    fake_context.processes[0].alive = False
    host.tick()

    assert host._commands is not first_commands
    assert host.acknowledgements() == [{"id": 77, "result": "WORKER_CRASHED"}]


def test_game_lost_is_sent_to_live_worker(monkeypatch):
    context = WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    host = WorkerProcessHost(WorkerConfig(plugin="dst"), context)
    host._ctx = FakeMPContext()
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: None,
    )
    host.start()

    host.set_game_ready(False)

    messages = []
    while not host._commands.empty():
        messages.append(host._commands.get_nowait())
    assert messages[-1]["command"] == "GAME_LOST"
    host.shutdown()


def test_stopped_worker_child_is_reaped_without_restart(monkeypatch):
    context = WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    host = WorkerProcessHost(WorkerConfig(plugin="dst"), context)
    fake_context = FakeMPContext()
    host._ctx = fake_context
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: None,
    )
    host.start()
    host.request_stop(command_id=88)
    fake_context.processes[0].alive = False

    report = host.tick()

    assert host._process is None
    assert report.state == "STOPPED"
    assert host.acknowledgements() == [{"id": 88, "result": "WORKER_STOPPED"}]


def test_worker_stop_falls_back_to_kill_when_ipc_is_full(monkeypatch):
    context = WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    host = WorkerProcessHost(WorkerConfig(plugin="dst"), context)
    fake_context = FakeMPContext()
    host._ctx = fake_context
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: None,
    )
    host.start()
    while True:
        try:
            host._commands.put_nowait({"command": "STATUS"})
        except queue.Full:
            break

    host.request_stop(command_id=99)

    assert not fake_context.processes[0].is_alive()
    assert host.acknowledgements() == [{"id": 99, "result": "FORCED_STOP"}]


def test_worker_prepare_cleans_partial_resources(monkeypatch):
    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.OBSERVE))
    monkeypatch.setattr(
        "runtime_agent.gameworker.dst.worker.VisionDetector",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad assets")),
    )

    report = worker.prepare(
        WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    )

    assert report.state == "ERROR"
    assert worker.capture is None
    assert worker.input is None
    assert worker.deadman is None


def test_worker_prepare_closes_executor_created_before_later_failure(monkeypatch):
    calls = []

    class Actions:
        def __init__(self, *_args, **_kwargs):
            pass

        def shutdown(self):
            calls.append("actions")

    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.OBSERVE))
    monkeypatch.setattr("runtime_agent.gameworker.dst.worker.GameActions", Actions)
    monkeypatch.setattr(
        "runtime_agent.gameworker.dst.worker.VisionDetector",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("bad assets")),
    )

    report = worker.prepare(
        WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    )

    assert report.state == "ERROR"
    assert calls == ["actions"]


def test_xpra_cleanup_does_not_hide_operational_failure():
    failed = subprocess.CompletedProcess(
        ["incus"], 1, stdout="", stderr="cannot connect to daemon"
    )
    absent = subprocess.CompletedProcess(
        ["incus"], 1, stdout="", stderr="device not found"
    )

    assert XpraRuntimeViewProvider._idempotent_cleanup_result(absent)
    assert not XpraRuntimeViewProvider._idempotent_cleanup_result(failed)


def test_xpra_cleanup_targets_only_the_created_shadow(monkeypatch):
    provider = XpraRuntimeViewProvider(object())
    calls = []

    def run(*args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_incus", run)
    provider._cleanup_backend("dst-runtime", "view-123", ":99")

    assert calls[-1][:5] == (
        "exec", "dst-runtime", "--", "/usr/bin/python3", "-c"
    )
    assert calls[-1][-2:] == (":99", "14500")


def test_xpra_cleanup_keeps_failed_shadow_termination_visible(monkeypatch):
    provider = XpraRuntimeViewProvider(object())
    calls = []

    def run(*args, **_kwargs):
        calls.append(args)
        if args[:4] == ("exec", "dst-runtime", "--", "/usr/bin/python3"):
            return subprocess.CompletedProcess(args, 7, stderr="still running")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(provider, "_incus", run)
    with pytest.raises(ViewUnavailable):
        provider._cleanup_backend("dst-runtime", "view-123", ":99")

    assert calls[-1][:5] == (
        "exec", "dst-runtime", "--", "/usr/bin/python3", "-c"
    )
    assert calls[-1][-2:] == (":99", "14500")


def test_agent_settings_hide_tokens_and_capture_display(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "https://control.example")
    monkeypatch.setenv("RUNTIME_ID", "1")
    monkeypatch.setenv("ACCOUNT_ID", "2")
    monkeypatch.setenv("NODE_ID", "3")
    monkeypatch.setenv("RUNTIME_TOKEN", "runtime-secret")
    monkeypatch.setenv("DISPLAY", ":123")
    monkeypatch.setenv("XAUTHORITY", "/run/dst-runtime/Xauthority")
    settings = RuntimeAgentSettings.from_env()

    assert settings.display == ":123"
    assert settings.xauthority == "/run/dst-runtime/Xauthority"
    assert settings.xvfb_command[-2:] == (
        "-auth",
        "/run/dst-runtime/Xauthority",
    )
    assert "runtime-secret" not in repr(settings)

    monkeypatch.setenv("NODE_SECRET", "node-secret")
    node = NodeAgentSettings.from_env()
    assert "node-secret" not in repr(node)


def test_auto_launch_rejects_non_readiness_aware_defaults(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_URL", "https://control.example")
    monkeypatch.setenv("RUNTIME_ID", "1")
    monkeypatch.setenv("ACCOUNT_ID", "2")
    monkeypatch.setenv("NODE_ID", "3")
    monkeypatch.setenv("RUNTIME_TOKEN", "runtime-secret")
    monkeypatch.setenv("AUTO_LAUNCH_STEAM", "true")

    with pytest.raises(ValueError, match="readiness-aware"):
        RuntimeAgentSettings.from_env()


def test_bootstrap_environment_uses_runtime_agent_contract():
    config = RuntimeAgentConfig(
        runtime_id=1,
        account_id=2,
        node_id=3,
        runtime_token='secret " value',
        orchestrator_url="https://control.example",
        protocol_version=1,
        heartbeat_interval=5,
        runtime_generation=7,
        steam_enabled=True,
        steam_command="/opt/dst-runtime/bin/launch-steam --profile 'one two'",
        xauthority="/run/dst-runtime/Xauthority",
    )
    rendered = config.environment_file().decode()

    assert "AUTO_LAUNCH_STEAM=\"1\"" in rendered
    assert "RUNTIME_GENERATION=\"7\"" in rendered
    assert "STEAM_ENABLED" not in rendered
    assert "STEAM_COMMAND=" in rendered
    assert 'XAUTHORITY="/run/dst-runtime/Xauthority"' in rendered
    assert 'RUNTIME_TOKEN="secret \\" value"' in rendered
    assert "secret" not in repr(config)
    with pytest.raises(ValueError, match="control character"):
        RuntimeAgentConfig(
            runtime_id=1,
            account_id=2,
            node_id=3,
            runtime_token="bad\nvalue",
            orchestrator_url="https://control.example",
            protocol_version=1,
            heartbeat_interval=5,
        ).environment_file()
