from types import SimpleNamespace

import pytest

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.locomotion import Locomotion
from runtime_agent.gameworker.vision import DSTScreen


def observation(sequence, at, screen=DSTScreen.IN_WORLD_IDLE, fresh=True):
    return SimpleNamespace(source_frame_id=f"frame-{sequence}", observed_monotonic=at,
                           production_ready=True, is_fresh=lambda: fresh, screen=screen)


def test_invalid_world_and_heartbeat_gaps_do_not_accumulate_active_time():
    run = Locomotion()
    run.configure("CONTROL", "account1-test", 120)
    run.observe(observation(1, 0), True)
    run.observe(observation(2, 2), True)
    run.observe(observation(3, 4, DSTScreen.LOADING), True)
    run.observe(observation(4, 60), True)
    run.observe(observation(5, 62), True)
    run.observe(observation(6, 100), True)
    assert run.telemetry()["active_elapsed"] == 4
    assert run.proposal(observation(7, 101, DSTScreen.DEAD)) is None
    assert run.proposal(observation(8, 102, fresh=False)) is None
    run.suspend()
    run.observe(observation(9, 150), True)
    assert run.telemetry()["active_elapsed"] == 4


def test_high_activity_is_measurably_more_active(monkeypatch):
    from runtime_agent.gameworker import locomotion
    counters = {}
    for profile in ("CONTROL", "HIGH_ACTIVITY"):
        now = [100.0]
        monkeypatch.setattr(locomotion.time, "monotonic", lambda now=now: now[0])
        run = Locomotion()
        run.configure(profile, profile, 120)
        sequence = 0
        while now[0] < 220:
            sequence += 1
            obs = observation(sequence, now[0])
            run.observe(obs, True)
            proposal = run.proposal(obs)
            if proposal:
                action, duration = proposal
                sent = ActionResult(str(sequence), action, ActionStatus.VERIFYING, duration, 1, 1, 1)
                run.sent(sent)
                now[0] += duration
                run.verified(ActionResult(str(sequence), action, ActionStatus.SUCCEEDED, duration, 1, 1, 1))
            now[0] += 1
        counters[profile] = run.telemetry()
    assert counters["HIGH_ACTIVITY"]["moving_seconds"] > 5 * counters["CONTROL"]["moving_seconds"]
    assert counters["HIGH_ACTIVITY"]["movement_commands"] > 3 * counters["CONTROL"]["movement_commands"]
    assert counters["HIGH_ACTIVITY"]["direction_changes"] > counters["CONTROL"]["direction_changes"]


def test_unverified_action_never_counts_moving_time_or_blocks_next_pulse():
    run = Locomotion()
    run.configure("CONTROL", "safe", 120)
    result = ActionResult("1", ActionName.MOVE_FORWARD, ActionStatus.VERIFYING, .4, 1, 1, 1)
    run.sent(result)
    assert run.proposal(observation(1, 1)) is None
    run.verified(ActionResult("1", ActionName.MOVE_FORWARD, ActionStatus.TIMED_OUT, .4, 1, 1, 1))
    assert run.telemetry()["movement_commands"] == 1
    assert run.telemetry()["moving_seconds"] == 0
    assert not run.pending


def test_characterization_stops_at_valid_target_or_wall_safety(monkeypatch):
    from runtime_agent.gameworker import locomotion

    now = [100.0]
    monkeypatch.setattr(locomotion.time, "monotonic", lambda: now[0])
    run = Locomotion()
    run.configure("CONTROL", "characterization", 16 * 3600, target_valid_seconds=14 * 3600)
    assert run.stop_reason is None
    run.valid_elapsed = 14 * 3600
    assert run.stop_reason == "TARGET_VALID_ONLINE_REACHED"
    run.valid_elapsed = 0
    now[0] += 16 * 3600
    assert run.stop_reason == "MAX_WALL_CLOCK_REACHED"


@pytest.mark.parametrize("session,seconds", [("../other", 120), ("ok", 0), ("ok", float("nan"))])
def test_session_path_and_duration_are_bounded(session, seconds):
    with pytest.raises(ValueError):
        Locomotion().configure("HIGH_ACTIVITY", session, seconds)


def test_profile_survives_canonical_enable_and_runtime_loss_resumes_only_its_owner(monkeypatch, tmp_path):
    from runtime_agent.gameworker.base import WorkerContext
    from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
    from runtime_agent.gameworker.dst.worker import DSTGameWorker
    from runtime_agent.gameworker.state import WorkerState
    worker = DSTGameWorker(WorkerConfig(mode=WorkerMode.DISABLED, diagnostic_directory=tmp_path))
    context = WorkerContext(2, 4, runtime_verified=True)
    worker.prepare(context)
    worker.configure_experiment("HIGH_ACTIVITY", "isolated", 120)
    def prepare(context):
        worker.context = context
        worker.capture = object()
        worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test capture prepared")
        return worker.status()
    monkeypatch.setattr(worker, "prepare", prepare)
    worker._game_ready = True
    worker.set_mode(WorkerMode.ACTIVE)
    assert worker.activity.locomotion is worker.locomotion
    assert worker.activity.locomotion.profile == "HIGH_ACTIVITY"
    worker.on_game_lost()
    assert worker.machine.state == WorkerState.PAUSED
    worker.on_game_ready(context)
    assert worker.machine.state == WorkerState.OBSERVING
    worker.pause()
    worker.on_game_lost()
    worker.on_game_ready(context)
    assert worker.machine.state == WorkerState.PAUSED
