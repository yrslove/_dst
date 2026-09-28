from __future__ import annotations

import hashlib
import json
import random
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.actions import (
    Action,
    ActionName,
    ActionResult,
    ActionStatus,
)
from runtime_agent.gameworker.activity import ActionProposal, FakePlanner
from runtime_agent.gameworker.base import WorkerContext
from runtime_agent.gameworker.capture import CaptureError, FakeCaptureSource, Frame
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.geometry import CalibrationProfile
from runtime_agent.gameworker.perception import ObservePipeline
from runtime_agent.gameworker.recording import (
    RecordingEventType,
    RecordingLimits,
    SessionRecorder,
)
from runtime_agent.gameworker.replay import (
    ReplayActionSink,
    ReplayCaptureSource,
    ReplayClock,
    ReplayError,
    ReplayRunner,
    ReplayTimingMode,
)
from runtime_agent.gameworker.vision import (
    Detection,
    DSTScreen,
    GameObservation,
    ObservationValidity,
)


def frame(sequence: int = 1, *, generation: int = 1, captured: float | None = None):
    return Frame(
        frame_id=f"frame-{sequence}",
        sequence=sequence,
        captured_at=f"2026-01-01T00:00:0{sequence}+00:00",
        captured_monotonic=time.monotonic() if captured is None else captured,
        runtime_id=7,
        runtime_generation=generation,
        worker_generation=3,
        width=2,
        height=2,
        pixels=bytes([sequence % 256]) * 12,
    )


def recorder(tmp_path: Path, *, limits: RecordingLimits | None = None):
    return SessionRecorder(
        tmp_path.resolve(),
        runtime_instance_id="runtime-7-g1",
        runtime_id=7,
        runtime_generation=1,
        worker_generation=3,
        worker_mode="OBSERVE",
        capture_source={"kind": "fake"},
        calibration={"profile_id": "test", "version": 1},
        perception={"engine": "FakeEngine", "assets_verified": True},
        limits=limits
        or RecordingLimits(
            max_duration_seconds=60,
            max_frames=10,
            max_bytes=1024 * 1024,
            queue_size=8,
            frame_interval_seconds=0,
            shutdown_timeout_seconds=1,
        ),
    )


def recorded_session(tmp_path: Path, count: int = 2) -> Path:
    value = recorder(tmp_path)
    assert value.record_event(RecordingEventType.GAME_READY)
    start = time.monotonic()
    for sequence in range(1, count + 1):
        assert value.record_frame(frame(sequence, captured=start + sequence))
    assert value.close()
    return value.path


