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
    Detection,
    DSTScreen,
    ObservationValidity,
    VisionDetector,
)

ASSETS = Path(__file__).resolve().parents[2] / "runtime_agent/gameworker/dst/assets"


def test_real_world_reset_frame_and_canonical_recovery_route():
    death = analyze_image(
        Image.open(ASSETS / "samples/death_world_reset_live.png").convert("RGB"),
        "death-reset-1", 1,
    )
    assert death.validity == ObservationValidity.VALID
    assert death.screen == DSTScreen.WORLD_RESET_PENDING
    assert death.screen_confidence >= 0.94
    assert {"death_world_reset_text", "death_reset_now_button"} <= {
        item.kind for item in death.detections if item.detected and item.verified
    }
    alive = analyze_image(
        Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB"),
        "alive-control", 1,
    )
    assert alive.screen == DSTScreen.IN_WORLD_IDLE
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(death) is None
    assert policy.propose(replace(death, source_frame_id="death-reset-2", source_sequence=2)) is None
    character = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "character-after-reset-1", 3,
    )
    assert policy.propose(character) is None
    proposal = policy.propose(replace(
        character, source_frame_id="character-after-reset-2", source_sequence=4,
    ))
    assert proposal is not None and proposal.action == ActionName.SELECT_SURVIVOR
    loadout = analyze_image(
        Image.open(ASSETS / "samples/character_loadout_live.png").convert("RGB"),
        "loadout-after-reset-1", 5,
    )
    policy.on_verified(loadout, ActionResult(
        "select-survivor", ActionName.SELECT_SURVIVOR, ActionStatus.SUCCEEDED,
        0.1, 1, 1, 1,
    ))
    assert policy.propose(loadout) is None
    go = policy.propose(replace(
        loadout, source_frame_id="loadout-after-reset-2", source_sequence=6,
    ))
    assert go is not None and go.action == ActionName.START_SURVIVOR
    policy.on_verified(alive, ActionResult(
        "start-survivor", ActionName.START_SURVIVOR, ActionStatus.SUCCEEDED,
        0.1, 1, 1, 1,
    ))
    assert policy._validation_step == 3


def test_opt_in_in_world_validation_moves_boundedly_then_returns_to_pause():
    alive = analyze_image(
        Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB"),
        "movement-goal-1",
        1,
    )
    policy = ActivityController(
        validation_flow_enabled=True, validation_movement_enabled=True
    )
    proposal = None
    observation = alive
    for sequence in range(1, 7):
        observation = replace(
            alive,
            source_frame_id=f"movement-goal-{sequence}",
            source_sequence=sequence,
        )
        proposal = policy.propose(observation)
    assert proposal is not None
    assert proposal.action == ActionName.MOVE_FORWARD
    assert proposal.duration == 0.65
    policy.on_action_result(
        observation,
        ActionResult(
            "move-forward", ActionName.MOVE_FORWARD, ActionStatus.VERIFYING,
            0.35, 1, 1, 1,
        ),
    )
    policy.on_verified(
        observation,
        ActionResult(
            "move-forward", ActionName.MOVE_FORWARD, ActionStatus.SUCCEEDED,
            0.35, 1, 1, 1,
        ),
    )

    backward_observation = replace(
        alive, source_frame_id="movement-goal-backward", source_sequence=7
    )
    backward = policy.propose(backward_observation)
    assert backward is not None and backward.action == ActionName.MOVE_BACKWARD
    assert backward.duration == 0.65
    policy.on_action_result(
        backward_observation,
        ActionResult(
            "move-backward", ActionName.MOVE_BACKWARD, ActionStatus.VERIFYING,
            0.35, 1, 1, 1,
        ),
    )
    policy.on_verified(
        backward_observation,
        ActionResult(
            "move-backward", ActionName.MOVE_BACKWARD, ActionStatus.SUCCEEDED,
            0.35, 1, 1, 1,
        ),
    )

    pause_observation = replace(
        alive,
        source_frame_id="movement-goal-pause",
        source_sequence=8,
        interaction_prompt_visible=Detection(
            "interaction_prompt", True, 0.99, detector_id="test", verified=True
        ),
    )
    pause = policy.propose(pause_observation)
    assert pause is not None and pause.action == ActionName.PAUSE_WORLD
    policy.on_verified(
        replace(pause_observation, screen=DSTScreen.PAUSED),
        ActionResult(
            "pause", ActionName.PAUSE_WORLD, ActionStatus.SUCCEEDED, 0.2, 1, 1, 1
        ),
    )
    assert policy._validation_step == 7
    paused = replace(
        pause_observation,
        screen=DSTScreen.PAUSED,
        source_frame_id="movement-goal-paused",
        source_sequence=9,
    )
    assert policy.propose(paused) is None
    paused = replace(
        paused, source_frame_id="movement-goal-paused-2", source_sequence=10
    )
    resume = policy.propose(paused)
    assert resume is not None and resume.action == ActionName.RESUME_WORLD
    policy.on_verified(
        alive,
        ActionResult(
            "resume", ActionName.RESUME_WORLD, ActionStatus.SUCCEEDED, 0.2, 1, 1, 1
        ),
    )
    resumed_world = replace(
        alive, source_frame_id="movement-goal-resumed-1", source_sequence=11
    )
    assert policy.propose(resumed_world) is None
    final_pause = policy.propose(
        replace(resumed_world, source_frame_id="movement-goal-final-pause", source_sequence=12)
    )
    assert final_pause is not None and final_pause.action == ActionName.PAUSE_WORLD
    policy.on_verified(
        replace(alive, screen=DSTScreen.PAUSED),
        ActionResult(
            "final-pause", ActionName.PAUSE_WORLD, ActionStatus.SUCCEEDED,
            0.2, 1, 1, 1,
        ),
    )
    assert policy.validation_complete


