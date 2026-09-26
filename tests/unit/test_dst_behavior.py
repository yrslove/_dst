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
    assert 0.5 < proposal.target.x < 0.53
    assert proposal.viewport.width == 1280
    hovered = observe(hover=True)
    assert hovered.screen == DSTScreen.LOGIN_REWARD_AVAILABLE
    assert hovered.validity == ObservationValidity.VALID
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


def test_reward_click_is_one_shot_and_observe_has_no_input():
    observation = observe()
    policy = ActivityController()
    policy.propose(observation)
    proposal = policy.propose(observation)
    assert proposal is not None
    sink = ObserveActions(runtime_id=1, runtime_generation=1, worker_generation=1)
    assert not hasattr(sink, "controller")
    dry_run = sink.execute(
        proposal.action, target=proposal.target, viewport=proposal.viewport
    )
    assert dry_run.status == ActionStatus.SUPPRESSED
    policy.on_action_result(observation, dry_run)
    completed = ActionResult(
        "one", proposal.action, ActionStatus.COMPLETED, 0.01, 1, 1, 1
    )
    pending = policy.on_action_result(observation, completed)
    assert pending.status == ActionStatus.PENDING_VERIFICATION
    assert policy.propose(observation) is None
    assert policy.counters["active_actions"] == 1
    assert policy.counters["reward_detected"] == 1


def test_reward_retry_is_bounded_and_requires_unchanged_verified_screen():
    observation = observe()
    policy = ActivityController()
    policy.propose(observation)
    proposal = policy.propose(observation)
    completed = ActionResult(
        "first", proposal.action, ActionStatus.COMPLETED, 0.01, 1, 1, 1
    )
    pending = policy.on_action_result(observation, completed)
    assert pending.status == ActionStatus.PENDING_VERIFICATION
    assert policy.propose(observation) is None
    later = replace(observation, observed_monotonic=observation.observed_monotonic + 13)
    failed = policy.verify_observation(later)
    assert failed.status == ActionStatus.FAILED
    assert "transition" in failed.reason
    retry = policy.propose(later)
    assert retry is not None and retry.action == ActionName.CLICK_REWARD_OPEN
    policy.on_action_result(later, replace(completed, action_id="second"))
    final = replace(later, observed_monotonic=later.observed_monotonic + 13)
    failed = policy.verify_observation(final)
    assert failed.status == ActionStatus.FAILED
    assert policy.intervention_required


def test_click_success_requires_two_replay_verified_transition_frames():
    reward = observe()
    policy = ActivityController()
    policy.propose(reward)
    proposal = policy.propose(reward)
    injected = ActionResult(
        "click", proposal.action, ActionStatus.COMPLETED, 0.1, 1, 1, 1
    )
    pending = policy.on_action_result(reward, injected)
    assert pending.status == ActionStatus.PENDING_VERIFICATION
    assert policy.counters["reward_claimed"] == 0
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "menu-1",
        2,
    )
    assert policy.verify_observation(menu) is None
    menu2 = replace(menu, source_frame_id="menu-2", source_sequence=3)
    verified = policy.verify_observation(menu2)
    assert verified.status == ActionStatus.COMPLETED
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
