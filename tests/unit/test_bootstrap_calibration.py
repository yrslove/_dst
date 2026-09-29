from app.runtime.bootstrap_models import RuntimeAgentConfig


def test_bootstrap_preserves_verified_worker_calibration():
    config = RuntimeAgentConfig(
        runtime_id=1,
        account_id=1,
        node_id=1,
        runtime_token="test-token",
        orchestrator_url="http://control.example",
        protocol_version=1,
        heartbeat_interval=5,
        worker_calibration_profile="dst-1280x720-linux-v1",
        worker_calibration_verified=True,
    )
    rendered = config.environment_file().decode()
    assert 'WORKER_CALIBRATION_PROFILE="dst-1280x720-linux-v1"' in rendered
    assert 'WORKER_CALIBRATION_VERIFIED="1"' in rendered