def test_live_wilson_loadout_has_guarded_go_action():
    image = Image.open(ASSETS / "samples/character_loadout_live.png").convert("RGB")
    first = analyze_image(image, "wilson-loadout-1", 1)
    assert first.validity == ObservationValidity.VALID
    assert first.screen == DSTScreen.CHARACTER_LOADOUT
    button = next(
        item for item in first.detections
        if item.kind == "character_loadout_go_button"
    )
    assert button.detected and button.verified and button.confidence >= 0.94
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(first) is None
    second = replace(first, source_frame_id="wilson-loadout-2", source_sequence=2)
    proposal = policy.propose(second)
    assert proposal is not None and proposal.action == ActionName.START_SURVIVOR
    target, _ = click_request(proposal.action, second)
    assert 0.81 < target.x < 0.97 and 0.9 < target.y < 0.98


def test_live_wilson_world_requires_both_hud_anchors():
    for filename in (
        "in_world_wilson_live.png",
        "in_world_wilson_later_live.png",
        "in_world_wilson_reentry_live.png",
    ):
        image = Image.open(ASSETS / "samples" / filename).convert("RGB")
        observation = analyze_image(image, filename, 1)
        assert observation.validity == ObservationValidity.VALID
        assert observation.screen == DSTScreen.IN_WORLD_IDLE
        assert observation.screen_confidence >= 0.94
        assert {"game_hud", "player_marker"} <= {
            item.kind for item in observation.detections if item.detected and item.verified
        }
    for filename in ("character_selection_live.png", "character_loadout_live.png"):
        image = Image.open(ASSETS / "samples" / filename).convert("RGB")
        observation = analyze_image(image, filename, 1)
        assert observation.screen != DSTScreen.IN_WORLD_IDLE


