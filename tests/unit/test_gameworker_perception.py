from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.actions import (
    ActionName,
    ActionResult,
    ActionStatus,
    GameActions,
)
from runtime_agent.gameworker.activity import ActionProposal, FakePlanner
from runtime_agent.gameworker.capture import (
    CaptureError,
    CaptureFailure,
    FakeCaptureSource,
    Frame,
    LatestFrameSlot,
    X11ScreenCapture,
)
from runtime_agent.gameworker.config import InputBindings, WorkerMode
from runtime_agent.gameworker.geometry import (
    CalibrationProfile,
    CoordinateSpace,
    NormalizedPoint,
    NormalizedRegion,
    Viewport,
)
from runtime_agent.gameworker.input import (
    DeadmanSafety,
    FakeInputDriver,
    InputController,
    InputLease,
)
from runtime_agent.gameworker.perception import ObservePipeline
from runtime_agent.gameworker.vision import (
    AssetRegistry,
    Detection,
    GameObservation,
    ObservationStore,
    ObservationValidity,
    VisionDetector,
)


def make_frame(
    sequence: int = 1,
    *,
    captured_monotonic: float = 11.0,
    runtime_generation: int = 7,
    worker_generation: int = 3,
    runtime_id: int = 2,
    image: Image.Image | None = None,
) -> Frame:
    image = image or Image.new("RGB", (8, 6), "black")
    return Frame(
        frame_id=f"r{runtime_generation}-w{worker_generation}-f{sequence}",
        sequence=sequence,
        captured_at="2026-01-01T00:00:00+00:00",
        captured_monotonic=captured_monotonic,
        runtime_id=runtime_id,
        runtime_generation=runtime_generation,
        worker_generation=worker_generation,
        width=image.width,
        height=image.height,
        pixels=image.tobytes(),
        source="test",
    )


def make_observation(
    frame: Frame,
    *,
    generation: int = 1,
    validity: ObservationValidity = ObservationValidity.VALID,
    verified: bool = True,
    observed_monotonic: float | None = None,
    prompt: bool = True,
) -> GameObservation:
    observed = (
        frame.captured_monotonic if observed_monotonic is None else observed_monotonic
    )
    game = Detection("game_hud", True, 0.99, verified=verified)
    menu = Detection("pause_menu", False, 0.01, verified=verified)
    player = Detection("player_marker", True, 0.95, verified=verified)
    interaction = Detection(
        "interaction_prompt", prompt, 0.98 if prompt else 0.02, verified=verified
    )
    detections = (game, menu, player, interaction)
    return GameObservation(
        timestamp="2026-01-01T00:00:00+00:00",
        observed_monotonic=observed,
        observation_generation=generation,
        source_frame_id=frame.frame_id,
        source_sequence=frame.sequence,
        source_captured_monotonic=frame.captured_monotonic,
        runtime_id=frame.runtime_id,
        runtime_generation=frame.runtime_generation,
        worker_generation=frame.worker_generation,
        validity=validity,
        fresh_until=frame.captured_monotonic + 5,
        game_visible=game,
        menu_visible=menu,
        player_visible=player,
        interaction_prompt_visible=interaction,
        worker_confidence=0.99,
        detections=detections,
        calibration_profile_id="test",
        calibration_version=1,
        calibration_verified=verified,
        assets_verified=verified,
        screen_hash="synthetic",
        screen_change=None,
        perception_latency=max(0.0, observed - frame.captured_monotonic),
    )


class FakeEngine:
    def __init__(self, clock_value: list[float], *, validity=ObservationValidity.VALID):
        self.clock_value = clock_value
        self.validity = validity
        self.frames: list[Frame] = []
        self.advance = 0.0

    def analyze(
        self,
        frame,
        *,
        calibration,
        observation_generation,
        max_frame_age,
        deadline,
    ):
        self.frames.append(frame)
        self.clock_value[0] += self.advance
        return make_observation(
            frame,
            generation=observation_generation,
            validity=self.validity,
            verified=self.validity == ObservationValidity.VALID,
            observed_monotonic=self.clock_value[0],
        )