def read_manifest(path: Path) -> dict:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def read_events(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def write_events(path: Path, events: list[dict]) -> None:
    (path / "events.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in events),
        encoding="utf-8",
    )


class DeterministicEngine:
    def analyze(
        self,
        source: Frame,
        *,
        calibration,
        observation_generation: int,
        max_frame_age: float,
        deadline: float,
    ) -> GameObservation:
        detection = Detection("synthetic", True, 1.0, detector_id="test", verified=True)
        return GameObservation(
            timestamp=source.captured_at,
            observed_monotonic=source.captured_monotonic,
            observation_generation=observation_generation,
            source_frame_id=source.frame_id,
            source_sequence=source.sequence,
            source_captured_monotonic=source.captured_monotonic,
            runtime_id=source.runtime_id,
            runtime_generation=source.runtime_generation,
            worker_generation=source.worker_generation,
            validity=ObservationValidity.VALID,
            fresh_until=source.captured_monotonic + max_frame_age,
            game_visible=detection,
            menu_visible=detection,
            player_visible=detection,
            interaction_prompt_visible=detection,
            worker_confidence=1.0,
            detections=(detection,),
            calibration_profile_id=calibration.profile_id,
            calibration_version=calibration.version,
            calibration_verified=True,
            assets_verified=True,
            screen_hash=hashlib.sha256(source.pixels).hexdigest(),
            screen_change=0.0,
            perception_latency=0.0,
        )


def runner(path: Path) -> ReplayRunner:
    source = ReplayCaptureSource(path)
    return ReplayRunner(
        source,
        DeterministicEngine(),
        FakePlanner(ActionProposal(ActionName.INTERACT, reason="synthetic")),
        CalibrationProfile("test", 1, 2, 2, verified=True),
        max_frame_age=5,
        max_observation_age=5,
        perception_timeout=1,
        planner_timeout=1,
    )


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_recording_writes_versioned_manifest_jsonl_and_external_png(tmp_path):
    value = recorder(tmp_path)
    assert read_manifest(value.path)["completion_status"] == "INTERRUPTED"
    assert value.record_frame(frame())
    assert value.marker("opened inventory", "synthetic note")

    assert value.close()

    manifest = read_manifest(value.path)
    events = read_events(value.path)
    frame_event = next(
        item for item in events if item["event_type"] == "FRAME_CAPTURED"
    )
    assert manifest["format_version"] == 1
    assert manifest["completion_status"] == "COMPLETE"
    assert manifest["counters"]["recorded_frames"] == 1
    assert (value.path / frame_event["payload"]["image_file"]).is_file()
    assert "pixels" not in json.dumps(events)


def test_recording_rejects_generation_mixing_and_sensitive_payload(tmp_path):
    value = recorder(tmp_path)
    assert not value.record_frame(frame(generation=2))
    assert not value.record_event(
        RecordingEventType.USER_MARKER, payload={"runtime_token": "not-written"}
    )
    assert value.close()
    artifact = (value.path / "events.jsonl").read_text(encoding="utf-8")
    assert "not-written" not in artifact


def test_observation_proposal_and_action_result_are_structured_events(tmp_path):
    value = recorder(tmp_path)
    source = frame()
    assert value.record_frame(source)
    observation = DeterministicEngine().analyze(
        source,
        calibration=CalibrationProfile("test", 1, 2, 2, verified=True),
        observation_generation=1,
        max_frame_age=5,
        deadline=time.monotonic() + 1,
    )
    proposal = ActionProposal(ActionName.INTERACT, reason="synthetic")
    action = Action("action-1", ActionName.INTERACT, 1, 3, runtime_id=7)
    result = ActionResult(
        "action-1",
        ActionName.INTERACT,
        ActionStatus.SUPPRESSED,
        0.0,
        1,
        3,
        runtime_id=7,
        reason="OBSERVE",
    )
    assert value.record_observation(observation)
    assert value.record_proposal(
        proposal, frame_id=source.frame_id, observation_id="observation-1"
    )
    assert value.record_action(action, result)
    assert value.close()

    events = read_events(value.path)
    kinds = {item["event_type"] for item in events}
    assert {
        "OBSERVATION_PRODUCED",
        "PLANNER_PROPOSAL",
        "ACTION_RESULT",
    } <= kinds
    observation_event = next(
        item for item in events if item["event_type"] == "OBSERVATION_PRODUCED"
    )
    assert observation_event["payload"]["origin"] == "recorded"
    assert observation_event["payload"]["observation"]["validity"] == "VALID"
    replay = ReplayCaptureSource(value.path)
    expected = replay.recorded_observation(source.frame_id)
    assert expected["screen_hash"] == observation.screen_hash
    assert expected["source_frame_id"] == source.frame_id
    assert len(list(replay.iter_recorded_events())) == len(events)


def test_existing_observe_pipeline_records_the_complete_causal_chain(tmp_path):
    value = recorder(tmp_path)
    captured = frame(captured=time.monotonic() + 1)
    actions = ReplayActionSink(runtime_id=7, runtime_generation=1, worker_generation=3)
    pipeline = ObservePipeline(
        FakeCaptureSource([captured], runtime_generation=1, worker_generation=3),
        DeterministicEngine(),
        FakePlanner(ActionProposal(ActionName.INTERACT)),
        actions,
        CalibrationProfile("test", 1, 2, 2, verified=True),
        runtime_generation=1,
        worker_generation=3,
        max_frame_age=5,
        max_observation_age=5,
        perception_timeout=1,
        planner_timeout=1,
        recorder=value,
    )
    pipeline.on_game_ready()

    outcome = pipeline.tick()
    pipeline.close()
    assert value.close()

    assert outcome.status == "ACTION_RESULT"
    kinds = [item["event_type"] for item in read_events(value.path)]
    assert kinds.index("FRAME_CAPTURED") < kinds.index("OBSERVATION_PRODUCED")
    assert kinds.index("OBSERVATION_PRODUCED") < kinds.index("PLANNER_PROPOSAL")
    assert kinds.index("PLANNER_PROPOSAL") < kinds.index("ACTION_RESULT")


def test_recording_includes_action_transition_timeout_and_last_observation(tmp_path):
    value = recorder(tmp_path)
    clock = [time.monotonic()]
    captured = frame(captured=clock[0] + 1)
    pipeline = ObservePipeline(
        FakeCaptureSource([captured], runtime_generation=1, worker_generation=3),
        DeterministicEngine(),
        FakePlanner(None),
        ReplayActionSink(runtime_id=7, runtime_generation=1, worker_generation=3),
        CalibrationProfile("test", 1, 2, 2, verified=True),
        runtime_generation=1,
        worker_generation=3,
        max_frame_age=5,
        max_observation_age=5,
        perception_timeout=1,
        planner_timeout=1,
        clock=lambda: clock[0],
        recorder=value,
    )
    pipeline.on_game_ready()
    observed = pipeline.tick().observation
    assert observed is not None
    observed = replace(observed, screen=DSTScreen.MAIN_MENU, screen_confidence=1.0)
    pending = pipeline.action_lifecycle.begin(
        ActionResult(
            "click-timeout",
            ActionName.CLICK_HOST_GAME,
            ActionStatus.SENT,
            0.5,
            1,
            3,
            7,
        ),
        observed,
    )
    assert pending.status == ActionStatus.VERIFYING

    clock[0] += 46
    failed = pipeline.tick()
    pipeline.close()
    assert value.close()

    assert failed.status == "ACTION_FAILED"
    assert failed.action_result is not None
    assert failed.action_result.status == ActionStatus.TIMED_OUT
    timeout_event = next(
        event
        for event in read_events(value.path)
        if event["event_type"] == "ACTION_RESULT"
        and event["payload"]["result"]["status"] == "TIMED_OUT"
    )
    assert timeout_event["frame_id"] == observed.source_frame_id
    assert timeout_event["payload"]["result"]["reason"]


def test_recording_bounds_finish_as_truncated_without_unbounded_queue(tmp_path):
    limits = replace(
        RecordingLimits(),
        max_duration_seconds=60,
        max_frames=1,
        max_bytes=1024 * 1024,
        queue_size=1,
        frame_interval_seconds=0,
        shutdown_timeout_seconds=1,
    )
    value = recorder(tmp_path, limits=limits)
    assert wait_until(lambda: value.snapshot()["queue_depth"] == 0)
    start = time.monotonic()
    assert value.record_frame(frame(1, captured=start))
    assert not value.record_frame(frame(2, captured=start + 1))
    assert value.snapshot()["queue_capacity"] == 1
    assert value.close()
    assert read_manifest(value.path)["completion_status"] == "TRUNCATED"


def test_recording_queue_pressure_drops_frames_without_blocking(tmp_path, monkeypatch):
    limits = replace(
        RecordingLimits(),
        max_duration_seconds=60,
        max_frames=10,
        max_bytes=1024 * 1024,
        queue_size=1,
        frame_interval_seconds=0,
        shutdown_timeout_seconds=1,
    )
    value = recorder(tmp_path, limits=limits)
    assert wait_until(lambda: value.snapshot()["queue_depth"] == 0)
    entered = threading.Event()
    release = threading.Event()
    original = value._write_frame

    def blocked_write(*args):
        entered.set()
        assert release.wait(1)
        return original(*args)

    monkeypatch.setattr(value, "_write_frame", blocked_write)
    start = time.monotonic()
    assert value.record_frame(frame(1, captured=start))
    assert entered.wait(1)
    assert value.record_frame(frame(2, captured=start + 1))
    before = time.monotonic()
    assert not value.record_frame(frame(3, captured=start + 2))
    assert time.monotonic() - before < 0.1
    assert value.snapshot()["queue_depth"] <= 1
    assert value.snapshot()["dropped_recording_frames"] >= 1
    release.set()
    assert value.close()
    assert value.snapshot()["buffered_frame_bytes"] == 0


def test_recording_storage_limit_drops_payload_and_truncates(tmp_path):
    limits = RecordingLimits(
        max_duration_seconds=60,
        max_frames=10,
        max_bytes=1024 * 1024,
        queue_size=2,
        frame_interval_seconds=0,
        shutdown_timeout_seconds=2,
    )
    value = recorder(tmp_path, limits=limits)
    pixels = random.Random(4).randbytes(700 * 600 * 3)
    large = Frame(
        "large-frame",
        1,
        "2026-01-01T00:00:00+00:00",
        time.monotonic(),
        7,
        1,
        3,
        700,
        600,
        pixels,
    )

    assert value.record_frame(large)
    assert value.close()

    manifest = read_manifest(value.path)
    assert manifest["completion_status"] == "TRUNCATED"
    assert manifest["counters"]["recorded_frames"] == 0
    assert manifest["counters"]["dropped_recording_frames"] >= 1


def test_recorder_shutdown_timeout_leaves_session_interrupted(tmp_path, monkeypatch):
    value = recorder(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = value._write_frame

    def blocked_write(*args):
        entered.set()
        release.wait(1)
        return original(*args)

    monkeypatch.setattr(value, "_write_frame", blocked_write)
    assert value.record_frame(frame())
    assert entered.wait(1)

    assert not value.close(timeout=0.02)
    assert value.snapshot()["completion_status"] == "INTERRUPTED"
    release.set()
    assert wait_until(lambda: not value.snapshot()["writer_alive"])
    assert read_manifest(value.path)["completion_status"] == "INTERRUPTED"


def test_recording_ids_do_not_collide(tmp_path):
    first = recorder(tmp_path)
    second = recorder(tmp_path)
    assert first.path != second.path
    assert first.close()
    assert second.close()


def test_disk_failure_isolated_and_manifest_failed(tmp_path, monkeypatch):
    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(SessionRecorder, "_write_frame", fail_write)
    value = recorder(tmp_path)
    assert value.record_frame(frame())
    assert wait_until(lambda: value.snapshot()["write_failures"] == 1)
    assert not value.close()
    snapshot = value.snapshot()
    assert snapshot["state"] == "FAILED"
    assert snapshot["queue_depth"] == 0
    assert read_manifest(value.path)["completion_status"] == "FAILED"


def test_recorder_disk_failure_does_not_break_perception_pipeline(
    tmp_path, monkeypatch
):
    value = recorder(tmp_path)

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(value, "_write_frame", fail_write)
    captured = frame(captured=time.monotonic() + 1)
    pipeline = ObservePipeline(
        FakeCaptureSource([captured], runtime_generation=1, worker_generation=3),
        DeterministicEngine(),
        FakePlanner(ActionProposal(ActionName.INTERACT)),
        ReplayActionSink(runtime_id=7, runtime_generation=1, worker_generation=3),
        CalibrationProfile("test", 1, 2, 2, verified=True),
        runtime_generation=1,
        worker_generation=3,
        max_frame_age=5,
        max_observation_age=5,
        perception_timeout=1,
        planner_timeout=1,
        recorder=value,
    )
    pipeline.on_game_ready()

    assert pipeline.tick().status == "ACTION_RESULT"
    assert wait_until(lambda: value.snapshot()["state"] == "FAILED")
    pipeline.close()
    assert not value.close()


def test_replay_restores_frame_contract_and_uses_explicit_generation(tmp_path):
    path = recorded_session(tmp_path, 1)
    source = ReplayCaptureSource(
        path, runtime_id=9, runtime_generation=11, worker_generation=12
    )

    replayed = source.capture()

    assert replayed.frame_id == "frame-1"
    assert replayed.sequence == 1
    assert (replayed.width, replayed.height) == (2, 2)
    assert replayed.runtime_id == 9
    assert replayed.runtime_generation == 11
    assert replayed.worker_generation == 12
    assert replayed.source == "replay"
    assert dict(replayed.metadata)["recorded_worker_generation"] == 3


def test_record_to_replay_uses_same_pipeline_and_never_reaches_input(tmp_path):
    replay = runner(recorded_session(tmp_path))

    outcomes = replay.run()

    assert [item.status for item in outcomes] == ["ACTION_RESULT", "ACTION_RESULT"]
    assert all(
        item.action_result.status == ActionStatus.SUPPRESSED for item in outcomes
    )
    assert replay.actions.suppressed_total == 2
    assert not hasattr(replay.actions, "controller")
    assert replay.lifecycle_events[0]["event_type"] == "REPLAY_STARTED"
    assert replay.lifecycle_events[-1]["event_type"] == "REPLAY_FINISHED"
    replay.close()


def test_fast_replay_is_deterministic_and_does_not_sleep(tmp_path):
    path = recorded_session(tmp_path)
    first = runner(path)
    second = runner(path)

    def result(value):
        return [
            (
                item.status,
                item.frame_id,
                item.observation.screen_hash,
                item.proposal.action,
                item.action_result.status,
            )
            for item in value.run()
        ]

    assert result(first) == result(second)
    called = []
    clock = ReplayClock(
        ReplayTimingMode.AS_FAST_AS_POSSIBLE, sleeper=lambda delay: called.append(delay)
    )
    clock.advance(600)
    assert called == []


def test_recorded_timing_and_step_clock_have_explicit_policies():
    slept = []
    recorded = ReplayClock(
        ReplayTimingMode.RECORDED_TIMING, sleeper=lambda delay: slept.append(delay)
    )
    recorded.advance(10)
    recorded.advance(12.5)
    assert slept == [10.0, 2.5]

    stepped = ReplayClock(ReplayTimingMode.STEP)
    stepped.step()
    stepped.advance(30)
    assert stepped() == 30


def test_step_replay_cancels_between_frames_without_dangling_wait(tmp_path):
    path = recorded_session(tmp_path, 1)
    clock = ReplayClock(ReplayTimingMode.STEP)
    source = ReplayCaptureSource(path, clock=clock)
    failures = []

    def capture():
        try:
            source.capture()
        except CaptureError as exc:
            failures.append(exc)

    thread = threading.Thread(target=capture)
    thread.start()
    assert wait_until(thread.is_alive)
    source.close()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert failures


def test_replay_shutdown_during_perception_discards_inflight_result(tmp_path):
    path = recorded_session(tmp_path, 1)
    entered = threading.Event()
    release = threading.Event()

    class BlockingEngine(DeterministicEngine):
        def analyze(self, source, **kwargs):
            entered.set()
            assert release.wait(1)
            return super().analyze(source, **kwargs)

    source = ReplayCaptureSource(path)
    replay = ReplayRunner(
        source,
        BlockingEngine(),
        FakePlanner(None),
        CalibrationProfile("test", 1, 2, 2, verified=True),
        max_frame_age=5,
        max_observation_age=5,
        perception_timeout=1,
        planner_timeout=1,
    )
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(replay.step()))
    thread.start()
    assert entered.wait(1)

    replay.close()
    release.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert outcomes[0].status == "DISCARDED"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda manifest: manifest.update(format_version=999), "version"),
        (lambda manifest: manifest.update(runtime_generation=0), "runtime_generation"),
        (lambda manifest: manifest.update(completion_status="MAYBE"), "status"),
    ],
)
def test_replay_rejects_invalid_manifest(tmp_path, mutation, message):
    path = recorded_session(tmp_path, 1)
    manifest = read_manifest(path)
    mutation(manifest)
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayError, match=message):
        ReplayCaptureSource(path)


