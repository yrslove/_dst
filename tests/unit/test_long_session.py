from datetime import timedelta

import pytest

from app.models import utcnow
from app.runtime.world_profile import SAFE_PROFILE, application_script, render_profile
from app.services.session import session_failure


def test_safe_profile_is_idempotent_preserving_prepared_metadata():
    source = (
        'return {id="SURVIVAL_TOGETHER", name="Survival", overrides={\n'
        + ",\n".join(f'{k}="default"' for k in SAFE_PROFILE)
        + "}}"
    )
    rendered = render_profile(source, SAFE_PROFILE)
    assert 'id="SURVIVAL_TOGETHER"' in rendered
    assert 'day="onlyday"' in rendered
    assert 'hunger="nonlethal"' in rendered
    assert render_profile(rendered, SAFE_PROFILE) == rendered
    assert "dontstarve" in application_script()
    with pytest.raises(ValueError):
        render_profile("return {}", SAFE_PROFILE)


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
    assert config.environment_file() == replace(config).environment_file()
    assert (
        b"WORKER_OBSERVATION_INTERVAL"
        not in replace(config, safe_idle_world=False).environment_file()
    )
