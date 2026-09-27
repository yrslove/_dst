from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from PIL import Image

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.actions import (
    Action,
    ActionExecutor,
    ActionName,
    ActionResult,
    ActionStatus,
    ObserveActions,
)
from runtime_agent.gameworker.activity import ActivityController
from runtime_agent.gameworker.base import WorkerContext
from runtime_agent.gameworker.capture import Frame
from runtime_agent.gameworker.config import InputBindings, WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.geometry import (
    CalibrationProfile,
    NormalizedPoint,
    Viewport,
)
from runtime_agent.gameworker.input import (
    DeadmanSafety,
    FakeInputDriver,
    InputController,
    InputLease,
)
from runtime_agent.gameworker.recording import (
    RecordingEventType,
    RecordingLimits,
    SessionRecorder,
)
from runtime_agent.gameworker.transitions import ActionLifecycle, click_request
from runtime_agent.gameworker.vision import (
    AssetRegistry,
    DSTScreen,
    ObservationValidity,
    VisionDetector,
)

ASSETS = Path(__file__).resolve().parents[2] / "runtime_agent/gameworker/dst/assets"


def observe(
    *, button: bool = True, title: bool = True, hover: bool = False, shift: int = 0
):
    image = Image.new("RGB", (1280, 720), "#202020")
    if title:
        image.paste(Image.open(ASSETS / "login_reward_title.png"), (511 + shift, 108))
    if button:
        image.paste(
            Image.open(
                ASSETS
                / (
                    "login_reward_open_hover.png"
                    if hover
                    else "login_reward_open_button.png"
                )
            ),
            (563 + shift, 607),
        )
    image.paste(Image.open(ASSETS / "main_menu_browse.png"), (65, 339))
    return analyze_image(image, "test-frame", 1)


def analyze_image(image: Image.Image, frame_id: str, sequence: int):
    now = time.monotonic()
    frame = Frame(
        frame_id=frame_id,
        sequence=sequence,
        captured_at="2026-09-26T00:00:00Z",
        captured_monotonic=now,
        runtime_id=1,
        runtime_generation=1,
        worker_generation=1,
        width=1280,
        height=720,
        pixels=image.tobytes(),
        source="test",
    )
    return VisionDetector(
        AssetRegistry(ASSETS / "manifest.json"), default_threshold=0.8
    ).analyze(
        frame,
        calibration=CalibrationProfile("dst", 1, 1280, 720, verified=True),
        observation_generation=1,
        max_frame_age=3,
        deadline=now + 3,
    )


def test_reward_requires_two_anchors_and_click_tracks_match():
    reward = observe(shift=20)
    assert reward.validity == ObservationValidity.VALID
    assert reward.screen == DSTScreen.LOGIN_REWARD_AVAILABLE
    policy = ActivityController()
    assert policy.propose(reward) is None  # two-frame hysteresis
    proposal = policy.propose(replace(reward, source_frame_id="test-frame-2"))
    assert proposal is not None
    assert proposal.action == ActionName.CLICK_REWARD_OPEN
    target, viewport = click_request(proposal.action, reward)
    assert 0.5 < target.x < 0.53
    assert viewport.width == 1280
    hovered = observe(hover=True)
    assert hovered.screen == DSTScreen.LOGIN_REWARD_AVAILABLE
    assert hovered.validity == ObservationValidity.VALID
    hover_target, _ = click_request(ActionName.CLICK_REWARD_OPEN, hovered)
    assert 0.4 < hover_target.x < 0.6
    without_button = observe(button=False)
    assert without_button.screen == DSTScreen.UNKNOWN
    assert without_button.validity == ObservationValidity.UNKNOWN
    menu_only = observe(button=False, title=False)
    assert menu_only.screen == DSTScreen.UNKNOWN
    main_menu_image = Image.open(ASSETS / "samples/main_menu_after_reward.png").convert(
        "RGB"
    )
    main_menu = analyze_image(main_menu_image, "manual-main-menu", 2)
    assert main_menu.screen == DSTScreen.MAIN_MENU
    assert main_menu.validity == ObservationValidity.VALID


