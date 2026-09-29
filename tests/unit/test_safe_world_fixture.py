import json
import shutil

import pytest

from app.runtime.world_profile import (
    FIXTURE_MANIFEST,
    SAFE_PROFILE,
    SAFE_PROFILE_VERSION,
    attest_fixture,
    config_profile_hash,
    loaded_profile,
    render_profile,
    saved_world,
    verified_fixture,
)


def world(
    master,
    *,
    session="EA6E12E4296C650B",
    day="onlyday",
    segments="day=16,dusk=0,night=0",
):
    p = master / "save/session" / session / "0000000003"
    p.parent.mkdir(parents=True, exist_ok=True)
    settings = {**SAFE_PROFILE, "day": day}
    entries = ",".join(f'{k}="{v}"' for k, v in settings.items())
    p.write_text(
        'tablefunctions["map_fn"] = function()\nreturn {topology={overrides={'
        + entries
        + "}}}\nend\n"
        'tablefunctions["meta_fn"] = function()\nreturn {session_identifier="'
        + session
        + '"}\nend\n'
        'tablefunctions["world_network_fn"] = function()\nreturn {persistdata={clock={segs={'
        + segments
        + "}}}}\nend\n"
    )
    return p


def log(session="EA6E12E4296C650B"):
    return f"Loading world: session/{session}/0000000003\n" + "\n".join(
        f"OVERRIDE: setting\t{k}\tto\t{v}" for k, v in SAFE_PROFILE.items()
    )


def test_unsafe_world_with_correct_config_never_verifies(tmp_path):
    world(tmp_path, day="default")
    (tmp_path / "worldgenoverride.lua").write_text(render_profile())
    with pytest.raises((FileNotFoundError, ValueError)):
        verified_fixture(tmp_path)
    with pytest.raises(ValueError, match="settings"):
        attest_fixture(tmp_path, application_log=log())


def test_supported_applied_and_saved_fixture_attestation_is_idempotent(tmp_path):
    world(tmp_path)
    first = attest_fixture(tmp_path, application_log=log())
    assert first["profile_version"] == SAFE_PROFILE_VERSION
    before = (tmp_path / FIXTURE_MANIFEST).read_bytes()
    assert attest_fixture(tmp_path, application_log=log()) == first
    assert (tmp_path / FIXTURE_MANIFEST).read_bytes() == before
    assert verified_fixture(tmp_path)["clock_segments"] == {
        "day": 16,
        "dusk": 0,
        "night": 0,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_version", 0),
        ("desired_profile_hash", "old"),
        ("world_session_id", "OTHER"),
    ],
)
def test_stale_fixture_detected(tmp_path, field, value):
    world(tmp_path)
    manifest = attest_fixture(tmp_path, application_log=log())
    (tmp_path / FIXTURE_MANIFEST).write_text(json.dumps({**manifest, field: value}))
    with pytest.raises(ValueError, match="stale"):
        verified_fixture(tmp_path)
    with pytest.raises(ValueError):
        attest_fixture(tmp_path, application_log=log())


def test_restore_and_restart_keep_world_verification(tmp_path):
    master = tmp_path / "Master"
    world(master)
    attest_fixture(master, application_log=log())
    restored = tmp_path / "Restored"
    shutil.copytree(master, restored)
    assert (
        verified_fixture(restored)["world_session_id"]
        == verified_fixture(master)["world_session_id"]
    )
    assert verified_fixture(restored) == verified_fixture(restored)


def test_other_session_cannot_inherit_fixture(tmp_path):
    world(tmp_path)
    attest_fixture(tmp_path, application_log=log())
    shutil.rmtree(tmp_path / "save")
    world(tmp_path, session="AAAAAAAAAAAAAAAA")
    with pytest.raises(ValueError, match="stale"):
        verified_fixture(tmp_path)
    assert not loaded_profile(log("AAAAAAAAAAAAAAAA"), "EA6E12E4296C650B")


def test_missing_application_and_wrong_clock_fail(tmp_path):
    world(tmp_path)
    with pytest.raises(ValueError, match="markers"):
        attest_fixture(tmp_path, application_log="mouse click succeeded")
    world(tmp_path, segments="day=8,dusk=6,night=2")
    with pytest.raises(ValueError, match="clock"):
        saved_world(tmp_path)