def test_validation_resumes_from_live_world_and_counts_fresh_frames():
    image = Image.open(ASSETS / "samples/in_world_wilson_later_live.png").convert("RGB")
    observation = analyze_image(image, "wilson-world-1", 1)
    policy = ActivityController(validation_flow_enabled=True)
    for sequence in range(1, 5):
        fresh = replace(
            observation,
            source_frame_id=f"wilson-world-{sequence}",
            source_sequence=sequence,
        )
        assert policy.propose(fresh) is None
    fifth = replace(observation, source_frame_id="wilson-world-5", source_sequence=5)
    proposal = policy.propose(fifth)
    assert proposal is not None and proposal.action == ActionName.PAUSE_WORLD
    assert proposal.duration is None


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
    button_only = observe(title=False)
    assert button_only.screen == DSTScreen.LOGIN_REWARD_AVAILABLE
    assert button_only.validity == ObservationValidity.VALID
    button_policy = ActivityController()
    assert button_policy.propose(button_only) is None
    button_proposal = button_policy.propose(
        replace(button_only, source_frame_id="button-only-frame-2")
    )
    assert button_proposal is not None
    assert button_proposal.action == ActionName.CLICK_REWARD_OPEN
    menu_only = observe(button=False, title=False)
    assert menu_only.screen == DSTScreen.UNKNOWN
    main_menu_image = Image.open(ASSETS / "samples/main_menu_after_reward.png").convert(
        "RGB"
    )
    main_menu = analyze_image(main_menu_image, "manual-main-menu", 2)
    assert main_menu.screen == DSTScreen.MAIN_MENU
    assert main_menu.validity == ObservationValidity.VALID


def test_live_reward_result_uses_detected_close_anchor():
    image = Image.open(ASSETS / "samples/login_reward_result_live.png").convert("RGB")
    result = analyze_image(image, "live-reward-result-1", 1)

    assert result.validity == ObservationValidity.VALID
    assert result.screen == DSTScreen.REWARD_RESULT
    close = next(
        item for item in result.detections if item.kind == "login_reward_close_button"
    )
    assert close.detected and close.verified and close.bounds is not None

    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(result) is None  # two-frame state hysteresis
    next_result = replace(
        result,
        source_frame_id="live-reward-result-2",
        source_sequence=2,
        observation_generation=2,
    )
    proposal = policy.propose(next_result)
    assert proposal is not None
    assert proposal.action == ActionName.CLICK_REWARD_CLOSE

    target, viewport = click_request(proposal.action, next_result)
    assert close.bounds.left < target.x < close.bounds.right
    assert close.bounds.top < target.y < close.bounds.bottom
    assert viewport.width == 1280 and viewport.height == 720


def test_real_host_game_caves_prompt_replays_without_menu_confounds(tmp_path):
    real_frame = Image.open(
        ASSETS / "samples/host_game_caves_prompt_live.png"
    ).convert("RGB")
    observation = analyze_image(real_frame, "host-game-caves-prompt-live", 1)

    assert observation.validity == ObservationValidity.VALID
    assert observation.screen == DSTScreen.HOST_GAME_CAVES_PROMPT
    assert observation.screen_confidence >= 0.94
    assert {
        "host_game_caves_prompt_title",
        "host_game_caves_option_caves",
        "host_game_caves_option_no_caves",
        "host_game_caves_back",
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
        if item.kind.startswith("host_game_caves_")
    )
    playstyle = analyze_image(
        Image.open(ASSETS / "samples/host_game_playstyle_live.png").convert("RGB"),
        "playstyle-confounder",
        3,
    )
    assert playstyle.screen == DSTScreen.HOST_GAME_PLAYSTYLE
    assert playstyle.screen != DSTScreen.HOST_GAME_CAVES_PROMPT
    assert not any(
        item.detected
        for item in playstyle.detections
        if item.kind
        in {
            "host_game_caves_prompt_title",
            "host_game_caves_option_caves",
            "host_game_caves_option_no_caves",
        }
    )

    recorder = SessionRecorder(
        tmp_path.resolve(),
        runtime_instance_id="host-game-caves-prompt-test",
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
        frame_id="host-game-caves-prompt-real-frame",
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
            DSTScreen.HOST_GAME_CAVES_PROMPT.value
        )
        assert replayed.details["observation"]["validity"] == "VALID"
        assert worker.input is None
    finally:
        worker.shutdown()


