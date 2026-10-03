from __future__ import annotations

import http.client
import os
import queue
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.providers.view.base import ViewUnavailable
from app.providers.view.xpra import XpraRuntimeViewProvider
from app.runtime.bootstrap import BootstrapPhase, RuntimeBootstrapService
from app.runtime.bootstrap_models import RuntimeAgentConfig
from app.runtime.display import DisplayEnvironment
from node_agent.config import NodeAgentSettings
from runtime_agent.config import RuntimeAgentSettings
from runtime_agent.gameworker.base import WorkerContext
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.process import WorkerProcessHost
from runtime_agent.main import _handoff_environment, _managed_runtime_is_adoptable
from runtime_agent.process_supervisor import ProcessSupervisor, RestartPolicy
from runtime_agent.processes.dst import DSTProcess, DSTState
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


def test_ready_dst_loss_restarts_after_grace_instead_of_terminal_timeout(tmp_path):
    class Supervisor:
        def __init__(self):
            self.now = 0.0
            self.alive = True
            self.running_for = 2200.0
            self.status = type("Status", (), {"exhausted": False,
                                               "started_at": "started"})()
            self.before_start = None
            self.stops = 0

        def clock(self):
            return self.now

        def shutdown(self, timeout=3):
            self.stops += 1
            self.alive = False

        def request_start(self):
            self.alive = True

    supervisor = Supervisor()
    marker = tmp_path / "dst.ready"
    marker.touch()
    dst = DSTProcess(supervisor, marker, readiness_timeout_seconds=300)
    assert dst.status() == DSTState.READY
    marker.unlink()
    assert dst.status() == DSTState.RUNNING
    supervisor.now = 16.0
    assert dst.status() == DSTState.CRASHED
    assert supervisor.stops == 1
    assert dst.start() == DSTState.STARTING
    assert not dst._terminal_error


def test_dst_lifecycle_reconciles_cluster_created_after_server_start(tmp_path):
    root = tmp_path / "DoNotStarveTogether"
    account = root / "account"
    account.mkdir(parents=True)

    class Supervisor:
        def __init__(self):
            self.now = 0.0
            self.alive = False
            self.running_for = 0.0
            self.status = type("Status", (), {"exhausted": False, "started_at": "started"})()
            self.before_start = None

        def clock(self):
            return self.now

        def request_start(self):
            if self.before_start:
                self.before_start()
            cluster = account / "Cluster_1"
            cluster.mkdir()
            (cluster / "cluster.ini").write_text(
                "[NETWORK]\nidle_timeout = 1800\n", encoding="utf-8"
            )
            self.alive = True

    supervisor = Supervisor()
    dst = DSTProcess(
        supervisor,
        tmp_path / "dst.ready",
        user_root=root,
    )

    dst.start()
    assert "idle_timeout = 1800" in (account / "Cluster_1/cluster.ini").read_text()
    supervisor.now = 30.0
    assert dst.status() == DSTState.RUNNING
    cluster_ini = (account / "Cluster_1/cluster.ini").read_text()

    assert cluster_ini == "[NETWORK]\nidle_timeout = 0\n"
    assert dst._idle_timeout_reconciled


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


def test_supervisor_adopts_only_exact_isolated_launcher_identity(tmp_path):
    command = (sys.executable, "-c", "import time; time.sleep(30)")
    process = subprocess.Popen(command, start_new_session=True)
    original = ProcessSupervisor("dst", command, popen=lambda *_a, **_k: process)
    replacement = ProcessSupervisor("dst", command)
    try:
        original.request_start()
        identity = original.adoption_identity()
        assert identity is not None
        pid, start_ticks = identity
        ready = tmp_path / "steam.ready"
        ready.touch()
        with pytest.raises(RuntimeError, match="cannot safely adopt"):
            replacement.adopt(pid, start_ticks + 1)
        with pytest.raises(RuntimeError, match="cannot safely adopt"):
            ProcessSupervisor("other", ("sleep", "30")).adopt(pid, start_ticks)

        replacement.adopt(pid, start_ticks)
        assert replacement.alive
        assert replacement.status.process_group == pid
        from runtime_agent.processes.steam import SteamProcess, SteamState

        assert SteamProcess(replacement, ready).status() == SteamState.READY
    finally:
        if replacement.alive:
            replacement.shutdown(timeout=1, kill_timeout=1)
        elif process.poll() is None:
            process.kill()
            process.wait(timeout=1)