def test_replay_rejects_missing_manifest(tmp_path):
    path = tmp_path / "missing"
    path.mkdir()
    with pytest.raises(ReplayError, match="manifest"):
        ReplayCaptureSource(path)


def test_replay_rejects_missing_frame_and_path_traversal(tmp_path):
    path = recorded_session(tmp_path, 1)
    events = read_events(path)
    frame_event = next(
        item for item in events if item["event_type"] == "FRAME_CAPTURED"
    )
    image = path / frame_event["payload"]["image_file"]
    image.unlink()
    with pytest.raises(ReplayError, match="frame image"):
        ReplayCaptureSource(path)

    image.write_bytes(b"not an image")
    frame_event["payload"]["image_file"] = "../../secret.png"
    frame_event["payload"]["image_bytes"] = len(b"not an image")
    write_events(path, events)
    with pytest.raises(ReplayError, match="escapes"):
        ReplayCaptureSource(path)


def test_replay_rejects_malformed_event_and_duplicate_sequence(tmp_path):
    path = recorded_session(tmp_path, 1)
    original = (path / "events.jsonl").read_text(encoding="utf-8")
    (path / "events.jsonl").write_text(original + "{bad\n", encoding="utf-8")
    with pytest.raises(ReplayError, match="malformed"):
        ReplayCaptureSource(path)

    events = [json.loads(line) for line in original.splitlines()]
    events[1]["sequence"] = events[0]["sequence"]
    write_events(path, events)
    with pytest.raises(ReplayError, match="sequence"):
        ReplayCaptureSource(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("width", 0, "width"),
        ("runtime_generation", 99, "generation"),
        ("frame_sequence", 1, "duplicate"),
    ],
)
def test_replay_rejects_invalid_frame_contract(tmp_path, field, value, message):
    path = recorded_session(tmp_path, 2)
    events = read_events(path)
    frames = [item for item in events if item["event_type"] == "FRAME_CAPTURED"]
    frames[-1]["payload"][field] = value
    write_events(path, events)

    with pytest.raises(ReplayError, match=message):
        ReplayCaptureSource(path)