def test_real_host_game_world_list_uses_existing_world_anchor():
    frame = Image.open(
        ASSETS / "samples/host_game_world_list_live.png"
    ).convert("RGB")
    observation = analyze_image(frame, "host-game-world-list-live", 1)

    assert observation.validity == ObservationValidity.VALID
    assert observation.screen == DSTScreen.HOST_GAME_WORLD_LIST
    assert observation.screen_confidence >= 0.94
    detections = {
        item.kind: item
        for item in observation.detections
        if item.detected and item.verified
    }
    assert {
        "host_game_playstyle_title",
        "host_game_world_list_search",
        "host_game_world_list_create_new",
        "host_game_existing_world_row",
    } <= detections.keys()
    row = detections["host_game_existing_world_row"]
    assert row.bounds is not None and row.confidence >= 0.94
    assert DSTScreen.HOST_GAME_WORLD_LIST != DSTScreen.MAIN_MENU
    assert DSTScreen.HOST_GAME_WORLD_LIST != DSTScreen.HOST_GAME_PLAYSTYLE


def test_selected_saved_world_is_detected_and_targets_resume_button():
    frame = Image.open(
        ASSETS / "samples/host_game_world_selected_live.png"
    ).convert("RGB")
    observation = analyze_image(frame, "selected-world-live-1", 1)

    assert observation.validity == ObservationValidity.VALID
    assert observation.screen == DSTScreen.HOST_GAME_WORLD_SELECTED
    assert observation.screen_confidence >= 0.94
    detections = {
        item.kind: item
        for item in observation.detections
        if item.detected and item.verified
    }
    assert {
        "host_game_playstyle_title",
        "host_game_world_selected_name",
        "host_game_world_selected_start",
    } <= detections.keys()

    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(observation) is None
    proposal = policy.propose(
        replace(observation, source_frame_id="selected-world-live-2", source_sequence=2)
    )
    assert proposal is not None
    assert proposal.action == ActionName.START_EXISTING_WORLD
    target, viewport = click_request(proposal.action, observation)
    assert 0.75 <= target.x <= 0.91
    assert 0.92 <= target.y <= 0.99
    assert viewport.width == 1280 and viewport.height == 720


def test_live_world_loading_and_survivor_select_states_are_detected():
    loading = analyze_image(
        Image.open(ASSETS / "samples/dst_world_loading_live.png").convert("RGB"),
        "live-world-loading", 1,
    )
    assert loading.validity == ObservationValidity.VALID
    assert loading.screen == DSTScreen.LOADING
    assert loading.screen_confidence >= 0.94
    loading_anchor = next(
        item for item in loading.detections if item.kind == "loading_label"
    )
    assert loading_anchor.detected and loading_anchor.verified

    animated_loading = analyze_image(
        Image.open(
            ASSETS / "samples/dst_world_loading_animated_live.png"
        ).convert("RGB"),
        "live-world-loading-animated",
        3,
    )
    assert animated_loading.validity == ObservationValidity.VALID
    assert animated_loading.screen == DSTScreen.LOADING
    assert animated_loading.screen_confidence >= 0.94
    loading_anchor = next(
        item for item in animated_loading.detections if item.kind == "loading_label"
    )
    assert loading_anchor.detected and loading_anchor.verified

    character_select = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "live-character-selection", 4,
    )
    assert character_select.validity == ObservationValidity.VALID
    assert character_select.screen == DSTScreen.CHARACTER_SELECTION
    assert character_select.screen_confidence >= 0.94
    assert {
        "character_select_title",
        "character_select_players",
        "character_select_wilson_name",
        "character_select_wilson_icon",
    } <= {
        item.kind for item in character_select.detections
        if item.detected and item.verified
    }
    assert not next(
        item for item in character_select.detections
        if item.kind == "character_loadout_wilson_name"
    ).detected