class NoopActions:
    def execute(self, _action, *, duration=None):
        raise AssertionError("actions must not be reached")


def make_pipeline(source, engine, planner, actions, now):
    return ObservePipeline(
        source,
        engine,
        planner,
        actions,
        CalibrationProfile("test", 1, 8, 6, verified=True),
        runtime_generation=7,
        worker_generation=3,
        max_frame_age=5,
        max_observation_age=5,
        perception_timeout=1,
        planner_timeout=1,
        clock=lambda: now[0],
    )


def test_frame_is_immutable_bounded_typed_and_monotonic_fresh():
    value = make_frame(captured_monotonic=10)

    assert value.coordinate_space is CoordinateSpace.FRAME_PIXELS
    assert value.is_fresh(2, 12)
    assert not value.is_fresh(2, 12.01)
    assert value.image().size == (8, 6)
    with pytest.raises((AttributeError, TypeError)):
        value.sequence = 2
    with pytest.raises(ValueError, match="payload"):
        replace(value, pixels=b"short")
    with pytest.raises(ValueError, match="metadata"):
        replace(value, metadata=(("bad", object()),))
    with pytest.raises(ValueError, match="dimensions"):
        replace(value, width=0)


def test_fake_capture_and_latest_slot_are_bounded_and_closeable():
    first, second = make_frame(1), make_frame(2)
    source = FakeCaptureSource(
        [first, CaptureError("failed", failure=CaptureFailure.TIMEOUT), second],
        runtime_generation=7,
        worker_generation=3,
    )
    slot = LatestFrameSlot()

    slot.publish(first)
    slot.publish(second)
    assert slot.retained == 1
    assert slot.take() is second
    assert slot.retained == 0
    assert source.capture() is first
    with pytest.raises(CaptureError) as captured:
        source.capture()
    assert captured.value.failure is CaptureFailure.TIMEOUT
    source.close()
    with pytest.raises(CaptureError) as closed:
        source.capture()
    assert closed.value.failure is CaptureFailure.CLOSED
    with pytest.raises(ValueError, match="bound"):
        FakeCaptureSource([first] * 257, runtime_generation=7, worker_generation=3)
    repeating = FakeCaptureSource(
        [second], runtime_generation=7, worker_generation=3, repeat_last=True
    )
    assert repeating.capture() is second
    assert repeating.capture() is second
    repeating.close()


def test_viewport_and_calibration_make_coordinate_spaces_explicit():
    viewport = Viewport(800, 600, left=100, top=50)

    assert viewport.point(NormalizedPoint(0.5, 0.5)) == (500, 350)
    assert viewport.region(NormalizedRegion(0.25, 0.25, 0.75, 0.75)) == (
        300,
        200,
        700,
        500,
    )
    assert viewport.contains(900, 650)
    calibration = CalibrationProfile(
        "offset",
        2,
        1000,
        800,
        NormalizedRegion(0.1, 0.1, 0.9, 0.9),
        verified=False,
    )
    assert calibration.viewport(1000, 800) == Viewport(800, 640, 100, 80)
    assert calibration.matches(1000, 800)
    assert not calibration.verified
    with pytest.raises(ValueError, match="normalized"):
        NormalizedPoint(float("nan"), 0.5)


def test_detection_and_observation_validate_confidence_and_provenance():
    with pytest.raises(ValueError, match="confidence"):
        Detection("bad", True, 1.1)
    with pytest.raises(ValueError, match="provenance"):
        Detection("bad", True, 0.5, metadata=(("value", object()),))

    value = make_observation(make_frame())
    assert value.production_ready
    assert value.is_fresh(12)
    unknown = replace(value, validity=ObservationValidity.UNKNOWN)
    assert not unknown.valid
    assert not unknown.production_ready


def write_manifest(directory: Path, templates) -> Path:
    manifest = directory / "manifest.json"
    manifest.write_text(
        json.dumps(
            {"schema_version": 1, "profile": "synthetic", "templates": templates}
        ),
        encoding="utf-8",
    )
    return manifest


