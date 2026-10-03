from datetime import timedelta

import pytest

from app.models import utcnow
from app.runtime.world_profile import (
    SAFE_PROFILE,
    SAFE_PROFILE_VERSION,
    desired_profile_hash,
    reconcile_idle_timeout,
    reconcile_world_profile,
    render_profile,
)
from app.services.session import safe_world_profile_failure, session_failure


def test_safe_profile_is_deterministic_partial_override():
    rendered = render_profile(SAFE_PROFILE)
    assert rendered == render_profile(dict(reversed(list(SAFE_PROFILE.items()))))
    assert "override_enabled = true" in rendered
    assert 'day = "onlyday"' in rendered
    assert 'hunger = "nonlethal"' in rendered
    assert 'temperaturedamage = "nonlethal"' in rendered
    assert 'wildfires = "never"' in rendered
    assert 'earthquakes = "never"' in rendered
    assert 'meteorshowers = "never"' in rendered
    assert 'spiders = "never"' in rendered
    assert "leveldataoverride" not in rendered
    assert desired_profile_hash() == desired_profile_hash()


def test_reconciliation_creates_repairs_and_noops(tmp_path):
    cluster = tmp_path / "123" / "Cluster_1"
    cluster.mkdir(parents=True)
    config = cluster / "Master" / "worldgenoverride.lua"
    created = reconcile_world_profile(user_root=tmp_path)
    assert created["changed"] is True
    assert config.read_text() == render_profile()
    before = config.stat().st_mtime_ns
    unchanged = reconcile_world_profile(user_root=tmp_path)
    assert unchanged["changed"] is False
    assert config.stat().st_mtime_ns == before
    config.write_text("return { override_enabled = false }\n")
    repaired = reconcile_world_profile(user_root=tmp_path)
    assert repaired["changed"] is True
    assert config.read_text() == render_profile()


def test_profile_reconciliation_rejects_ambiguous_worlds(tmp_path):
    (tmp_path / "123" / "Cluster_1").mkdir(parents=True)
    (tmp_path / "456" / "Cluster_1").mkdir(parents=True)
    with pytest.raises(RuntimeError, match="exactly one"):
        reconcile_world_profile(user_root=tmp_path)


def test_generated_new_cluster_gets_zero_network_idle_timeout(tmp_path):
    # Model a freshly generated account/cluster, with no prior config to repair.
    cluster = tmp_path / "new-account" / "Cluster_1"
    cluster.mkdir(parents=True)
    config = cluster / "cluster.ini"
    config.write_text("[SHARD]\nname = Master\n")

    result = reconcile_idle_timeout(user_root=tmp_path)

    assert result["changed"] is True
    assert result["idle_timeout"] == 0
    assert "[NETWORK]\nidle_timeout = 0\n" in config.read_text()


def test_death_process_stale_and_input_fail_closed():
    now = utcnow()
    base = {
        "healthy": True,
        "updated_at": now,
        "state": "WAITING",
        "held_inputs": False,
        "observation": {"screen": "IN_WORLD_IDLE"},
    }
    assert session_failure(base, now) is None
    for screen in ["DEAD", "WORLD_RESET_PENDING"]:
        assert (
            session_failure({**base, "observation": {"screen": screen}}, now)
            == "GAMEPLAY_" + screen
        )
    assert session_failure({**base, "healthy": False}, now) == "RUNTIME_UNHEALTHY"
    assert (
        session_failure({**base, "updated_at": now - timedelta(seconds=21)}, now)
        == "RUNTIME_UNHEALTHY"
    )
    assert session_failure({**base, "held_inputs": True}, now) == "INPUT_NOT_RELEASED"


def test_managed_bootstrap_preserves_safe_profile_and_observation_cadence():
    from dataclasses import replace

    from app.runtime.bootstrap_models import RuntimeAgentConfig

    config = RuntimeAgentConfig(
        runtime_id=1,
        account_id=1,
        node_id=1,
        runtime_token="test",
        orchestrator_url="http://test",
        protocol_version=1,
        heartbeat_interval=5,
        safe_idle_world=True,
    )
    assert b'WORKER_OBSERVATION_INTERVAL="12"' in config.environment_file()
    assert b'SAFE_IDLE_WORLD_PROFILE="1"' in config.environment_file()
    assert config.environment_file() == replace(config).environment_file()
    assert (
        b"WORKER_OBSERVATION_INTERVAL"
        not in replace(config, safe_idle_world=False).environment_file()
    )
    assert (
        b'SAFE_IDLE_WORLD_PROFILE="0"'
        in replace(config, safe_idle_world=False).environment_file()
    )


def test_long_session_requires_fresh_current_process_profile_evidence():
    from types import SimpleNamespace

    now = utcnow()
    runtime = SimpleNamespace(id=4, account_id=7, runtime_generation=3)
    evidence = {
        "status": "WORLD_PROFILE_VERIFIED",
        "verification_scope": "PERSISTED_WORLD_SETTINGS_AND_CURRENT_PROCESS",
        "world_profile_verified": True,
        "loaded_world_verified": True,
        "profile_version": SAFE_PROFILE_VERSION,
        "world_session_id": "EA6E12E4296C650B",
        "fixture_manifest_sha256": "a" * 64,
        "settings_fingerprint": desired_profile_hash(),
        "profile_layer": "worldgenoverride.lua",
        "configuration_verified": True,
        "account_id": 7,
        "runtime_id": 4,
        "runtime_generation": 3,
        "world_path": "/home/dst/.klei/DoNotStarveTogether/123/Cluster_1/Master/worldgenoverride.lua",
        "desired_profile_hash": desired_profile_hash(),
        "applied_profile_hash": desired_profile_hash(),
        "process_id": 314,
        "process_start_ticks": 880,
        "process_generation": "r4-g3-p314-t880",
        "process_started_at": (now - timedelta(seconds=10)).isoformat(),
        "verified_at": now.isoformat(),
    }
    snapshot = {"dst_running": True, "world_profile": evidence}
    assert safe_world_profile_failure(snapshot, runtime, now) is None
    assert (
        safe_world_profile_failure(snapshot, runtime, now, require_loaded=True) is None
    )
    for change in [
        {"status": "CONFIG_PRESENT"},
        {"verification_scope": "CONFIG_FILE_AND_PROCESS"},
        {"world_profile_verified": False},
        {"loaded_world_verified": False},
        {"profile_version": 0},
        {"world_session_id": ""},
        {"fixture_manifest_sha256": ""},
        {"settings_fingerprint": "old"},
    ]:
        assert safe_world_profile_failure(
            {**snapshot, "world_profile": {**evidence, **change}},
            runtime,
            now,
            require_loaded=True,
        )
    assert safe_world_profile_failure({"dst_running": True}, runtime, now)
    assert safe_world_profile_failure(
        {**snapshot, "world_profile": {**evidence, "runtime_generation": 2}},
        runtime,
        now,
    )
    assert safe_world_profile_failure(
        {**snapshot, "world_profile": {**evidence, "process_id": 99}},
        runtime,
        now,
    )
    assert safe_world_profile_failure(
        {
            **snapshot,
            "world_profile": {
                **evidence,
                "verified_at": (now - timedelta(seconds=21)).isoformat(),
            },
        },
        runtime,
        now,
    )