def test_validation_activity_enters_host_game_from_main_menu():
    policy = ActivityController(validation_flow_enabled=True)
    menu_frame = Image.open(
        ASSETS / "samples/main_menu_after_reward.png"
    ).convert("RGB")
    first = analyze_image(menu_frame, "validation-main-menu-1", 1)
    assert first.screen == DSTScreen.MAIN_MENU
    assert policy.propose(first) is None
    menu = replace(first, source_frame_id="validation-main-menu-2", source_sequence=2)
    host = policy.propose(menu)
    assert host is not None and host.action == ActionName.CLICK_HOST_GAME

    verifying = ActionResult(
        "click-host-game", ActionName.CLICK_HOST_GAME,
        ActionStatus.VERIFYING, 0.01, 1, 1, 1,
    )
    policy.on_action_result(menu, verifying)
    world_list = analyze_image(
        Image.open(ASSETS / "samples/host_game_world_list_live.png").convert("RGB"),
        "validation-world-list-1", 7,
    )
    policy.on_verified(
        world_list, replace(verifying, status=ActionStatus.SUCCEEDED)
    )
    assert policy.propose(world_list) is None
    selected = policy.propose(
        replace(world_list, source_frame_id="validation-world-list-2", source_sequence=8)
    )
    assert selected is not None
    assert selected.action == ActionName.SELECT_EXISTING_WORLD

    verifying = ActionResult(
        "select-existing", ActionName.SELECT_EXISTING_WORLD,
        ActionStatus.VERIFYING, 0.01, 1, 1, 1,
    )
    policy.on_action_result(world_list, verifying)
    loading = replace(
        world_list, screen=DSTScreen.LOADING,
        source_frame_id="validation-loading", source_sequence=9,
    )
    policy.on_verified(loading, replace(verifying, status=ActionStatus.SUCCEEDED))
    in_world = replace(
        loading, screen=DSTScreen.IN_WORLD_IDLE,
        source_frame_id="validation-world-1", source_sequence=10,
    )
    assert policy.propose(in_world) is None
    assert policy.propose(
        replace(in_world, source_frame_id="validation-world-2", source_sequence=11)
    ) is None
    assert not policy.validation_complete
    for sequence in range(12, 14):
        assert policy.propose(
            replace(
                in_world,
                source_frame_id=f"validation-world-{sequence}",
                source_sequence=sequence,
            )
        ) is None
    pause = policy.propose(replace(
        in_world, source_frame_id="validation-world-14", source_sequence=14,
    ))
    assert pause is not None and pause.action == ActionName.PAUSE_WORLD


def test_host_game_timeout_retries_only_from_fresh_unchanged_verified_menu():
    menu_frame = Image.open(
        ASSETS / "samples/main_menu_after_reward.png"
    ).convert("RGB")
    menu = analyze_image(menu_frame, "host-retry-1", 1)
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_frame_id="host-retry-2", source_sequence=2)
    first = policy.propose(menu)
    assert first is not None and first.action == ActionName.CLICK_HOST_GAME

    timed_out = ActionResult(
        "host-first", ActionName.CLICK_HOST_GAME, ActionStatus.TIMED_OUT,
        45.0, 1, 1, 1, "verified transition deadline elapsed",
    )
    policy.on_action_failure(timed_out)
    assert not policy.intervention_required

    fresh_menu = replace(
        menu, source_frame_id="host-retry-3", source_sequence=3,
        screen_change=0.002,
    )
    retry = policy.propose(fresh_menu)
    assert retry is not None and retry.action == ActionName.CLICK_HOST_GAME
    assert "once" in (retry.reason or "")

    policy.on_action_failure(replace(timed_out, action_id="host-second"))
    assert policy.intervention_required
    assert policy.propose(
        replace(fresh_menu, source_frame_id="host-retry-4", source_sequence=4)
    ) is None


def test_host_game_timeout_does_not_retry_after_source_state_changes():
    menu_frame = Image.open(
        ASSETS / "samples/main_menu_after_reward.png"
    ).convert("RGB")
    menu = analyze_image(menu_frame, "host-change-1", 1)
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_frame_id="host-change-2", source_sequence=2)
    assert policy.propose(menu).action == ActionName.CLICK_HOST_GAME
    policy.on_action_failure(ActionResult(
        "host-timeout", ActionName.CLICK_HOST_GAME, ActionStatus.TIMED_OUT,
        45.0, 1, 1, 1, "verified transition deadline elapsed",
    ))

    world_list = analyze_image(
        Image.open(ASSETS / "samples/host_game_world_list_live.png").convert("RGB"),
        "host-change-list-1", 3,
    )
    assert policy.propose(world_list) is None
    assert policy.propose(
        replace(world_list, source_frame_id="host-change-list-2", source_sequence=4)
    ) is None
    assert policy.intervention_required