def asset(template_id="game_hud", filename="template.png", *, verified=False):
    return {
        "id": template_id,
        "filename": filename,
        "expected_region": [0, 0, 1, 1],
        "threshold": 0.8,
        "version": 1,
        "expected_use": template_id,
        "verified": verified,
    }


def test_template_registry_rejects_missing_escape_duplicate_and_fake_verification(
    tmp_path,
):
    missing = AssetRegistry(write_manifest(tmp_path, [asset()]))
    assert missing.error_code == "WORKER_ASSET_MISSING"
    assert not missing.production_ready

    (tmp_path / "template.png").write_bytes(b"synthetic")
    duplicate = AssetRegistry(
        write_manifest(tmp_path, [asset(), asset(filename="template.png")])
    )
    assert duplicate.error_code == "WORKER_ASSET_INVALID"

    escaped = AssetRegistry(
        write_manifest(tmp_path, [asset(filename="../outside.png")])
    )
    assert escaped.error_code == "WORKER_ASSET_INVALID"

    invalid_verified = asset()
    invalid_verified["verified"] = "yes"
    invalid = AssetRegistry(write_manifest(tmp_path, [invalid_verified]))
    assert invalid.error_code == "WORKER_ASSET_INVALID"

    invalid_threshold = asset()
    invalid_threshold["threshold"] = 1.1
    invalid = AssetRegistry(write_manifest(tmp_path, [invalid_threshold]))
    assert invalid.error_code == "WORKER_ASSET_INVALID"

    unverified = AssetRegistry(write_manifest(tmp_path, [asset()]))
    assert unverified.configured
    assert not unverified.production_ready


def test_vision_detector_recognizes_only_verified_synthetic_fixture(tmp_path):
    pattern = Image.new("L", (4, 4))
    pattern.putdata([0, 255, 0, 255, 255, 0, 255, 0, 0, 255, 0, 255, 255, 0, 255, 0])
    canvas = Image.new("RGB", (32, 24), "gray")
    canvas.paste(pattern.convert("RGB"), (10, 8))
    templates = []
    for template_id in (
        "game_hud",
        "pause_menu",
        "player_marker",
        "interaction_prompt",
    ):
        filename = f"{template_id}.png"
        pattern.save(tmp_path / filename)
        templates.append(asset(template_id, filename, verified=True))
    registry = AssetRegistry(write_manifest(tmp_path, templates))
    detector = VisionDetector(registry, default_threshold=0.8)
    frame = make_frame(captured_monotonic=time.monotonic(), image=canvas)

    observation = detector.analyze(
        frame,
        calibration=CalibrationProfile("synthetic", 1, 32, 24, verified=True),
        observation_generation=1,
        max_frame_age=5,
        deadline=float("inf"),
    )

    assert registry.production_ready
    assert observation.validity is ObservationValidity.VALID
    assert observation.production_ready
    assert all(item.detected and item.verified for item in observation.detections)


def test_detector_failure_is_isolated_and_observable_as_unknown(tmp_path):
    canvas = Image.new("RGB", (32, 24), "gray")
    templates = []
    for template_id in (
        "game_hud",
        "pause_menu",
        "player_marker",
        "interaction_prompt",
    ):
        filename = f"{template_id}.png"
        (tmp_path / filename).write_bytes(b"not-a-real-png")
        templates.append(asset(template_id, filename, verified=True))
    detector = VisionDetector(
        AssetRegistry(write_manifest(tmp_path, templates)), default_threshold=0.8
    )
    frame = make_frame(captured_monotonic=time.monotonic(), image=canvas)

    observation = detector.analyze(
        frame,
        calibration=CalibrationProfile("synthetic", 1, 32, 24, verified=True),
        observation_generation=1,
        max_frame_age=5,
        deadline=float("inf"),
    )

    assert observation.validity is ObservationValidity.UNKNOWN
    assert "DETECTOR_ERROR" in observation.diagnostic_flags
    assert all(item.metadata for item in observation.detections)