def test_real_host_game_playstyle_frame_replays_as_its_own_state(tmp_path):
    real_frame = Image.open(
        ASSETS / "samples/host_game_playstyle_live.png"
    ).convert("RGB")
    observation = analyze_image(real_frame, "host-game-playstyle-live", 1)

    assert observation.validity == ObservationValidity.VALID
    assert observation.screen == DSTScreen.HOST_GAME_PLAYSTYLE
    assert observation.screen_confidence >= 0.94
    assert {
        "host_game_playstyle_title",
        "host_game_playstyle_prompt",
        "host_game_playstyle_survival",
    } <= {
        item.kind for item in observation.detections if item.detected and item.verified
    }

    main_menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "main-menu-confounder",
        2,
    )
    assert main_menu.screen == DSTScreen.MAIN_MENU
    assert not any(
        item.detected
        for item in main_menu.detections
        if item.kind.startswith("host_game_playstyle_")
    )

    recorder = SessionRecorder(
        tmp_path.resolve(),
        runtime_instance_id="host-game-screen-test",
        runtime_id=1,
        runtime_generation=1,
        worker_generation=1,
        worker_mode="OBSERVE",
        capture_source={"kind": "saved-real-x11"},
        calibration={
            "profile_id": "dst-1280x720-linux-v1",
            "version": 1,
            "verified": True,
            "expected_width": 1280,
            "expected_height": 720,
        },
        perception={
            "engine": "VisionDetector",
            "engine_version": 1,
            "registry_version": 1,
            "assets_configured": True,
            "assets_verified": True,
        },
        limits=RecordingLimits(
            max_duration_seconds=30,
            max_frames=1,
            max_bytes=10 * 1024 * 1024,
            queue_size=8,
            frame_interval_seconds=0,
            shutdown_timeout_seconds=30,
        ),
    )
    assert recorder.record_event(RecordingEventType.GAME_READY)
    captured_monotonic = time.monotonic()
    recorded_frame = Frame(
        frame_id="host-game-playstyle-real-frame",
        sequence=1,
        captured_at="2026-09-27T00:00:00+00:00",
        captured_monotonic=captured_monotonic,
        runtime_id=1,
        runtime_generation=1,
        worker_generation=1,
        width=1280,
        height=720,
        pixels=real_frame.tobytes(),
        source="captured-x11",
    )
    assert recorder.record_frame(recorded_frame)
    assert recorder.close()

    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.REPLAY,
            replay_session_path=recorder.path,
            capture_max_width=1280,
            capture_max_height=720,
            calibration_verified=True,
            vision_threshold=0.8,
            assets_manifest=ASSETS / "manifest.json",
        ),
        worker_generation=2,
    )
    context = WorkerContext(
        1,
        1,
        DisplayEnvironment(":99"),
        runtime_verified=False,
        runtime_generation=1,
    )
    try:
        assert worker.prepare(context).mode == WorkerMode.REPLAY
        replayed = worker.tick(context)
        assert replayed.telemetry["replay_frames_processed"] == 1
        assert replayed.details["observation"]["screen"] == (
            DSTScreen.HOST_GAME_PLAYSTYLE.value
        )
        assert replayed.details["observation"]["validity"] == "VALID"
        assert worker.input is None
    finally:
        worker.shutdown()


def test_reward_click_is_one_shot_and_observe_has_no_input():
    observation = observe()
    policy = ActivityController()
    policy.propose(observation)
    proposal = policy.propose(observation)
    assert proposal is not None
    sink = ObserveActions(runtime_id=1, runtime_generation=1, worker_generation=1)
    assert not hasattr(sink, "controller")
    target, viewport = click_request(proposal.action, observation)
    dry_run = sink.execute(proposal.action, target=target, viewport=viewport)
    assert dry_run.status == ActionStatus.SUPPRESSED
    policy.on_action_result(observation, dry_run)
    verifying = ActionResult(
        "one", proposal.action, ActionStatus.VERIFYING, 0.01, 1, 1, 1
    )
    pending = policy.on_action_result(observation, verifying)
    assert pending.status == ActionStatus.VERIFYING
    assert policy.propose(observation) is None
    assert policy.counters["active_actions"] == 1
    assert policy.counters["reward_detected"] == 1


