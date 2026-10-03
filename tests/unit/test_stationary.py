from dataclasses import replace
from types import SimpleNamespace

import pytest
from PIL import Image

from app.runtime.world_profile import reconcile_idle_timeout
from runtime_agent.gameworker.actions import ActionName
from runtime_agent.gameworker.activity import ActivityController
from runtime_agent.gameworker.locomotion import Locomotion
from runtime_agent.gameworker.stationary import StationarySession
from runtime_agent.gameworker.vision import DSTScreen
from tests.unit.test_dst_behavior import ASSETS, analyze_image


@pytest.mark.parametrize("profile", ["CONTROL", "HIGH_ACTIVITY"])
def test_stationary_production_never_calls_periodic_locomotion(profile, monkeypatch):
    locomotion = Locomotion()
    locomotion.configure(profile, "stationary-no-input", 8 * 3600)
    locomotion.click_anchor = (640, 400)
    locomotion.click_clearance_pending = True

    def forbidden(_observation):
        pytest.fail("periodic locomotion was invoked")

    monkeypatch.setattr(locomotion, "proposal", forbidden)
    policy = ActivityController(locomotion=locomotion)
    policy.set_production_actions_enabled(True)
    world = analyze_image(
        Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB"),
        "stationary-world", 1,
    )
    for sequence in range(1, 100):
        assert policy.propose(replace(world, source_sequence=sequence)) is None
    assert locomotion.movement_commands == 0


def test_reveal_dwell_starts_on_recovered_popup_and_does_not_close_early():
    received = analyze_image(
        Image.open(ASSETS / "samples/inworld_gift_received_live.png").convert("RGB"),
        "stationary-reveal", 1, profile_id="dst-1280x720-linux-v1",
    )
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(received) is None
    assert policy.propose(replace(received, source_sequence=2)) is None
    started = policy._gift_clicked_monotonic
    assert started is not None
    policy._gift_clicked_monotonic = received.observed_monotonic - 9.99
    assert policy.propose(replace(received, source_sequence=3)) is None
    policy._gift_clicked_monotonic = received.observed_monotonic - 10
    assert policy.propose(replace(received, source_sequence=4)).action == ActionName.CLICK_INWORLD_USE_LATER


def test_stationary_profile_preserves_existing_session_join():
    locomotion = Locomotion()
    locomotion.configure("CONTROL", "stationary-join", 1800)
    policy = ActivityController(locomotion=locomotion)
    policy.set_production_actions_enabled(True)
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "stationary-menu", 1, profile_id="dst-1280x720-linux-v1",
    )
    assert policy.propose(menu) is None
    proposal = policy.propose(replace(menu, source_sequence=2))
    assert proposal.action == ActionName.CLICK_HOST_GAME
    assert locomotion.movement_commands == 0


def observation(sequence, now, availability=None, displacement=(0, 0), screen=DSTScreen.IN_WORLD_IDLE):
    icon = SimpleNamespace(kind="gift_icon", detected=True, verified=True,
                           confidence=.99, metadata=(("availability", availability),))
    return SimpleNamespace(source_frame_id=f"frame-{sequence}",
                           timestamp=str(now), observed_monotonic=now,
                           production_ready=True, is_fresh=lambda: True,
                           screen=screen, local_displacement=displacement,
                           detections=[icon] if availability else [])


def test_stationary_wait_uses_operator_precondition_without_anchor_calibration():
    session = StationarySession()
    session.observe(observation(1, 100))
    assert session.state == "STATIONARY_WAIT"
    assert session.wait_started_at == "100"
    assert session.telemetry()["station_position"] == "OPERATOR_PRECONDITION"
    assert session.movement_count == 0
    session.observe(observation(2, 110, "IN_WORLD_GIFT_PENDING", displacement=(80, 0)))
    assert session.state == "GIFT_DETECTED"
    assert not session.telemetry()["periodic_movement_enabled"]
    assert not any("ANCHOR" in e["event"] for e in session.events)
    session.observe(observation(3, 120, screen=DSTScreen.DEAD))
    assert session.state == "RECOVERY_BLOCKER"
    session.observe(observation(4, 130))
    assert session.state == "RECOVERY_BLOCKER"
    assert session.wait_started_at is None


