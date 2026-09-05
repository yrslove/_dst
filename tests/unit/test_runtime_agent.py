
from datetime import datetime, timedelta, timezone

from app.workers.contracts import WorkerContext
from app.workers.noop import NoopGameWorker
from runtime_agent.process_supervisor import ProcessSupervisor
from runtime_agent.steam import is_ready


class FakeProcess:
    next_pid = 100

    def __init__(self):
        self.pid = FakeProcess.next_pid
        FakeProcess.next_pid += 1
        self.exit_code = None

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.exit_code = 0

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.exit_code = -9


def test_process_restart_is_bounded():
    processes = []

    def factory(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    supervisor = ProcessSupervisor(
        "dst",
        ("dst",),
        max_restarts=2,
        backoff_seconds=0,
        popen=factory,
    )
    supervisor.request_start()
    processes[-1].exit_code = 1
    supervisor.tick()
    processes[-1].exit_code = 1
    supervisor.tick()
    processes[-1].exit_code = 1
    supervisor.tick()
    assert len(processes) == 3
    assert supervisor.status.restart_count == 2
    assert supervisor.status.exhausted is True


def test_process_existence_is_not_readiness(tmp_path):
    marker = tmp_path / "steam.ready"
    assert is_ready(process_alive=True, readiness_file=marker) is False
    marker.touch()
    started = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert is_ready(process_alive=True, readiness_file=marker, started_at=started) is True
    future = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    assert is_ready(process_alive=True, readiness_file=marker, started_at=future) is False
    assert is_ready(process_alive=False, readiness_file=marker) is False


def test_game_worker_remains_noop():
    worker = NoopGameWorker()
    context = WorkerContext(account_id=1, runtime_id=1, state="GAME_READY")
    report = worker.on_game_ready(context)
    assert report.phase == "WORKER_IDLE"
    assert report.healthy is True
    assert worker.get_status(context).phase == "WORKER_IDLE"


def test_missing_executable_has_bounded_retries():
    attempts = []

    def missing(*args, **kwargs):
        attempts.append(1)
        raise FileNotFoundError("missing executable")

    supervisor = ProcessSupervisor("dst", ("missing",), max_restarts=3, backoff_seconds=0, popen=missing)
    for _ in range(20):
        supervisor.request_start()
        supervisor.tick()
    assert len(attempts) == 4
    assert supervisor.status.exhausted