def test_observation_store_rejects_old_duplicate_cross_generation_and_late_after_invalidate():
    store = ObservationStore(7, 3)
    first = make_observation(make_frame(2), generation=2)

    assert store.publish(first)
    assert not store.publish(make_observation(make_frame(1), generation=3))
    assert not store.publish(make_observation(make_frame(2), generation=3))
    assert not store.publish(
        make_observation(
            make_frame(3, runtime_generation=8),
            generation=4,
        )
    )
    store.invalidate()
    assert store.current() is None
    assert not store.publish(make_observation(make_frame(1), generation=1))
    latest = make_observation(make_frame(3), generation=3)
    assert store.publish(latest)
    assert store.current() is latest


def observe_actions():
    driver = FakeInputDriver()
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=100,
        max_key_presses_per_second=100,
        driver=driver,
    )
    deadman = DeadmanSafety(controller, 2)
    actions = GameActions(
        controller,
        deadman,
        InputBindings(),
        mode=WorkerMode.OBSERVE,
        action_timeout=0.5,
        runtime_generation=7,
        worker_generation=3,
        runtime_id=2,
    )
    return actions, driver


@pytest.mark.parametrize(
    ("action_name", "duration"),
    [
        (ActionName.MOVE_FORWARD, 0.01),
        (ActionName.MOVE_BACKWARD, 0.01),
        (ActionName.TURN_LEFT, 0.01),
        (ActionName.TURN_RIGHT, 0.01),
        (ActionName.INTERACT, None),
        (ActionName.CANCEL, None),
        (ActionName.OPEN_INVENTORY, None),
    ],
)
def test_end_to_end_observe_pipeline_returns_suppressed_without_input(
    action_name, duration
):
    now = [10.0]
    frame = make_frame(captured_monotonic=11)
    engine = FakeEngine(now)
    planner = FakePlanner(
        ActionProposal(action_name, duration=duration, reason="synthetic")
    )
    actions, driver = observe_actions()
    pipeline = make_pipeline(
        FakeCaptureSource([frame], runtime_generation=7, worker_generation=3),
        engine,
        planner,
        actions,
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11

    outcome = pipeline.tick()

    pipeline.close()
    actions.shutdown()
    assert outcome.status == "ACTION_RESULT"
    assert outcome.frame_id == frame.frame_id
    assert outcome.action_result is not None
    assert outcome.action_result.status is ActionStatus.SUPPRESSED
    assert outcome.action_result.action is action_name
    assert outcome.proposal == planner.proposal
    assert planner.observations == [outcome.observation]
    assert driver.events == []


def test_pipeline_blocks_interact_without_verified_prompt_before_input(
    monkeypatch,
):
    from test_dst_behavior import ASSETS, analyze_image

    now = [11.0]
    character_image = Image.open(
        ASSETS / "samples/in_world_wilson_live.png"
    ).convert("RGB")
    character = analyze_image(character_image, "fixture-character", 1)
    frames = [
        make_frame(
            sequence,
            captured_monotonic=11.0 + sequence / 10,
            runtime_generation=7,
            worker_generation=3,
            image=character_image,
        )
        for sequence in (1, 2)
    ]

    class CharacterEngine:
        def analyze(self, frame, **_kwargs):
            return replace(
                character,
                observation_generation=frame.sequence,
                source_frame_id=frame.frame_id,
                source_sequence=frame.sequence,
                source_captured_monotonic=frame.captured_monotonic,
                observed_monotonic=frame.captured_monotonic,
                fresh_until=frame.captured_monotonic + 5,
                runtime_id=frame.runtime_id,
                runtime_generation=frame.runtime_generation,
                worker_generation=frame.worker_generation,
            )

    class ActionSink:
        def __init__(self):
            self.calls = []

        def execute(self, action, *, duration=None, target=None, viewport=None):
            self.calls.append((action, duration, target, viewport))
            return ActionResult(
                "interact-sent",
                action,
                ActionStatus.SENT,
                0.01,
                7,
                3,
                2,
            )

    def unexpected_click_request(*_args):
        raise AssertionError("anchorless key action must not resolve a click target")

    monkeypatch.setattr(
        "runtime_agent.gameworker.perception.click_request",
        unexpected_click_request,
    )
    actions = ActionSink()
    pipeline = make_pipeline(
        FakeCaptureSource(frames, runtime_generation=7, worker_generation=3),
        CharacterEngine(),
        FakePlanner(ActionProposal(ActionName.INTERACT)),
        actions,
        now,
    )
    pipeline.on_game_ready()
    first = pipeline.tick()
    second = pipeline.tick()
    pipeline.close()

    assert first.status == "ACTION_FAILED"
    assert second.status == "ACTION_FAILED"
    assert second.action_result.status == ActionStatus.SAFETY_BLOCKED
    assert actions.calls == []


def test_unknown_observation_never_reaches_planner_or_action():
    now = [10.0]
    planner = FakePlanner(ActionProposal(ActionName.INTERACT))
    pipeline = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        FakeEngine(now, validity=ObservationValidity.UNKNOWN),
        planner,
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11

    outcome = pipeline.tick()

    pipeline.close()
    assert outcome.status == "UNKNOWN"
    assert planner.observations == []


def test_invalid_observation_never_reaches_planner_or_action():
    now = [10.0]
    planner = FakePlanner(ActionProposal(ActionName.INTERACT))
    pipeline = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        FakeEngine(now, validity=ObservationValidity.INVALID),
        planner,
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11

    assert pipeline.tick().status == "UNKNOWN"
    assert planner.observations == []
    pipeline.close()


def test_stale_observation_and_perception_failure_do_not_reach_planner():
    now = [10.0]
    planner = FakePlanner(ActionProposal(ActionName.INTERACT))

    class StaleEngine(FakeEngine):
        def analyze(self, frame, **kwargs):
            observation = super().analyze(frame, **kwargs)
            return replace(observation, fresh_until=10.5)

    stale = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        StaleEngine(now),
        planner,
        NoopActions(),
        now,
    )
    stale.on_game_ready()
    now[0] = 11
    assert stale.tick().status == "STALE_OBSERVATION"
    stale.close()

    class FailedEngine:
        def analyze(self, *_args, **_kwargs):
            raise RuntimeError("synthetic failure")

    now[0] = 20
    failed = make_pipeline(
        FakeCaptureSource(
            [make_frame(2, captured_monotonic=21)],
            runtime_generation=7,
            worker_generation=3,
        ),
        FailedEngine(),
        planner,
        NoopActions(),
        now,
    )
    failed.on_game_ready()
    now[0] = 21
    assert failed.tick().status == "PERCEPTION_FAILED"
    assert planner.observations == []
    failed.close()