def test_reload_handoff_requires_identity_for_every_managed_process(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")

    class Managed:
        def __init__(self, identity):
            self.identity = identity

        def adoption_identity(self):
            return self.identity

    monkeypatch.setenv("CONTROL_PLANE_URL", "https://control.example")
    monkeypatch.setenv("RUNTIME_ID", "1")
    monkeypatch.setenv("ACCOUNT_ID", "2")
    monkeypatch.setenv("NODE_ID", "3")
    monkeypatch.setenv("RUNTIME_TOKEN", "runtime-secret")
    settings = RuntimeAgentSettings.from_env()
    monkeypatch.delenv("RUNTIME_TOKEN")
    complete = _handoff_environment(
        Managed((21, 101)), Managed((22, 202)), Managed((23, 303)),
        runtime_token=settings.runtime_token,
    )
    assert complete["RUNTIME_ADOPT_DISPLAY"] == "21:101"
    assert complete["RUNTIME_ADOPT_STEAM"] == "22:202"
    assert complete["RUNTIME_ADOPT_DST"] == "23:303"
    assert complete["RUNTIME_TOKEN"] == "runtime-secret"
    assert "RUNTIME_TOKEN" not in os.environ
    with monkeypatch.context() as handoff:
        handoff.setattr(os, "environ", complete)
        assert RuntimeAgentSettings.from_env().runtime_token == "runtime-secret"
    assert _handoff_environment(
        Managed((21, 101)), Managed(None), Managed((23, 303)),
        runtime_token=settings.runtime_token,
    ) is None


def test_worker_only_stop_failure_does_not_block_identity_valid_agent_reload():
    class Managed:
        alive = True

        def status(self):
            return "READY"

    display = SimpleNamespace(status=lambda: SimpleNamespace(value="READY"))
    steam_process = SimpleNamespace(status=lambda: SteamState.READY)
    dst_process = SimpleNamespace(status=lambda: DSTState.READY)
    identities = {
        "RUNTIME_ADOPT_DISPLAY": "11:101",
        "RUNTIME_ADOPT_STEAM": "12:102",
        "RUNTIME_ADOPT_DST": "13:103",
    }
    # A bounded-force-stop may report worker unhealthy. Managed-process
    # adoption remains gated by each process's own readiness and identity.
    worker_healthy = False
    assert worker_healthy is False
    assert _managed_runtime_is_adoptable(
        display, steam_process, dst_process, Managed(), Managed(), identities
    )
    assert not _managed_runtime_is_adoptable(
        display, steam_process, dst_process, Managed(), Managed(), None
    )


def test_process_supervisor_logs_crash_attempt_exhaustion_and_downtime(caplog):
    clock = [0.0]
    processes = []

    def popen(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    supervisor = ProcessSupervisor(
        "dst",
        ("dst-launcher",),
        restart_policy=RestartPolicy(
            max_attempts=1,
            window_seconds=30,
            initial_backoff_seconds=1,
            max_backoff_seconds=1,
        ),
        clock=lambda: clock[0],
        popen=popen,
    )
    supervisor.request_start()
    processes[-1].exit_code = 1
    supervisor.tick()
    clock[0] = 1.0
    supervisor.tick()
    assert supervisor.status.restart_count == 1
    assert "managed_process_crash name=dst" in caplog.text
    assert "managed_process_restart_attempt name=dst" in caplog.text
    assert "managed_process_restart_started name=dst" in caplog.text

    processes[-1].exit_code = 2
    supervisor.tick()
    assert supervisor.status.exhausted
    assert "managed_process_restart_exhausted name=dst" in caplog.text


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


def test_dead_worker_ipc_is_abandoned_without_reading_partial_queue(monkeypatch):
    class UnreadableQueue(FakeQueue):
        def get_nowait(self):
            raise AssertionError("dead worker queue must not be read")

    context = WorkerContext(1, 2, DisplayEnvironment(":99"), runtime_verified=True)
    host = WorkerProcessHost(WorkerConfig(plugin="dst"), context, max_restarts=0)
    fake_context = FakeMPContext()
    host._ctx = fake_context
    monkeypatch.setattr(
        "runtime_agent.gameworker.process.emergency_release_all",
        lambda *_args, **_kwargs: None,
    )
    host.start()
    host._reports = UnreadableQueue()
    host._ack_reports = UnreadableQueue()
    fake_context.processes[0].alive = False

    host.tick()

    assert host.process_state.value == "FAILED"


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
    monkeypatch.setattr("runtime_agent.gameworker.dst.worker.ObserveActions", Actions)
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


def test_xpra_transport_readiness_requires_a_complete_http_page(monkeypatch):
    responses = [
        http.client.RemoteDisconnected("server closed before HTTP headers"),
        (200, "text/html", b"<title>xpra websockets client</title>"),
    ]

    class Connection:
        def __init__(self, _host, _port, *, timeout):
            assert timeout == 0.5

        def request(self, method, path):
            assert (method, path) == ("GET", "/")

        def getresponse(self):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result

            class Response:
                status = result[0]

                def getheader(self, name, default=""):
                    return result[1] if name == "Content-Type" else default

                def read(self, size):
                    return result[2][:size]

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("app.providers.view.xpra.http.client.HTTPConnection", Connection)

    assert not XpraRuntimeViewProvider._http_transport_ready(14500)
    assert XpraRuntimeViewProvider._http_transport_ready(14500)


def test_xpra_view_shadow_does_not_launch_xsession_commands():
    argv = XpraRuntimeViewProvider._shadow_argv(
        DisplayEnvironment(":100"), 14500
    )

    assert argv[:2] == ("/usr/bin/env", "DISPLAY=:100")
    assert "XPRA_SYSTEM_CONF_DIRS=/dev/null" in argv
    assert argv[argv.index("xpra"):argv.index("xpra") + 2] == ("xpra", "shadow")
    assert "--start=" not in argv
    assert "--start-child=" not in argv
    assert all("=true" not in value for value in argv)


def test_xpra_cleanup_targets_only_the_created_shadow(monkeypatch):
    provider = XpraRuntimeViewProvider(object())
    calls = []

    def run(*args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(provider, "_incus", run)
    provider._cleanup_backend("dst-runtime", "view-123", ":99")

    assert calls[-1][:5] == ("exec", "dst-runtime", "--", "/usr/bin/python3", "-c")
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

    assert calls[-1][:5] == ("exec", "dst-runtime", "--", "/usr/bin/python3", "-c")
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

    assert 'AUTO_LAUNCH_STEAM="1"' in rendered
    assert 'RUNTIME_GENERATION="7"' in rendered
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


def test_bootstrap_publishes_only_nonsecret_image_identity_for_agent_handoff():
    config = RuntimeAgentConfig(
        runtime_id=1,
        account_id=2,
        node_id=3,
        runtime_token="secret-token",
        orchestrator_url="https://control.example",
        protocol_version=1,
        heartbeat_interval=5,
        runtime_image_version="dst-base-v2",
    )

    class Provider:
        def __init__(self):
            self.files = []

        def put_file(self, runtime, path, content, *, mode, correlation_id):
            self.files.append((path, content, mode))

    provider = Provider()
    service = RuntimeBootstrapService(None, provider)
    service._apply(
        BootstrapPhase.AGENT_CONFIGURED,
        SimpleNamespace(id=1),
        config,
        correlation_id="test",
    )

    assert provider.files == [
        ("/etc/dst-runtime/agent.env", config.environment_file(), 0o600),
        ("/etc/dst-runtime/runtime-image-version", b"dst-base-v2\n", 0o644),
    ]
