
import os
from datetime import datetime, timedelta, timezone

from app.workers.contracts import WorkerContext
from app.workers.noop import NoopGameWorker
from runtime_agent.process_supervisor import ProcessSupervisor
from runtime_agent.processes.dst import DSTProcess
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


def test_dst_restart_reconciles_profile_and_invalidates_previous_process_evidence(
    tmp_path,
):
    processes = []

    def factory(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    world_root = tmp_path / "klei"
    cluster = world_root / "123" / "Cluster_1"
    cluster.mkdir(parents=True)
    evidence = tmp_path / "run" / "world-profile.json"
    evidence.parent.mkdir()
    evidence.write_text('{"process_id": 1}')
    supervisor = ProcessSupervisor(
        "dst", ("dst",), backoff_seconds=0, popen=factory
    )
    DSTProcess(
        supervisor,
        tmp_path / "dst.ready",
        account_id=1,
        runtime_id=1,
        runtime_generation=2,
        safe_idle_world_profile=True,
        user_root=world_root,
        evidence_path=evidence,
    )
    supervisor.request_start()
    config = cluster / "worldgenoverride.lua"
    assert config.is_file()
    assert not evidence.exists()
    config.unlink()
    processes[-1].exit_code = 1
    supervisor.tick()
    assert len(processes) == 2
    assert config.is_file()
    assert not evidence.exists()


def test_dst_adoption_accepts_only_evidence_for_matching_process_generation(tmp_path):
    from types import SimpleNamespace

    from app.runtime.world_profile import (
        reconcile_world_profile,
        record_process_evidence,
    )

    world_root = tmp_path / "klei"
    cluster = world_root / "123" / "Cluster_1"
    cluster.mkdir(parents=True)
    reconciliation = reconcile_world_profile(user_root=world_root)
    evidence_path = tmp_path / "run" / "world-profile.json"
    record_process_evidence(
        reconciliation=reconciliation,
        account_id=2,
        runtime_id=1,
        runtime_generation=3,
        process_id=314,
        process_start_ticks=880,
        process_started_at="2026-09-29T21:00:00+00:00",
        evidence_path=evidence_path,
    )
    ready = tmp_path / "dst.ready"
    ready.touch()
    supervisor = SimpleNamespace(
        before_start=None,
        alive=True,
        status=SimpleNamespace(
            pid=314,
            started_at="2026-09-29T21:00:00+00:00",
            exhausted=False,
        ),
        process_identity=lambda _pid: (880, 314, 314),
    )
    process = DSTProcess(
        supervisor,
        ready,
        account_id=2,
        runtime_id=1,
        runtime_generation=3,
        safe_idle_world_profile=True,
        user_root=world_root,
        evidence_path=evidence_path,
    )
    assert process.world_profile_evidence()["status"] == "VERIFIED"
    supervisor.status.pid = 315
    assert process.world_profile_evidence()["status"] == "UNVERIFIED"


def test_process_existence_is_not_readiness(tmp_path):
    marker = tmp_path / "steam.ready"
    assert is_ready(process_alive=True, readiness_file=marker) is False
    marker.touch()
    started = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    assert is_ready(process_alive=True, readiness_file=marker, started_at=started) is True
    future = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    assert is_ready(process_alive=True, readiness_file=marker, started_at=future) is False
    os.utime(marker, (4_000_000_000, 4_000_000_000))
    assert is_ready(process_alive=True, readiness_file=marker, started_at=started) is False
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