def test_partial_session_prefix_is_explicit_and_configurable(tmp_path):
    path = recorded_session(tmp_path, 1)
    manifest = read_manifest(path)
    manifest["completion_status"] = "INTERRUPTED"
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    source = ReplayCaptureSource(path)
    assert source.incomplete
    assert source.frame_count == 1
    with pytest.raises(ReplayError, match="incomplete"):
        ReplayCaptureSource(path, allow_incomplete=False)


def test_interrupted_session_ignores_only_an_unambiguous_partial_tail(tmp_path):
    path = recorded_session(tmp_path, 1)
    manifest = read_manifest(path)
    manifest["completion_status"] = "INTERRUPTED"
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with (path / "events.jsonl").open("ab") as handle:
        handle.write(b'{"event_type":"SHUT')

    source = ReplayCaptureSource(path)

    assert source.incomplete
    assert source.frame_count == 1


def test_replay_applies_recorded_game_lost_and_ready_boundaries(tmp_path):
    value = recorder(tmp_path)
    start = time.monotonic()
    assert value.record_event(RecordingEventType.GAME_READY)
    assert value.record_frame(frame(1, captured=start + 1))
    assert value.record_event(RecordingEventType.GAME_LOST)
    assert value.record_frame(frame(2, captured=start + 2))
    assert value.record_event(RecordingEventType.GAME_READY)
    assert value.record_frame(frame(3, captured=start + 3))
    assert value.close()

    outcomes = runner(value.path).run()

    assert [item.status for item in outcomes] == [
        "ACTION_RESULT",
        "GAME_NOT_READY",
        "ACTION_RESULT",
    ]