def test_latest_world_transition_does_not_inherit_markers():
    assert loaded_profile(log(), "EA6E12E4296C650B")
    assert not loaded_profile(
        log() + "\nLoading world: session/AAAAAAAAAAAAAAAA/0000000001",
        "EA6E12E4296C650B",
    )
    assert not loaded_profile(
        log().replace("to\tonlyday", "to\tdefault"), "EA6E12E4296C650B"
    )


def test_dst_save_rewrites_extra_settings_without_safe_profile_drift(tmp_path):
    path = tmp_path / "worldgenoverride.lua"
    path.write_text(render_profile())
    baseline = config_profile_hash(path)
    path.write_text(
        render_profile().replace(
            'day = "onlyday",', 'day = "onlyday",\nbeefalo = "default",'
        )
    )
    assert config_profile_hash(path) == baseline
    path.write_text(path.read_text().replace('"onlyday"', '"default"'))
    assert config_profile_hash(path) != baseline


def test_guarded_archive_restore_rejects_unsafe_and_preserves_verified_fixture(
    tmp_path,
):
    from scripts.diagnostics.safe_world_fixture import backup, restore

    master = tmp_path / "Master"
    world(master)
    attest_fixture(master, application_log=log())
    archive = tmp_path / "safe.tar.gz"
    backup(master, archive)
    with pytest.raises(FileExistsError):
        backup(master, archive)
    world(master, day="default")
    restore(master, archive, rollback_path=tmp_path / "rollback.tar.gz")
    assert verified_fixture(master)["settings"] == SAFE_PROFILE
    unsafe = tmp_path / "unsafe.tar.gz"
    world(master, day="default")
    backup(master, unsafe)
    with pytest.raises(ValueError, match="settings"):
        restore(master, unsafe, rollback_path=tmp_path / "another-rollback.tar.gz")
    assert not (tmp_path / "another-rollback.tar.gz").exists()


def test_process_evidence_requires_fixture_and_current_loaded_session(tmp_path):
    from types import SimpleNamespace

    from app.runtime.world_profile import (
        reconcile_world_profile,
        record_process_evidence,
    )
    from runtime_agent.processes.dst import DSTProcess

    user = tmp_path / "klei"
    master = user / "123/Cluster_1/Master"
    world(master)
    result = reconcile_world_profile(user_root=user)
    path = tmp_path / "evidence.json"
    record_process_evidence(
        reconciliation=result,
        account_id=2,
        runtime_id=1,
        runtime_generation=3,
        process_id=314,
        process_start_ticks=880,
        process_started_at="2026-09-29T21:00:00+00:00",
        evidence_path=path,
    )
    ready = tmp_path / "ready"
    ready.touch()
    supervisor = SimpleNamespace(
        before_start=None,
        alive=True,
        status=SimpleNamespace(pid=314, started_at="2026-09-29T21:00:00+00:00"),
        process_identity=lambda _: (880, 314, 314),
    )
    process = DSTProcess(
        supervisor,
        ready,
        account_id=2,
        runtime_id=1,
        runtime_generation=3,
        safe_idle_world_profile=True,
        user_root=user,
        evidence_path=path,
    )
    assert process.world_profile_evidence()["status"] == "CONFIG_PRESENT"
    attest_fixture(master, application_log=log())
    logfile = user / "client_log.txt"
    logfile.write_text("Current time: Tue Sep 29 20:59:00 2026\n" + log())
    evidence = process.world_profile_evidence()
    assert evidence["status"] == "WORLD_PROFILE_VERIFIED"
    assert evidence["loaded_world_verified"] is False
    logfile.write_text("Current time: Tue Sep 29 21:00:01 2026\n" + log())
    assert process.world_profile_evidence()["loaded_world_verified"] is True
    assert json.loads(path.read_text())["world_session_id"] == "EA6E12E4296C650B"
    logfile.write_text(
        "Current time: Tue Sep 29 21:00:01 2026\n" + log("AAAAAAAAAAAAAAAA")
    )
    assert process.world_profile_evidence()["loaded_world_verified"] is False
    supervisor.status.pid = 315
    assert process.world_profile_evidence()["status"] == "UNVERIFIED"