def test_mismatched_observation_provenance_is_rejected():
    now = [10.0]

    class WrongEngine(FakeEngine):
        def analyze(self, frame, **kwargs):
            observation = super().analyze(frame, **kwargs)
            return replace(observation, source_frame_id="another-frame")

    pipeline = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        WrongEngine(now),
        FakePlanner(None),
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11

    assert pipeline.tick().status == "INVALID_OBSERVATION"
    assert pipeline.latest_observation is None
    pipeline.close()


def test_game_lost_invalidates_and_ready_requires_a_newer_frame():
    now = [10.0]
    frames = [
        make_frame(1, captured_monotonic=11),
        make_frame(2, captured_monotonic=11.5),
        make_frame(3, captured_monotonic=21),
    ]
    planner = FakePlanner(None)
    pipeline = make_pipeline(
        FakeCaptureSource(frames, runtime_generation=7, worker_generation=3),
        FakeEngine(now),
        planner,
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11
    assert pipeline.tick().status == "NO_ACTION"
    assert pipeline.latest_observation is not None

    pipeline.on_game_lost()
    assert pipeline.latest_observation is None
    now[0] = 12
    assert pipeline.tick().status == "GAME_NOT_READY"
    assert pipeline.latest_observation is None

    now[0] = 20
    pipeline.on_game_ready()
    now[0] = 21
    assert pipeline.tick().status == "NO_ACTION"
    assert pipeline.latest_observation.source_frame_id == frames[2].frame_id
    pipeline.close()


def test_pipeline_rejects_stale_and_wrong_generation_frames_before_perception():
    now = [20.0]
    engine = FakeEngine(now)
    source = FakeCaptureSource(
        [
            make_frame(1, captured_monotonic=10),
            make_frame(2, captured_monotonic=21, runtime_generation=6),
        ],
        runtime_generation=7,
        worker_generation=3,
    )
    pipeline = make_pipeline(source, engine, FakePlanner(None), NoopActions(), now)
    pipeline.on_game_ready()

    assert pipeline.tick().status == "STALE_FRAME"
    now[0] = 21
    assert pipeline.tick().status == "STALE_GENERATION"
    assert engine.frames == []
    pipeline.close()


def test_perception_timeout_is_terminal_and_does_not_publish_result():
    now = [10.0]
    engine = FakeEngine(now)
    engine.advance = 2
    pipeline = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        engine,
        FakePlanner(None),
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11

    assert pipeline.tick().status == "PERCEPTION_TIMEOUT"
    assert pipeline.latest_observation is None
    pipeline.close()


def test_pipeline_is_single_flight_and_discards_result_after_game_lost():
    entered = threading.Event()
    release = threading.Event()
    now = [10.0]

    class BlockingCapture:
        runtime_generation = 7
        worker_generation = 3

        def capture(self):
            entered.set()
            assert release.wait(1)
            return make_frame(captured_monotonic=11)

        def close(self):
            release.set()

    pipeline = make_pipeline(
        BlockingCapture(), FakeEngine(now), FakePlanner(None), NoopActions(), now
    )
    pipeline.on_game_ready()
    now[0] = 11
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(pipeline.tick()))
    thread.start()
    assert entered.wait(1)

    assert pipeline.tick().status == "BUSY"
    pipeline.on_game_lost()
    release.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert outcomes[0].status == "DISCARDED"
    assert pipeline.latest_observation is None
    pipeline.close()