def test_stationary_executor_whitelist_excludes_all_gameplay_movement():
    from runtime_agent.gameworker.config import WorkerConfig
    from runtime_agent.gameworker.dst.worker import DSTGameWorker
    worker = DSTGameWorker(WorkerConfig())
    worker.locomotion.configure("CONTROL", "stationary-whitelist", 120)
    forbidden = {ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD,
                 ActionName.TURN_LEFT, ActionName.TURN_RIGHT,
                 ActionName.CLICK_LOCAL_TARGET, ActionName.INTERACT}
    assert not worker._permitted_active_actions() & forbidden
    assert ActionName.CLICK_GIFT_ICON in worker._permitted_active_actions()
    assert ActionName.CLICK_INWORLD_USE_LATER in worker._permitted_active_actions()


def test_stationary_events_preserve_next_gift_intervals():
    session = StationarySession()
    session.observe(observation(1, 100, "GIFT_AVAILABLE"))
    session.cleared({"item_id": 7}, "120", 120)
    session.observe(observation(2, 480, "IN_WORLD_GIFT_PENDING"))
    event = next(e for e in reversed(session.events) if e["event"] == "GIFT_FIRST_DETECTED")
    assert event["stationary_wait_to_gift_seconds"] == 360
    assert event["previous_gift_to_next_seconds"] == 380
    assert session.previous_gift_at == "100"


@pytest.mark.parametrize("original", [
    b"[GAMEPLAY]\ngame_mode = endless\n[NETWORK]\nmax_players = 1\nidle_timeout = 1800\n[MISC]\nconsole_enabled = true\n",
    b"[NETWORK]\r\nmax_players = 1\r\n[MISC]\r\nconsole_enabled = true\r\n",
    b"[GAMEPLAY]\ngame_mode = endless",
])
def test_idle_timeout_preserves_unrelated_configuration_and_is_idempotent(tmp_path, original):
    path = tmp_path / "account/Cluster_1/cluster.ini"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    result = reconcile_idle_timeout(user_root=tmp_path)
    assert result["idle_timeout"] == 0
    assert not result["loaded_server_verified"]
    actual = path.read_bytes()
    assert b"idle_timeout = 0" in actual
    for line in original.splitlines():
        if not line.startswith(b"idle_timeout"):
            assert line in actual.splitlines()
    assert not reconcile_idle_timeout(user_root=tmp_path)["changed"]
    assert path.read_bytes() == actual


def test_idle_timeout_rejects_ambiguous_configuration(tmp_path):
    path = tmp_path / "account/Cluster_1/cluster.ini"
    path.parent.mkdir(parents=True)
    original = b"[NETWORK]\nidle_timeout = 1\nidle_timeout = 2\n"
    path.write_bytes(original)
    with pytest.raises(ValueError):
        reconcile_idle_timeout(user_root=tmp_path)
    assert path.read_bytes() == original


@pytest.mark.parametrize('name', [ActionName.MOVE_FORWARD, ActionName.MOVE_BACKWARD,
                                ActionName.TURN_LEFT, ActionName.TURN_RIGHT,
                                ActionName.CLICK_LOCAL_TARGET, ActionName.INTERACT])
def test_stationary_executor_rejects_movement_without_any_driver_event(name):
    from runtime_agent.gameworker.actions import ActionStatus
    from runtime_agent.gameworker.config import WorkerConfig
    from runtime_agent.gameworker.dst.worker import DSTGameWorker
    from tests.unit.test_gameworker_actions import action, executor
    worker = DSTGameWorker(WorkerConfig())
    worker.locomotion.configure('CONTROL', 'stationary-input-gate', 120)
    value, controller, driver, _deadman = executor(allowed_actions=worker._permitted_active_actions())
    try:
        result = value.execute(action('forbidden-stationary', name, duration=.1))
        assert result.status == ActionStatus.REJECTED
        assert result.reason == 'action not whitelisted'
        assert driver.events == []
        assert not controller.has_held_inputs
    finally:
        value.shutdown()