def test_reward_verification_timeout_requires_intervention():
    observation = observe()
    policy = ActivityController()
    policy.propose(observation)
    proposal = policy.propose(observation)
    clock = [observation.observed_monotonic + .01]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    sent = ActionResult("first", proposal.action, ActionStatus.SENT, .01, 1, 1, 1)
    pending = lifecycle.begin(sent, observation)
    assert pending.status == ActionStatus.VERIFYING
    policy.on_action_result(observation, pending)
    assert policy.propose(observation) is None
    clock[0] += 46
    failed = lifecycle.poll()
    assert failed is not None and failed.status == ActionStatus.TIMED_OUT
    policy.on_action_failure(failed)
    assert policy.intervention_required
    assert policy.propose(observation) is None


def test_click_success_requires_two_replay_verified_transition_frames():
    reward = observe()
    policy = ActivityController()
    policy.propose(reward)
    proposal = policy.propose(reward)
    clock = [reward.observed_monotonic + .01]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    injected = ActionResult("click", proposal.action, ActionStatus.SENT, .1, 1, 1, 1)
    pending = lifecycle.begin(injected, reward)
    policy.on_action_result(reward, pending)
    assert pending.status == ActionStatus.VERIFYING
    assert policy.counters["reward_claimed"] == 0
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "menu-1",
        2,
    )
    assert lifecycle.observe(menu) is None
    menu2 = replace(menu, source_frame_id="menu-2", source_sequence=3,
                    observed_monotonic=menu.observed_monotonic + .02)
    verified = lifecycle.observe(menu2)
    assert verified is not None and verified.status == ActionStatus.SUCCEEDED
    policy.on_verified(menu2, verified)
    assert "MAIN_MENU" in verified.reason
    assert policy.counters["reward_opened"] == 1
    assert policy.counters["reward_claimed"] == 0


def test_active_executor_rejects_unlisted_input_and_requires_anchor():
    driver = FakeInputDriver()
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=10,
        max_key_presses_per_second=10,
        driver=driver,
    )
    executor = ActionExecutor(
        controller,
        DeadmanSafety(controller, 5),
        InputBindings(),
        runtime_generation=1,
        worker_generation=1,
        runtime_id=1,
        mode=WorkerMode.ACTIVE,
        action_timeout=1,
        allowed_actions=frozenset({ActionName.CLICK_REWARD_OPEN}),
    )
    now = time.monotonic()
    blocked = executor.execute(
        Action("blocked", ActionName.INTERACT, 1, 1, runtime_id=1, deadline=now + 0.8)
    )
    missing_anchor = executor.execute(
        Action(
            "missing",
            ActionName.CLICK_REWARD_OPEN,
            1,
            1,
            runtime_id=1,
            deadline=now + 0.8,
        )
    )
    assert blocked.status == ActionStatus.REJECTED
    assert missing_anchor.status == ActionStatus.REJECTED
    assert driver.events == []
    executor.shutdown()


def test_observe_worker_constructs_no_live_input_controller(monkeypatch):
    def forbid_input(*_args, **_kwargs):
        raise AssertionError("OBSERVE must not construct InputController")

    monkeypatch.setattr(
        "runtime_agent.gameworker.dst.worker.InputController", forbid_input
    )
    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.OBSERVE,
            assets_manifest=ASSETS / "manifest.json",
        )
    )
    report = worker.prepare(
        WorkerContext(1, 1, DisplayEnvironment(":99"), runtime_verified=True)
    )
    assert report.state != "ERROR"
    assert worker.input is None
    assert isinstance(worker.actions, ObserveActions)
    worker.set_mode(WorkerMode.DISABLED)
    assert worker.actions is None


def test_visual_click_focuses_dst_before_mouse_event():
    driver = FakeInputDriver()
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=10,
        max_key_presses_per_second=10,
        driver=driver,
    )
    controller.activate()
    controller.click(NormalizedPoint(0.5, 0.88), Viewport(1280, 720))
    assert [event.operation for event in driver.events] == [
        "mouse_move",
        "focus_game",
        "mouse_down",
        "mouse_up",
    ]