@pytest.mark.parametrize("blocking_stage", ["perception", "planner"])
def test_game_lost_cancels_inflight_perception_or_planner_result(blocking_stage):
    entered = threading.Event()
    release = threading.Event()
    now = [10.0]

    class BlockingEngine(FakeEngine):
        def analyze(self, frame, **kwargs):
            entered.set()
            assert release.wait(1)
            return super().analyze(frame, **kwargs)

    class BlockingPlanner:
        def propose(self, _observation):
            entered.set()
            assert release.wait(1)
            return ActionProposal(ActionName.INTERACT)

    engine = BlockingEngine(now) if blocking_stage == "perception" else FakeEngine(now)
    planner = BlockingPlanner() if blocking_stage == "planner" else FakePlanner(None)
    pipeline = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        engine,
        planner,
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(pipeline.tick()))
    thread.start()
    assert entered.wait(1)

    pipeline.on_game_lost()
    release.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert outcomes[0].status == "DISCARDED"
    assert pipeline.latest_observation is None
    pipeline.close()


def test_shutdown_during_capture_releases_waiter_and_leaves_no_background_work():
    entered = threading.Event()
    release = threading.Event()
    now = [10.0]

    class BlockingCapture:
        runtime_generation = 7
        worker_generation = 3

        def capture(self):
            entered.set()
            assert release.wait(1)
            return make_frame(captured_monotonic=11)

        def close(self):
            release.set()

    pipeline = make_pipeline(
        BlockingCapture(), FakeEngine(now), FakePlanner(None), NoopActions(), now
    )
    pipeline.on_game_ready()
    now[0] = 11
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(pipeline.tick()))
    thread.start()
    assert entered.wait(1)

    pipeline.close()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert outcomes[0].status == "DISCARDED"
    assert pipeline.tick().status == "CLOSED"


