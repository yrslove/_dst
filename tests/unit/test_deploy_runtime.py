from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.runtime.bootstrap import SYSTEMD_UNIT
from scripts.deploy_runtime import (
    GUEST_FINISH_STALLED_AGENT_RECOVERY,
    GUEST_INSTALLER,
    GUEST_PROBE,
    GUEST_RECOVER_STALLED_AGENT,
    DeployError,
    assert_clean_tree,
    guest_probe,
    make_metadata,
    parse_metadata,
    require_revision,
    runtime_files,
)


def test_runtime_manifest_excludes_non_runtime_content() -> None:
    repo = Path(__file__).resolve().parents[2]

    paths = {path.relative_to(repo).as_posix() for path in runtime_files(repo)}

    assert "runtime_agent/main.py" in paths
    assert "runtime_agent/gameworker/dst/assets/manifest.json" in paths
    assert "runtime_agent/gameworker/dst/assets/main_menu_host_game.png" in paths
    assert "app/runtime/display.py" in paths
    assert "app/runtime/world_profile.py" in paths
    assert "app/subprocess_env.py" in paths
    assert "app/runtime/bootstrap.py" not in paths
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in (
            ".git/",
            ".venv/",
            "tests/",
            "IO_REPORT.md",
            "recordings/",
            "world_saves/",
            "screenshots/",
        )
    )
    assert "runtime_agent/gameworker/dst/assets/samples/perception_corpus.json" not in paths


def test_dirty_tree_guard_only_allows_known_untracked_io_report() -> None:
    assert_clean_tree(["?? IO_REPORT.md"])

    with pytest.raises(DeployError, match="dirty checkout"):
        assert_clean_tree([" M runtime_agent/main.py"])


def test_deployment_metadata_round_trips_commit_identity() -> None:
    revision = "a" * 40
    metadata = make_metadata(revision, [], Path("."))

    parsed = parse_metadata(json.dumps(metadata))

    assert parsed["commit"] == revision
    assert parsed["dirty"] is False
    assert isinstance(parsed["deployed_at_utc"], str)


def test_revision_verification_fails_closed_on_mismatch() -> None:
    with pytest.raises(DeployError, match="revision mismatch"):
        require_revision("a" * 40, "b" * 40)


def test_guest_metadata_without_a_commit_is_rejected() -> None:
    with pytest.raises(DeployError, match="no valid commit"):
        parse_metadata('{"dirty": false}')


def test_guest_helpers_are_valid_python() -> None:
    compile(GUEST_INSTALLER, "<guest-installer>", "exec")
    compile(GUEST_PROBE, "<guest-probe>", "exec")
    compile(GUEST_RECOVER_STALLED_AGENT, "<stalled-agent-recovery>", "exec")
    compile(
        GUEST_FINISH_STALLED_AGENT_RECOVERY,
        "<finish-stalled-agent-recovery>",
        "exec",
    )
    assert '"app/runtime/world_profile.py"' in GUEST_INSTALLER


def test_runtime_agent_unit_uses_agent_only_reload_and_managed_shutdown() -> None:
    repo = Path(__file__).resolve().parents[2]
    deployed_unit = (repo / "deploy/systemd/dst-runtime-agent.service").read_text()

    for unit in (SYSTEMD_UNIT, deployed_unit):
        assert "ExecReload=/bin/kill -HUP $MAINPID" in unit
        assert "KillMode=mixed" in unit
        assert "RuntimeDirectoryPreserve=restart" in unit


def test_deploy_probe_rejects_stopped_agent_pid(monkeypatch) -> None:
    payload = {
        "active": True,
        "agent_pid": 491,
        "agent_state": "T",
        "runtime_id": "1",
        "service_layout_ok": True,
        "worker_mode": "DISABLED",
        "worker_autostart": "0",
        "validation_flow": "0",
        "validation_movement": "0",
        "processes": {"Xvfb": 498, "steam": 507, "dontstarve": 1237},
        "ready": {"steam": True, "dst": True},
    }
    monkeypatch.setattr("scripts.deploy_runtime.run", lambda *_args: json.dumps(payload))

    with pytest.raises(DeployError, match="MainPID is stopped"):
        guest_probe("dst-000001-g1")