def test_worker_replay_prepares_without_x11_or_runtime_verification(tmp_path):
    path = recorded_session(tmp_path, 1)
    config = WorkerConfig(
        plugin="dst",
        mode=WorkerMode.REPLAY,
        replay_session_path=path,
        capture_max_width=320,
        capture_max_height=240,
    )
    config.validate()
    worker = DSTGameWorker(config, worker_generation=8)
    context = WorkerContext(
        1,
        7,
        DisplayEnvironment(":99"),
        runtime_verified=False,
        runtime_generation=4,
    )

    prepared = worker.prepare(context)
    ticked = worker.tick(context)
    direct = worker.execute_action(
        Action(
            "replay-direct",
            ActionName.MOVE_FORWARD,
            runtime_generation=4,
            worker_generation=8,
            runtime_id=7,
            duration=1,
        )
    )

    assert prepared.mode == WorkerMode.REPLAY
    assert prepared.state == "OBSERVING"
    assert ticked.telemetry["replay_frames_processed"] == 1
    assert direct.status == ActionStatus.SUPPRESSED
    assert direct.action_id == "replay-direct"
    assert worker.input is None
    assert worker.actions is None
    assert ticked.details["diagnostics"]["input_safety"]["replay_input_forbidden"]
    diagnostics = ticked.details["diagnostics"]
    assert {"capture_health", "recording", "replay"} <= diagnostics.keys()
    assert len(ticked.details["transitions"]) <= 10
    serialized = json.dumps(ticked.as_dict())
    assert "pixels" not in serialized
    assert worker.shutdown().state == "STOPPED"