def test_validation_selects_survivor_to_open_loadout_panel():
    character = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "validation-character-1",
        1,
    )
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(character) is None
    selected = replace(
        character,
        source_frame_id="validation-character-2",
        source_sequence=2,
        observation_generation=2,
    )
    proposal = policy.propose(selected)
    assert proposal is not None
    assert proposal.action == ActionName.SELECT_SURVIVOR

    verifying = ActionResult(
        "select-survivor",
        ActionName.SELECT_SURVIVOR,
        ActionStatus.VERIFYING,
        0.01,
        1,
        1,
        1,
    )
    policy.on_action_result(selected, verifying)
    loadout = replace(
        selected,
        screen=DSTScreen.CHARACTER_LOADOUT,
        source_frame_id="validation-character-loadout",
        source_sequence=3,
        observation_generation=3,
    )
    policy.on_verified(loadout, replace(verifying, status=ActionStatus.SUCCEEDED))
    next_step = policy.propose(
        replace(
            loadout,
            source_frame_id="validation-character-loadout-2",
            source_sequence=4,
            observation_generation=4,
        )
    )
    assert next_step is None


def test_survivor_timeout_retries_once_from_fresh_unchanged_hover():
    image = Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB")
    selection = analyze_image(image, "survivor-1", 1)
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(selection) is None
    assert policy.propose(replace(
        selection, source_frame_id="survivor-2", source_sequence=2,
    )).action == ActionName.SELECT_SURVIVOR
    policy.on_action_failure(ActionResult(
        "survivor-timeout", ActionName.SELECT_SURVIVOR, ActionStatus.TIMED_OUT,
        30.0, 1, 1, 1, "verified transition deadline elapsed",
    ))
    image.paste(
        Image.open(ASSETS / "character_select_wilson_hover.png").convert("RGB"),
        (376, 135),
    )
    hover = replace(
        analyze_image(image, "survivor-hover-3", 3), screen_change=.001,
    )
    assert hover.screen == DSTScreen.CHARACTER_SELECTION_HOVERED
    assert policy.propose(hover) is None
    retry = policy.propose(replace(
        hover, source_frame_id="survivor-hover-4", source_sequence=4,
    ))
    assert retry is not None and retry.action == ActionName.SELECT_SURVIVOR
    policy.on_action_failure(ActionResult(
        "survivor-timeout-2", ActionName.SELECT_SURVIVOR,
        ActionStatus.TIMED_OUT, 30.0, 1, 1, 1,
        "verified transition deadline elapsed",
    ))
    assert policy.intervention_required


def test_late_verified_loadout_cancels_survivor_retry():
    selection = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "late-selection-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(selection) is None
    assert policy.propose(replace(
        selection, source_frame_id="late-selection-2", source_sequence=2,
    )).action == ActionName.SELECT_SURVIVOR
    policy.on_action_failure(ActionResult(
        "late-timeout", ActionName.SELECT_SURVIVOR, ActionStatus.TIMED_OUT,
        30.0, 1, 1, 1, "verified transition deadline elapsed",
    ))
    loadout = analyze_image(
        Image.open(ASSETS / "samples/character_loadout_live.png").convert("RGB"),
        "late-loadout-3", 3,
    )
    assert policy.propose(loadout) is None
    assert policy.propose(replace(
        loadout, source_frame_id="late-loadout-4", source_sequence=4,
    )) is None
    assert policy._validation_step == 4
    assert not policy.intervention_required


def test_world_present_banner_does_not_imply_gift_available():
    image = Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB")
    image.paste(
        Image.open(ASSETS / "world_present_banner.png").convert("RGB"),
        (168, 10),
    )
    observation = analyze_image(image, "world-present-banner", 1)
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    assert any(
        item.kind == "world_present_banner" and item.detected and item.verified
        for item in observation.detections
    )


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
        "mouse_move",
        "focus_game",
        "mouse_down",
        "mouse_up",
    ]