def test_stalled_agent_recovery_preserves_cgroup_children_and_adopts_launchers() -> None:
    assert "KillMode=process" in GUEST_RECOVER_STALLED_AGENT
    assert "TimeoutStopSec=5" in GUEST_RECOVER_STALLED_AGENT
    assert "RuntimeDirectoryPreserve=restart" in GUEST_RECOVER_STALLED_AGENT
    assert '"systemctl", "start", service' in GUEST_RECOVER_STALLED_AGENT
    assert "DST_ADOPT_GAME_PID" in GUEST_RECOVER_STALLED_AGENT
    assert GUEST_RECOVER_STALLED_AGENT.index('"systemctl", "start", service') < GUEST_RECOVER_STALLED_AGENT.index('steam_ready.touch()')
    assert 'f"RUNTIME_ADOPT_{name}={identity}"' in GUEST_RECOVER_STALLED_AGENT
    assert '"systemctl", "unset-environment"' in GUEST_FINISH_STALLED_AGENT_RECOVERY


def test_deploy_probe_allows_failed_agent_only_for_verified_live_game_adoption(monkeypatch) -> None:
    payload = {
        "active": False,
        "agent_pid": 0,
        "runtime_id": "1",
        "service_core_ok": True,
        "service_layout_ok": False,
        "worker_mode": "DISABLED",
        "worker_autostart": "0",
        "validation_flow": "0",
        "validation_movement": "0",
        "processes": {"Xvfb": 498, "steam": 600, "dontstarve_stea": 1237},
        "dst_game_pid": 1237,
        "ready": {"steam": False, "dst": False},
    }
    monkeypatch.setattr("scripts.deploy_runtime.run", lambda *_args: json.dumps(payload))

    result = guest_probe("dst-000001-g1", allow_inactive=True)

    assert result["dst_game_pid"] == 1237
    with pytest.raises(DeployError, match="not active"):
        guest_probe("dst-000001-g1")


def test_deploy_probe_allows_healthy_agent_startup_before_game_ready(monkeypatch) -> None:
    payload = {
        "active": True,
        "agent_pid": 491,
        "agent_state": "S",
        "runtime_id": "1",
        "service_core_ok": True,
        "service_layout_ok": True,
        "worker_mode": "DISABLED",
        "worker_autostart": "0",
        "validation_flow": "0",
        "validation_movement": "0",
        "processes": {"Xvfb": 498, "steam": 600, "dontstarve_stea": 1237},
        "dst_game_pid": 1237,
        "ready": {"steam": True, "dst": False},
    }
    monkeypatch.setattr("scripts.deploy_runtime.run", lambda *_args: json.dumps(payload))

    assert guest_probe("dst-000001-g1", allow_starting=True)["ready"]["dst"] is False
    with pytest.raises(DeployError, match="readiness markers"):
        guest_probe("dst-000001-g1")


@pytest.mark.parametrize("payload", [
    {"active": True, "agent_pid": 10},
    {"active": False, "agent_pid": 0, "processes": {"steam": 12}},
])
def test_cold_deployment_rejects_live_processes(monkeypatch, payload):
    from scripts.deploy_runtime import cold_probe
    monkeypatch.setattr("scripts.deploy_runtime.run", lambda *_args: json.dumps(payload))
    with pytest.raises(DeployError, match="stopped agent"):
        cold_probe("dst-000002-g2")


def test_cold_deployment_accepts_idle_guest(monkeypatch):
    from scripts.deploy_runtime import cold_probe
    payload = {"active": False, "agent_pid": 0, "processes": {}, "runtime_id": "4", "worker_mode": "DISABLED", "worker_autostart": "0"}
    monkeypatch.setattr("scripts.deploy_runtime.run", lambda *_args: json.dumps(payload))
    assert cold_probe("dst-000002-g2") == payload


def test_cold_installer_upgrades_base_with_missing_world_module(monkeypatch, tmp_path):
    import subprocess
    import sys

    from scripts.deploy_runtime import write_archive
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "guest"
    (root / "runtime_agent").mkdir(parents=True)
    (root / "app/runtime").mkdir(parents=True)
    revision = "a" * 40
    files = runtime_files(repo)
    archive = tmp_path / "runtime.tar.gz"
    write_archive(repo, files, make_metadata(revision, files, repo), archive)
    monkeypatch.setattr(sys, "argv", ["installer", str(archive), str(root), revision, "cold"])
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: "0")
    monkeypatch.setattr(subprocess, "call", lambda *_args, **_kwargs: 1)
    exec(compile(GUEST_INSTALLER, "<installer>", "exec"), {"__name__": "__main__"})  # noqa: S102 - trusted installer under mocked systemctl
    assert (root / "app/runtime/world_profile.py").read_bytes() == (repo / "app/runtime/world_profile.py").read_bytes()
    assert json.loads((root / "DEPLOYMENT.json").read_text())["commit"] == revision