def test_switching_replay_mode_replaces_source_and_never_reuses_live_path(tmp_path):
    path = recorded_session(tmp_path, 1)
    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.REPLAY,
            replay_session_path=path,
            capture_max_width=320,
            capture_max_height=240,
        ),
        worker_generation=3,
    )
    context = WorkerContext(1, 7, runtime_generation=1)
    worker.prepare(context)
    old_source = worker.capture

    observed = worker.set_mode(WorkerMode.OBSERVE)

    assert observed.mode == WorkerMode.OBSERVE
    assert worker.replay is None
    assert worker.input is None
    with pytest.raises(CaptureError, match="closed"):
        old_source.capture()

    replayed = worker.set_mode(WorkerMode.REPLAY)
    assert replayed.mode == WorkerMode.REPLAY
    assert worker.capture is not old_source
    assert worker.input is None
    worker.shutdown()


def test_replay_config_rejects_missing_session_and_live_recording_mix(tmp_path):
    with pytest.raises(ValueError, match="requires replay_session_path"):
        WorkerConfig(plugin="dst", mode=WorkerMode.REPLAY).validate()
    with pytest.raises(ValueError, match="cannot be enabled"):
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.REPLAY,
            recording_enabled=True,
            replay_session_path=tmp_path,
        ).validate()