def test_planner_timeout_discards_proposal_before_action():
    now = [10.0]

    class SlowPlanner:
        def propose(self, _observation):
            now[0] += 2
            return ActionProposal(ActionName.INTERACT)

    pipeline = make_pipeline(
        FakeCaptureSource(
            [make_frame(captured_monotonic=11)],
            runtime_generation=7,
            worker_generation=3,
        ),
        FakeEngine(now),
        SlowPlanner(),
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()
    now[0] = 11

    assert pipeline.tick().status == "PLANNER_TIMEOUT"
    pipeline.close()


def test_capture_failure_updates_bounded_health_without_observation():
    now = [10.0]
    recovered = make_frame(1, captured_monotonic=10.1)
    pipeline = make_pipeline(
        FakeCaptureSource(
            [
                CaptureError("synthetic timeout 1", failure=CaptureFailure.TIMEOUT),
                CaptureError("synthetic timeout 2", failure=CaptureFailure.TIMEOUT),
                CaptureError("synthetic timeout 3", failure=CaptureFailure.TIMEOUT),
                recovered,
            ],
            runtime_generation=7,
            worker_generation=3,
        ),
        FakeEngine(now),
        FakePlanner(None),
        NoopActions(),
        now,
    )
    pipeline.on_game_ready()

    assert pipeline.tick().status == "CAPTURE_FAILED"
    assert pipeline.tick().status == "CAPTURE_FAILED"
    assert pipeline.tick().status == "CAPTURE_FAILED"
    assert pipeline.latest_observation is None
    assert pipeline.health()["capture_errors"] == 3
    assert pipeline.tick().status != "CAPTURE_FAILED"
    assert pipeline.health()["capture_success_total"] == 1
    pipeline.close()


class _FakeCaptureConnection:
    def __init__(self, messages, role, owner):
        self.messages = messages
        self.role = role
        self.owner = owner
        self.closed = False

    def send(self, value):
        if self.role == "child":
            self.owner.responses.put(value)
        elif value is not None and self.owner.reply_to_capture:
            self.owner.responses.put(
                (
                    "FRAME",
                    value,
                    True,
                    1,
                    1,
                    b"\x00\x00\x00",
                    "captured",
                    time.monotonic(),
                    None,
                )
            )

    def poll(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.messages.empty():
                return True
            time.sleep(0.001)
        return not self.messages.empty()

    def recv(self):
        return self.messages.get_nowait()

    def close(self):
        self.closed = True


class _FakeCaptureProcess:
    def __init__(self, owner, kwargs):
        self.owner = owner
        self.kwargs = kwargs
        self.alive = False
        self.terminated = False

    def start(self):
        self.alive = True
        self.kwargs["args"][0].send(("READY",))

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.terminated = True
        self.alive = False

    def join(self, timeout=None):
        if timeout is not None and timeout <= 0.5:
            self.alive = False


class _FakeCaptureProcessContext:
    def __init__(self, *, reply_to_capture):
        self.reply_to_capture = reply_to_capture
        self.responses = queue.Queue()
        self.pipe_count = 0
        self.processes = []

    def Pipe(self, duplex=True):
        assert duplex
        self.pipe_count += 1
        return (
            _FakeCaptureConnection(self.responses, "parent", self),
            _FakeCaptureConnection(queue.Queue(), "child", self),
        )

    def Process(self, **kwargs):
        process = _FakeCaptureProcess(self, kwargs)
        self.processes.append(process)
        return process


def test_x11_capture_reuses_one_killable_helper_for_multiple_frames():
    context = _FakeCaptureProcessContext(reply_to_capture=True)
    capture = X11ScreenCapture(
        DisplayEnvironment(":99"),
        max_width=800,
        max_height=600,
        timeout=0.1,
        runtime_id=2,
        runtime_generation=7,
        worker_generation=3,
        process_context=context,
    )

    first = capture.capture()
    second = capture.capture()

    assert (first.sequence, second.sequence) == (1, 2)
    assert context.pipe_count == 1
    assert len(context.processes) == 1
    assert context.processes[0].is_alive()
    capture.close()
    assert not context.processes[0].is_alive()


def test_x11_capture_timeout_terminates_helper_and_restarts_cleanly():
    context = _FakeCaptureProcessContext(reply_to_capture=False)
    capture = X11ScreenCapture(
        DisplayEnvironment(":99"),
        max_width=800,
        max_height=600,
        timeout=0.01,
        runtime_id=2,
        runtime_generation=7,
        worker_generation=3,
        process_context=context,
    )

    with pytest.raises(CaptureError) as error:
        capture.capture()

    assert error.value.failure is CaptureFailure.TIMEOUT
    assert context.processes[0].terminated
    assert capture._process is None
    assert capture._connection is None
    capture.close()