def test_validation_movement_requires_opt_in_validation_flow():
    with pytest.raises(ValueError, match="requires the validation flow"):
        WorkerConfig(
            plugin="dst",
            validation_movement_enabled=True,
        ).validate()


def test_disabling_recording_worker_closes_session_and_releases_resources(tmp_path):
    config = WorkerConfig(
        plugin="dst",
        mode=WorkerMode.OBSERVE,
        recording_enabled=True,
        recording_root=tmp_path.resolve(),
    )
    replace(config, mode=WorkerMode.DISABLED).validate()
    worker = DSTGameWorker(config, worker_generation=3)
    session = recorder(tmp_path)
    worker.recorder = session

    report = worker.set_mode(WorkerMode.DISABLED)

    assert report.mode == WorkerMode.DISABLED
    assert report.state == "DISABLED"
    assert worker.recorder is None
    assert worker.input is None
    assert json.loads((session.path / "manifest.json").read_text())["completion_status"] == "COMPLETE"


@pytest.mark.parametrize(
    "change",
    [
        {"recording_max_duration": 0},
        {"recording_max_frames": 0},
        {"recording_max_bytes": 100},
        {"recording_queue_size": 0},
        {"recording_frame_interval": -1},
        {"recording_shutdown_timeout": 0},
        {"replay_timing_mode": "WALL_CLOCK_GUESS"},
        {
            "recording_queue_size": 64,
            "capture_max_width": 3840,
            "capture_max_height": 2160,
        },
    ],
)
def test_stage4_config_limits_fail_closed(tmp_path, change):
    config = WorkerConfig(
        plugin="dst",
        mode=WorkerMode.OBSERVE,
        recording_enabled=True,
        recording_root=tmp_path.resolve(),
    )
    with pytest.raises(ValueError):
        replace(config, **change).validate()


def test_environment_secret_is_not_copied_to_artifacts_or_diagnostics(
    tmp_path, monkeypatch
):
    secret = "fake-stage4-runtime-secret"
    monkeypatch.setenv("RUNTIME_BEARER_TOKEN", secret)
    path = recorded_session(tmp_path, 1)
    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.REPLAY,
            replay_session_path=path,
            capture_max_width=320,
            capture_max_height=240,
        ),
        worker_generation=3,
    )
    context = WorkerContext(1, 7, runtime_generation=1)
    worker.prepare(context)
    report = worker.tick(context)
    worker.shutdown()

    artifacts = (path / "manifest.json").read_text(encoding="utf-8") + (
        path / "events.jsonl"
    ).read_text(encoding="utf-8")
    diagnostics = json.dumps(report.as_dict())
    assert secret not in artifacts
    assert secret not in diagnostics
