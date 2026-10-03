from __future__ import annotations

import json
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
from runtime_agent.gameworker.activity import ActivityController, DailyGiftState
from runtime_agent.gameworker.base import WorkerContext
from runtime_agent.gameworker.capture import Frame
from runtime_agent.gameworker.config import InputBindings, WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.fixed_ui import DST_FIXED_1280X720
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
from runtime_agent.gameworker.perception import PipelineOutcome
from runtime_agent.gameworker.recording import (
    RecordingEventType,
    RecordingLimits,
    SessionRecorder,
)
from runtime_agent.gameworker.state import WorkerState
from runtime_agent.gameworker.transitions import ActionLifecycle, click_request
from runtime_agent.gameworker.vision import (
    AssetRegistry,
    Detection,
    DSTScreen,
    ObservationValidity,
    VisionDetector,
)

ASSETS = Path(__file__).resolve().parents[2] / "runtime_agent/gameworker/dst/assets"


def test_promotional_reward_next_uses_verified_anchor_and_bounded_policy():
    image = Image.open(ASSETS / "samples/login_reward_result_live.png").convert("RGB")
    image.paste("black", (448, 540, 832, 700))
    image.paste(Image.open(ASSETS / "login_reward_next_button.png"), (875, 367))
    observation = analyze_image(image, "promo-1", 1)
    assert observation.screen == DSTScreen.REWARD_RESULT
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(observation) is None
    fresh = replace(observation, source_frame_id="promo-2", source_sequence=2)
    proposal = policy.propose(fresh)
    assert proposal.action == ActionName.CLICK_REWARD_NEXT
    point, _ = click_request(proposal.action, fresh)
    assert .675 < point.x < .75
    assert policy.propose(replace(fresh, source_sequence=3)).action == ActionName.CLICK_REWARD_NEXT
    assert policy.propose(replace(fresh, source_sequence=4)) is None


def test_world_hud_allows_occupied_starting_inventory_slots():
    image = Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB")
    image.paste("black", (179, 662, 335, 720))
    observation = analyze_image(image, "occupied-starting-slots", 1)
    assert observation.production_ready
    assert observation.screen == DSTScreen.IN_WORLD_IDLE


def test_fresh_clean_real_hud_reports_no_gift_when_verified_gift_template_is_absent():
    # This is a retained real DST capture, not a rendered/synthetic screenshot.
    image = Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB")
    observation = analyze_image(
        image, "clean-hud-negative-gift", 1,
        profile_id="dst-1280x720-linux-v1",
    )
    assert observation.production_ready
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    gift = next(item for item in observation.detections if item.kind == "gift_icon")
    assert gift.verified
    assert not gift.detected
    assert dict(gift.metadata)["availability"] == "NO_REWARD_AVAILABLE"
    assert dict(gift.metadata)["negative_evidence"] == "VERIFIED_TEMPLATE_ABSENCE"


def test_real_perception_corpus_replays_ground_truth():
    corpus_path = ASSETS / "samples/perception_corpus.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))
    assert corpus["schema_version"] == 1
    assert corpus["fixtures"]
    for fixture in corpus["fixtures"]:
        image_path = ASSETS / "samples" / fixture["image"]
        observation = analyze_image(
            Image.open(image_path).convert("RGB"),
            fixture["id"],
            1,
            profile_id="dst-1280x720-linux-v1",
        )
        assert observation.validity == ObservationValidity.VALID, fixture["id"]
        assert observation.screen.value == fixture["expected_state"], fixture["id"]


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


def test_character_selection_recognizes_wilson_target_without_nameplate(monkeypatch):
    original_detect = VisionDetector.detect

    def hide_wilson_name(self, image, template_id, *, deadline=None):
        if template_id == "character_select_wilson_name":
            return Detection(template_id, False, 0.0, verified=True)
        return original_detect(self, image, template_id, deadline=deadline)

    monkeypatch.setattr(VisionDetector, "detect", hide_wilson_name)
    screen = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "character-select-wilson-target",
        1,
    )
    assert screen.screen == DSTScreen.CHARACTER_SELECTION
    assert screen.validity == ObservationValidity.VALID
    assert screen.screen_confidence >= 0.94
    assert "character_select_wilson_name" not in {
        item.kind for item in screen.detections if item.detected
    }


def test_partial_character_selection_anchors_remain_unknown(monkeypatch):
    original_detect = VisionDetector.detect

    def hide_title(self, image, template_id, *, deadline=None):
        if template_id == "character_select_title":
            return Detection(template_id, False, 0.0, verified=True)
        return original_detect(self, image, template_id, deadline=deadline)

    monkeypatch.setattr(VisionDetector, "detect", hide_title)
    screen = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "partial-character-select",
        1,
    )
    assert screen.screen == DSTScreen.UNKNOWN
    assert screen.production_ready is False


def test_validation_disabled_does_not_propose_validation_actions_in_world():
    alive = analyze_image(
        Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB"),
        "ordinary-world-1",
        1,
    )
    policy = ActivityController(validation_flow_enabled=False)

    proposal = None
    for sequence in range(1, 8):
        proposal = policy.propose(
            replace(
                alive,
                source_frame_id=f"ordinary-world-{sequence}",
                source_sequence=sequence,
            )
        )

    assert proposal is None
    assert policy._validation_step == 0


def test_pending_inworld_gift_waits_for_native_claim_evidence():
    observation = analyze_image(
        Image.open(ASSETS / "samples/gift_icon_gray_in_world_live.png").convert("RGB"),
        "gift-station-approach-1",
        1,
    )
    present = next(
        item for item in observation.detections if item.kind == "world_present_banner"
    )
    pending = Detection(
        "gift_icon",
        True,
        present.confidence,
        bounds=present.bounds,
        detector_id="gift-icon-test",
        verified=True,
        metadata=(("availability", "IN_WORLD_GIFT_PENDING"),),
    )
    observation = replace(observation, detections=observation.detections + (pending,))
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)

    policy.gift_claim_ready = False
    assert policy.propose(observation) is None
    assert policy.propose(replace(
        observation, source_frame_id="pending-not-ready", source_sequence=2,
    )) is None
    assert policy._gift_icon_click_attempts == 0
    assert policy.inworld_gift_confirmation is None


def test_active_production_world_entry_uses_fixed_profile_targets_without_validation_step():
    cases = (
        ("main_menu_after_reward.png", ActionName.CLICK_HOST_GAME, "HOST_GAME"),
        ("host_game_world_list_live.png", ActionName.SELECT_EXISTING_WORLD, "FARM_01"),
        ("host_game_world_selected_live.png", ActionName.START_EXISTING_WORLD, "RESUME_WORLD"),
        ("character_selection_live.png", ActionName.SELECT_SURVIVOR, "WILSON"),
        ("character_loadout_live.png", ActionName.START_SURVIVOR, "START_SURVIVOR"),
    )
    for index, (filename, action, profile_target) in enumerate(cases, start=1):
        observation = analyze_image(
            Image.open(ASSETS / "samples" / filename).convert("RGB"),
            f"production-entry-{index}-1",
            index * 10,
        )
        policy = ActivityController(validation_flow_enabled=False)
        policy.set_production_actions_enabled(True)
        policy._validation_step = 77

        assert policy.propose(observation) is None
        stable = replace(
            observation,
            source_frame_id=f"production-entry-{index}-2",
            source_sequence=observation.source_sequence + 1,
        )
        proposal = policy.propose(stable)

        assert proposal is not None and proposal.action == action
        point, viewport = click_request(proposal.action, stable)
        assert point == DST_FIXED_1280X720.point(profile_target, 1280, 720)
        assert viewport.width == 1280 and viewport.height == 720
        assert policy._validation_step == 77


def test_production_confirms_only_the_verified_mods_disabled_dialog():
    image = Image.new("RGB", (1280, 720), "black")
    image.paste(Image.open(ASSETS / "mods_disabled_title.png"), (490, 64))
    image.paste(Image.open(ASSETS / "mods_disabled_continue.png"), (424, 553))
    modal = analyze_image(image, "mods-disabled-1", 60)
    assert modal.screen == DSTScreen.MODS_DISABLED_CONFIRMATION
    assert modal.screen_confidence >= 0.94

    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(modal) is None
    proposal = policy.propose(
        replace(modal, source_frame_id="mods-disabled-2", source_sequence=61)
    )
    assert proposal is not None
    assert proposal.action == ActionName.CONFIRM_MODS_DISABLED
    point, viewport = click_request(proposal.action, modal)
    assert point == DST_FIXED_1280X720.point("MODS_DISABLED_CONTINUE", 1280, 720)
    assert viewport.width == 1280 and viewport.height == 720

    title_only = Image.new("RGB", (1280, 720), "black")
    title_only.paste(Image.open(ASSETS / "mods_disabled_title.png"), (490, 64))
    assert analyze_image(title_only, "not-mods-disabled", 62).screen == DSTScreen.UNKNOWN


def test_production_world_entry_ignores_non_entry_states_and_requires_active_gate():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "production-gate-menu", 1,
    )
    disabled = ActivityController(validation_flow_enabled=False)
    assert disabled.propose(menu) is None
    assert disabled.propose(replace(menu, source_sequence=2, source_frame_id="menu-2")) is None

    cases = (
        ("dst_world_loading_live.png", DSTScreen.LOADING),
        ("in_world_wilson_live.png", DSTScreen.IN_WORLD_IDLE),
        ("in_world_auto_paused_live.png", DSTScreen.PAUSED),
        ("death_world_reset_live.png", DSTScreen.WORLD_RESET_PENDING),
    )
    for index, (filename, screen) in enumerate(cases, start=10):
        observation = analyze_image(
            Image.open(ASSETS / "samples" / filename).convert("RGB"),
            f"production-non-entry-{index}-1",
            index * 10,
        )
        assert observation.screen == screen
        policy = ActivityController(validation_flow_enabled=False)
        policy.set_production_actions_enabled(True)
        assert policy.propose(observation) is None
        assert policy.propose(replace(
            observation,
            source_sequence=observation.source_sequence + 1,
            source_frame_id=f"production-non-entry-{index}-2",
        )) is None

    dead = replace(menu, screen=DSTScreen.DEAD, screen_confidence=0.99)
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(dead) is None
    assert policy.propose(replace(
        dead, source_sequence=2, source_frame_id="production-dead-2",
    )) is None

    unknown = replace(menu, screen=DSTScreen.UNKNOWN, screen_confidence=0.0)
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(unknown) is None
    assert policy.propose(replace(
        unknown, source_sequence=2, source_frame_id="production-unknown-2",
    )) is None


def test_production_policy_holds_while_action_verification_is_in_flight():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "production-flight-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(menu) is None
    stable = replace(menu, source_sequence=2, source_frame_id="production-flight-2")
    proposal = policy.propose(stable)
    assert proposal is not None and proposal.action == ActionName.CLICK_HOST_GAME
    policy.on_action_result(stable, ActionResult(
        "production-host", ActionName.CLICK_HOST_GAME,
        ActionStatus.VERIFYING, 0.01, 1, 1, 1,
    ))
    assert policy.propose(replace(
        stable, source_sequence=3, source_frame_id="production-flight-3",
    )) is None


def test_validation_route_owns_proposal_when_production_gate_is_also_enabled():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "validation-exclusive-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=True)
    policy.set_production_actions_enabled(True)
    assert policy.propose(menu) is None
    proposal = policy.propose(replace(
        menu, source_sequence=2, source_frame_id="validation-exclusive-2",
    ))
    assert proposal is not None and proposal.action == ActionName.CLICK_HOST_GAME
    assert len(policy.decisions) == 2


def test_production_host_game_retry_is_one_shot_and_keeps_the_profile_point():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "production-host-retry-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_sequence=2, source_frame_id="production-host-retry-2")
    first = policy.propose(menu)
    assert first is not None and first.action == ActionName.CLICK_HOST_GAME
    first_point, _ = click_request(first.action, menu)

    no_effect = replace(menu, source_sequence=3, source_frame_id="production-host-no-effect")
    policy.on_verified(no_effect, ActionResult(
        "production-host-first", ActionName.CLICK_HOST_GAME,
        ActionStatus.TIMED_OUT, 0.5, 1, 1, 1,
        "fresh unchanged MAIN_MENU proves Host Game click had no effect",
    ))
    retry_frame = replace(no_effect, source_sequence=4, source_frame_id="production-host-retry-3")
    retry = policy.propose(retry_frame)
    assert retry is not None and retry.action == ActionName.CLICK_HOST_GAME
    assert click_request(retry.action, retry_frame)[0] == first_point

    policy.on_verified(retry_frame, ActionResult(
        "production-host-second", ActionName.CLICK_HOST_GAME,
        ActionStatus.TIMED_OUT, 0.5, 1, 1, 1,
        "fresh unchanged MAIN_MENU proves Host Game click had no effect",
    ))
    assert policy.intervention_required


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


def test_live_idle_world_accepts_normal_health_marker_variation():
    image = Image.open(
        ASSETS / "samples/in_world_idle_player_marker_variation_live.png"
    ).convert("RGB")
    observation = analyze_image(image, "live-player-marker-variation", 1)

    assert observation.validity == ObservationValidity.VALID
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    assert observation.screen_confidence >= 0.75
    marker = next(item for item in observation.detections if item.kind == "player_marker")
    assert marker.detected and marker.verified
    assert 0.75 <= marker.confidence < 0.94
    from app.services.executor import JobExecutor

    assert observation.screen_confidence >= 0.94
    assert JobExecutor._safe_observation(observation.as_dict())
    for key in ("validity", "calibration_verified", "assets_verified", "screen_confidence"):
        malformed = {**observation.as_dict(), "production_ready": True}
        malformed.pop(key)
        assert not JobExecutor._safe_observation(malformed)
    for changes in (
        {"validity": "UNKNOWN"}, {"validity": "STALE"},
        {"validity": "INVALID"}, {"screen": "UNKNOWN"},
        {"screen": "DEAD"}, {"screen": "WORLD_RESET_PENDING"},
        {"screen": "RESET_PENDING"}, {"calibration_verified": False},
        {"assets_verified": False}, {"screen_confidence": float("nan")},
        {"screen_confidence": None},
    ):
        assert not JobExecutor._safe_observation({**observation.as_dict(), "production_ready": True, **changes})


def test_live_player_marker_false_negative_uses_independent_world_anchors():
    image = Image.open(
        ASSETS / "samples/in_world_marker_false_negative_live.png"
    ).convert("RGB")
    observation = analyze_image(image, "live-marker-false-negative", 1)
    detections = {item.kind: item for item in observation.detections}

    # The frame is the human-inspected live failure. The local replay score is
    # lower than the production observation's 0.657998, but the same marker
    # appearance is below its unchanged 0.75 template threshold.
    assert 0.60 < detections["player_marker"].confidence < 0.75
    assert not detections["player_marker"].detected
    assert detections["game_hud"].detected and detections["game_hud"].verified
    assert detections["world_present_banner"].detected
    assert detections["world_present_banner"].verified
    assert observation.validity == ObservationValidity.VALID
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    assert observation.screen_confidence >= 0.94


def test_invalid_player_marker_without_second_world_anchor_remains_unknown(monkeypatch):
    from app.services.executor import JobExecutor

    original = VisionDetector.detect

    def invalidate_world_anchors(self, image, template_id, *, deadline=None):
        item = original(self, image, template_id, deadline=deadline)
        if template_id == "player_marker":
            return replace(item, confidence=0.657998, detected=False)
        if template_id in {"world_present_banner", "world_inventory_frame"}:
            return replace(item, detected=False)
        return item

    monkeypatch.setattr(VisionDetector, "detect", invalidate_world_anchors)
    observation = analyze_image(
        Image.open(
            ASSETS / "samples/in_world_marker_false_negative_live.png"
        ).convert("RGB"),
        "ambiguous-world-anchors", 1,
    )
    assert observation.screen == DSTScreen.UNKNOWN
    assert not observation.valid
    assert not JobExecutor._safe_observation(observation.as_dict())


def test_reset_evidence_keeps_precedence_over_world_anchor_fallback(monkeypatch):
    original = VisionDetector.detect
    failure_frame = Image.open(
        ASSETS / "samples/in_world_marker_false_negative_live.png"
    ).convert("RGB")
    reset_frame = Image.open(
        ASSETS / "samples/death_world_reset_live.png"
    ).convert("RGB")

    def with_reset_evidence(self, image, template_id, *, deadline=None):
        if template_id in {"death_world_reset_text", "death_reset_now_button"}:
            return original(self, reset_frame, template_id, deadline=deadline)
        return original(self, image, template_id, deadline=deadline)

    monkeypatch.setattr(VisionDetector, "detect", with_reset_evidence)
    observation = analyze_image(failure_frame, "world-anchors-plus-reset", 1)
    assert observation.screen == DSTScreen.WORLD_RESET_PENDING
    assert observation.validity == ObservationValidity.VALID


def test_verified_low_confidence_world_can_source_and_verify_action():
    from app.services.executor import JobExecutor
    from runtime_agent.gameworker.transitions import action_precondition_error

    observation = analyze_image(
        Image.open(ASSETS / "samples/in_world_idle_player_marker_variation_live.png").convert("RGB"),
        "low-confidence-action", 1,
    )
    assert action_precondition_error(ActionName.MOVE_FORWARD, observation) is None
    assert JobExecutor._safe_observation(observation.as_dict())
    lifecycle = ActionLifecycle(clock=lambda: observation.observed_monotonic)
    lifecycle.begin(ActionResult(
        "move-low-confidence", ActionName.MOVE_FORWARD, ActionStatus.SENT,
        0.1, 1, 1, 1,
    ), observation)
    for sequence in (2, 3):
        result = lifecycle.observe(replace(
            observation, source_sequence=sequence, source_frame_id=str(sequence),
            gameplay_change=0.02,
        ))
    assert result is not None and result.status == ActionStatus.SUCCEEDED


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


def analyze_image(
    image: Image.Image,
    frame_id: str,
    sequence: int,
    *,
    profile_id: str = "dst",
):
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
        calibration=CalibrationProfile(profile_id, 1, 1280, 720, verified=True),
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


def test_host_game_unchanged_menu_allows_exactly_one_guarded_retry():
    menu_frame = Image.open(
        ASSETS / "samples/main_menu_after_reward.png"
    ).convert("RGB")
    menu = analyze_image(menu_frame, "host-retry-1", 1)
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_frame_id="host-retry-2", source_sequence=2)
    first = policy.propose(menu)
    assert first is not None and first.action == ActionName.CLICK_HOST_GAME
    first_point, _ = click_request(first.action, menu)

    timed_out = ActionResult(
        "host-first", ActionName.CLICK_HOST_GAME, ActionStatus.TIMED_OUT,
        0.5, 1, 1, 1,
        "fresh unchanged MAIN_MENU proves Host Game click had no effect",
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
    retry_point, _ = click_request(retry.action, fresh_menu)
    assert retry_point == first_point

    policy.on_action_failure(replace(timed_out, action_id="host-second"))
    assert policy.intervention_required
    assert policy.propose(
        replace(fresh_menu, source_frame_id="host-retry-4", source_sequence=4)
    ) is None


def test_host_game_retry_is_available_without_validation_flow_and_without_anchor_localization():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "host-profile-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    policy.on_verified(menu, ActionResult(
        "host-proven-no-effect", ActionName.CLICK_HOST_GAME,
        ActionStatus.TIMED_OUT, 0.5, 1, 1, 1,
        "fresh unchanged MAIN_MENU proves Host Game click had no effect",
    ))
    retry_frame = replace(
        menu, source_frame_id="host-profile-2", source_sequence=2,
        screen_change=0.001, detections=(),
    )
    assert policy.propose(retry_frame) is None
    retry_frame = replace(
        retry_frame, source_frame_id="host-profile-3", source_sequence=3,
    )
    retry = policy.propose(retry_frame)
    assert retry is not None and retry.action == ActionName.CLICK_HOST_GAME


def test_late_host_game_transition_recovers_to_world_list_without_repeating_click():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "late-host-menu-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_frame_id="late-host-menu-2", source_sequence=2)
    assert policy.propose(menu).action == ActionName.CLICK_HOST_GAME
    timeout = ActionResult(
        "host-first-timeout", ActionName.CLICK_HOST_GAME, ActionStatus.TIMED_OUT,
        30.0, 1, 1, 1, "verified transition deadline elapsed",
    )
    policy.on_action_failure(timeout)
    retry_frame = replace(
        menu, source_frame_id="late-host-menu-3", source_sequence=3,
        screen_change=0.001,
    )
    assert policy.propose(retry_frame).action == ActionName.CLICK_HOST_GAME
    policy.on_action_failure(replace(timeout, action_id="host-retry-timeout"))
    assert policy.intervention_required

    world_list = analyze_image(
        Image.open(ASSETS / "samples/host_game_world_list_live.png").convert("RGB"),
        "late-host-world-list-4", 4,
    )
    assert world_list.screen == DSTScreen.HOST_GAME_WORLD_LIST
    assert policy.propose(world_list) is None
    assert not policy.resolve_recoverable_intervention(
        world_list, no_action_in_flight=True, input_released=True,
    )
    world_list = replace(
        world_list, source_frame_id="late-host-world-list-5", source_sequence=5,
    )
    assert policy.propose(world_list) is None
    assert policy.resolve_recoverable_intervention(
        world_list, no_action_in_flight=True, input_released=True,
    )
    assert policy.production_actions_enabled
    farm = policy.propose(replace(
        world_list, source_frame_id="late-host-world-list-6", source_sequence=6,
    ))
    assert farm is not None and farm.action == ActionName.SELECT_EXISTING_WORLD
    assert not policy.intervention_required


def test_recoverable_intervention_requires_safe_fresh_state_and_released_input():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "recoverable-menu-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    policy.state = DSTScreen.MAIN_MENU
    policy._candidate = DSTScreen.MAIN_MENU
    policy._candidate_frames = 2
    policy.intervention_required = True
    policy._recoverable_intervention_action = ActionName.CLICK_HOST_GAME
    assert not policy.resolve_recoverable_intervention(
        menu, no_action_in_flight=False, input_released=True,
    )
    assert not policy.resolve_recoverable_intervention(
        menu, no_action_in_flight=True, input_released=False,
    )
    unknown = replace(
        menu, validity=ObservationValidity.UNKNOWN, screen=DSTScreen.UNKNOWN,
    )
    assert not policy.resolve_recoverable_intervention(
        unknown, no_action_in_flight=True, input_released=True,
    )
    loading = analyze_image(
        Image.open(ASSETS / "samples/dst_world_loading_live.png").convert("RGB"),
        "recoverable-loading-2", 2,
    )
    assert policy.propose(loading) is None
    assert policy.propose(replace(
        loading, source_frame_id="recoverable-loading-3", source_sequence=3,
    )) is None
    assert not policy.resolve_recoverable_intervention(
        loading, no_action_in_flight=True, input_released=True,
    )
    assert policy.intervention_required


def test_recovery_accepts_later_contract_continuation_and_rejects_rewind():
    selection = analyze_image(
        Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB"),
        "recoverable-selection-1", 1,
    )
    assert selection.screen == DSTScreen.CHARACTER_SELECTION
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    policy.state = selection.screen
    policy._candidate = selection.screen
    policy._candidate_frames = 2
    policy.intervention_required = True
    policy._recoverable_intervention_action = ActionName.SELECT_EXISTING_WORLD
    assert policy.resolve_recoverable_intervention(
        selection, no_action_in_flight=True, input_released=True,
    )

    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "recoverable-rewind-1", 1,
    )
    policy.state = menu.screen
    policy._candidate = menu.screen
    policy._candidate_frames = 2
    policy.intervention_required = True
    policy._recoverable_intervention_action = ActionName.START_EXISTING_WORLD
    assert not policy.resolve_recoverable_intervention(
        menu, no_action_in_flight=True, input_released=True,
    )
    assert policy.intervention_required


def test_timed_out_source_action_is_not_replayed_and_disabled_worker_stays_disabled():
    from runtime_agent.gameworker.dst.worker import DSTGameWorker

    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "same-source-menu-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    policy.state = DSTScreen.MAIN_MENU
    policy._candidate = DSTScreen.MAIN_MENU
    policy._candidate_frames = 2
    policy.intervention_required = True
    policy._recoverable_intervention_action = ActionName.CLICK_HOST_GAME
    assert not policy.resolve_recoverable_intervention(
        menu, no_action_in_flight=True, input_released=True,
    )
    assert policy.intervention_required
    assert policy.propose(menu) is None

    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.DISABLED))
    worker.context = WorkerContext(1, 1, DisplayEnvironment(":99"), runtime_verified=True)
    worker.activity.intervention_required = True
    report = worker.tick(worker.context)
    assert worker.mode == WorkerMode.DISABLED
    assert report.mode == WorkerMode.DISABLED


def test_worker_recovers_late_known_state_without_mode_command():
    worker = DSTGameWorker(WorkerConfig(
        plugin="dst", mode=WorkerMode.ACTIVE, validation_flow_enabled=False,
    ))
    worker.context = WorkerContext(1, 1, DisplayEnvironment(":99"), runtime_verified=True)
    worker._game_ready = True
    worker.capture = object()
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test runtime ready")
    worker.machine.transition(WorkerState.OBSERVING, "test worker active")
    worker._unknown_since = None

    class Actions:
        safety = None

        def set_safety(self, **values):
            self.safety = values

    worker.actions = Actions()
    policy = worker.activity
    policy.set_production_actions_enabled(True)
    policy.state = DSTScreen.HOST_GAME_WORLD_LIST
    policy._candidate = DSTScreen.HOST_GAME_WORLD_LIST
    policy._candidate_frames = 2
    policy.intervention_required = True
    policy._recoverable_intervention_action = ActionName.CLICK_HOST_GAME
    assert worker.mode == WorkerMode.ACTIVE
    assert worker._effective_mode() == WorkerMode.OBSERVE

    world_list = analyze_image(
        Image.open(ASSETS / "samples/host_game_world_list_live.png").convert("RGB"),
        "worker-late-world-list-1", 1,
    )
    assert policy.resolve_recoverable_intervention(
        world_list, no_action_in_flight=True, input_released=True,
    )
    worker._sync_action_mode()
    assert worker.mode == WorkerMode.ACTIVE
    assert worker._effective_mode() == WorkerMode.ACTIVE
    assert worker.actions.safety["configured_mode"] == WorkerMode.ACTIVE
    assert worker.actions.safety["effective_mode"] == WorkerMode.ACTIVE

    next_observation = replace(
        world_list, source_frame_id="worker-late-world-list-2", source_sequence=2,
    )
    proposal = policy.propose(next_observation)
    assert proposal is not None and proposal.action == ActionName.SELECT_EXISTING_WORLD


def test_worker_releases_inputs_on_first_uncertain_observation():
    unknown = replace(
        analyze_image(
            Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
            "uncertain-worker-observation",
            1,
        ),
        validity=ObservationValidity.UNKNOWN,
        screen=DSTScreen.UNKNOWN,
        screen_confidence=0.0,
    )

    class Actions:
        def __init__(self):
            self.releases = 0

        def release_all(self):
            self.releases += 1

        def set_safety(self, **_values):
            pass

    class Pipeline:
        verification_pending = False

        def tick(self):
            return PipelineOutcome("UNKNOWN", unknown.source_frame_id, unknown)

        def health(self):
            return {}

    worker = DSTGameWorker(
        WorkerConfig(plugin="dst", mode=WorkerMode.ACTIVE, validation_flow_enabled=False)
    )
    context = WorkerContext(1, 1, DisplayEnvironment(":99"), runtime_verified=True)
    worker.context = context
    worker._game_ready = True
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test runtime ready")
    worker.machine.transition(WorkerState.OBSERVING, "test worker active")
    worker.capture = object()
    worker.actions = Actions()
    worker.pipeline = Pipeline()

    report = worker.tick(context)

    assert worker.mode == WorkerMode.ACTIVE
    assert worker.actions.releases == 1
    assert report.mode == WorkerMode.OBSERVE
    assert worker._unknown_since is not None


def test_worker_tick_recovers_stable_state_locally_and_releases_input():
    worker = DSTGameWorker(WorkerConfig(
        plugin="dst", mode=WorkerMode.ACTIVE, validation_flow_enabled=False,
    ))
    worker.context = WorkerContext(1, 1, DisplayEnvironment(":99"), runtime_verified=True)
    worker._game_ready = True
    worker.capture = object()
    worker.machine.transition(WorkerState.WAITING_FOR_GAME, "test runtime ready")
    worker.machine.transition(WorkerState.OBSERVING, "test worker active")
    worker._last_capture_at = time.monotonic() - 10
    observation = analyze_image(
        Image.open(ASSETS / "samples/host_game_world_list_live.png").convert("RGB"),
        "worker-tick-late-world-list-1", 1,
    )

    class Input:
        has_held_inputs = True

    input_controller = Input()
    worker.input = input_controller

    class Actions:
        safety = None
        executor = type("Executor", (), {"current_action_id": None})()

        def release_all(self):
            input_controller.has_held_inputs = False

        def set_safety(self, **values):
            self.safety = values

    worker.actions = Actions()

    class Pipeline:
        verification_pending = False

        def tick(self):
            return PipelineOutcome("NO_ACTION", "frame-1", observation)

        def health(self):
            return {}

    worker.pipeline = Pipeline()
    policy = worker.activity
    policy.set_production_actions_enabled(True)
    policy.state = DSTScreen.HOST_GAME_WORLD_LIST
    policy._candidate = DSTScreen.HOST_GAME_WORLD_LIST
    policy._candidate_frames = 2
    policy.intervention_required = True
    policy._recoverable_intervention_action = ActionName.CLICK_HOST_GAME

    report = worker.tick(worker.context)

    assert not input_controller.has_held_inputs
    assert not policy.intervention_required
    assert worker.mode == WorkerMode.ACTIVE
    assert report.mode == WorkerMode.ACTIVE
    assert report.healthy
    assert report.state == WorkerState.WAITING
    assert report.details["requested_mode"] == WorkerMode.ACTIVE
    proposal = policy.propose(replace(
        observation, source_frame_id="worker-tick-late-world-list-2", source_sequence=2,
    ))
    assert proposal is not None and proposal.action == ActionName.SELECT_EXISTING_WORLD


def test_lifecycle_proven_no_effect_reaches_retry_without_rechecking_change_metric():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "host-proof-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_frame_id="host-proof-2", source_sequence=2)
    first = policy.propose(menu)
    assert first is not None and first.action == ActionName.CLICK_HOST_GAME

    no_effect = ActionResult(
        "host-no-effect", ActionName.CLICK_HOST_GAME, ActionStatus.TIMED_OUT,
        1.0, 1, 1, 1,
        "fresh unchanged MAIN_MENU proves Host Game click had no effect",
    )
    proof_frame = replace(
        menu, source_frame_id="host-proof-3", source_sequence=3,
        screen_change=.001,
    )
    policy.on_verified(proof_frame, no_effect)

    next_frame = replace(
        proof_frame, source_frame_id="host-proof-4", source_sequence=4,
        screen_change=.08,
    )
    retry = policy.propose(next_frame)
    assert retry is not None and retry.action == ActionName.CLICK_HOST_GAME
    assert "once" in (retry.reason or "")


def test_host_game_retry_is_withheld_for_ambiguous_non_menu_state():
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "host-ambiguous-1", 1,
    )
    policy = ActivityController(validation_flow_enabled=True)
    assert policy.propose(menu) is None
    menu = replace(menu, source_frame_id="host-ambiguous-2", source_sequence=2)
    assert policy.propose(menu).action == ActionName.CLICK_HOST_GAME
    policy.on_action_failure(ActionResult(
        "host-ambiguous", ActionName.CLICK_HOST_GAME, ActionStatus.FAILED,
        0.5, 1, 1, 1, "unexpected state LOADING during action verification",
    ))
    loading = replace(
        menu, screen=DSTScreen.LOADING, source_frame_id="host-loading",
        source_sequence=3, screen_confidence=1.0,
    )
    assert policy.propose(loading) is None
    assert policy.intervention_required


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
    policy = ActivityController()
    for sequence in (1, 2):
        policy.propose(replace(
            observation,
            source_frame_id=f"world-present-banner-{sequence}",
            source_sequence=sequence,
        ))
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0


def test_live_active_inworld_gift_is_distinct_from_daily_login_reward():
    observation = analyze_image(
        Image.open(ASSETS / "samples/gift_icon_active_in_world_live.png").convert("RGB"),
        "live-active-gift",
        1,
        profile_id="dst-1280x720-linux-v1",
    )
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    assert observation.validity == ObservationValidity.VALID
    active = next(item for item in observation.detections if item.kind == "gift_icon_active")
    icon = next(item for item in observation.detections if item.kind == "gift_icon")
    assert active.detected and active.verified and active.confidence >= .94
    assert icon.detected and icon.verified
    assert dict(icon.metadata)["availability"] == "GIFT_AVAILABLE"
    assert dict(icon.metadata)["active_reference_verified"] is True

    policy = ActivityController()
    policy.propose(observation)
    assert policy.inworld_gift_state.value == "IN_WORLD_GIFT_ACTIONABLE"
    assert policy.daily_gift_state == DailyGiftState.UNKNOWN


def test_daily_gift_available_and_open_action_are_not_confirmation():
    available = observe()
    policy = ActivityController()
    assert policy.propose(available) is None
    proposal = policy.propose(replace(
        available, source_frame_id="daily-gift-available-2", source_sequence=2,
    ))
    assert proposal is not None and proposal.action == ActionName.CLICK_REWARD_OPEN
    assert policy.daily_gift_state == DailyGiftState.GIFT_AVAILABLE

    verifying = ActionResult(
        "daily-gift-open", ActionName.CLICK_REWARD_OPEN,
        ActionStatus.VERIFYING, 0.1, 1, 1, 1,
    )
    policy.on_action_result(available, verifying)
    assert policy.daily_gift_state == DailyGiftState.GIFT_INTERACTION_STARTED
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0

    # A verified return to the menu proves dismissal/transition only.
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "dismissed-gift-menu", 3,
    )
    policy.on_verified(menu, replace(verifying, status=ActionStatus.SUCCEEDED))
    assert policy.daily_gift_state == DailyGiftState.GIFT_UI_CLOSED
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0


def test_daily_gift_requires_received_evidence_and_confirmation_is_idempotent():
    available = observe()
    policy = ActivityController()
    opening = ActionResult(
        "daily-gift-success-action", ActionName.CLICK_REWARD_OPEN,
        ActionStatus.VERIFYING, 0.1, 1, 1, 1,
    )
    policy.on_action_result(available, opening)
    received = analyze_image(
        Image.open(ASSETS / "samples/login_reward_result_live.png").convert("RGB"),
        "daily-gift-received", 2,
    )
    assert received.screen == DSTScreen.REWARD_RESULT
    assert any(
        item.kind == "login_reward_result_title" and item.detected and item.verified
        for item in received.detections
    )
    success = replace(opening, status=ActionStatus.SUCCEEDED)
    policy.on_verified(received, success)
    assert policy.daily_gift_state == DailyGiftState.DAILY_GIFT_CONFIRMED
    assert policy.daily_gift_confirmation is not None
    assert policy.daily_gift_confirmation.semantic == "DAILY_GIFT_CONFIRMED"
    assert policy.daily_gift_confirmation.evidence_frame_id == received.source_frame_id
    assert policy.counters["gift_claimed"] == 1
    policy.propose(received)
    policy.propose(received)
    assert policy.counters["gift_claimed"] == 1

    worker = DSTGameWorker(WorkerConfig(plugin="dst", mode=WorkerMode.DISABLED))
    worker.activity = policy
    report = worker.status()
    assert report.telemetry["daily_gift_state"] == "DAILY_GIFT_CONFIRMED"
    assert report.telemetry["daily_gift_confirmation"]["evidence_frame_id"] == (
        received.source_frame_id
    )

    # A duplicate callback and duplicate successful result frame cannot count again.
    policy.on_verified(received, success)
    assert policy.daily_gift_state == DailyGiftState.DAILY_GIFT_CONFIRMED
    assert policy.counters["gift_claimed"] == 1


def test_open_result_modal_without_received_anchor_is_not_confirmation():
    available = observe()
    policy = ActivityController()
    opening = ActionResult(
        "daily-gift-incomplete-result", ActionName.CLICK_REWARD_OPEN,
        ActionStatus.VERIFYING, 0.1, 1, 1, 1,
    )
    policy.on_action_result(available, opening)
    result = analyze_image(
        Image.open(ASSETS / "samples/login_reward_result_live.png").convert("RGB"),
        "daily-gift-result-anchor-missing", 2,
    )
    detections = tuple(
        replace(item, detected=False, verified=False)
        if item.kind == "login_reward_result_title"
        else item
        for item in result.detections
    )
    policy.on_verified(
        replace(result, detections=detections),
        replace(opening, status=ActionStatus.SUCCEEDED),
    )
    assert policy.daily_gift_state == DailyGiftState.GIFT_UI_OPEN
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0


def test_dismissing_received_modal_does_not_emit_a_second_gift_claim():
    received = analyze_image(
        Image.open(ASSETS / "samples/login_reward_result_live.png").convert("RGB"),
        "dismissed-gift-result", 1,
    )
    policy = ActivityController()
    closing = ActionResult(
        "dismiss-only", ActionName.CLICK_REWARD_CLOSE,
        ActionStatus.VERIFYING, 0.1, 1, 1, 1,
    )
    policy.on_action_result(received, closing)
    menu = analyze_image(
        Image.open(ASSETS / "samples/main_menu_after_reward.png").convert("RGB"),
        "dismissed-gift-menu", 2,
    )
    policy.on_verified(menu, replace(closing, status=ActionStatus.SUCCEEDED))
    assert policy.daily_gift_state == DailyGiftState.GIFT_UI_CLOSED
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0


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


def test_real_inworld_popup_and_canonical_use_later_remain_separate_from_daily():
    opening = analyze_image(
        Image.open(ASSETS / 'samples/inworld_gift_opening_live.png').convert('RGB'),
        'inworld-opening', 1, profile_id='dst-1280x720-linux-v1',
    )
    received = analyze_image(
        Image.open(ASSETS / 'samples/inworld_gift_received_live.png').convert('RGB'),
        'inworld-received', 2, profile_id='dst-1280x720-linux-v1',
    )
    assert opening.screen == DSTScreen.IN_WORLD_GIFT_OPENING
    assert received.screen == DSTScreen.IN_WORLD_GIFT_RECEIVED
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(opening) is None
    assert policy.propose(replace(opening, source_sequence=2)) is None
    assert policy.propose(replace(received, source_sequence=3)) is None
    policy._gift_clicked_monotonic = received.observed_monotonic - 10.0
    proposal = policy.propose(replace(received, source_sequence=4))
    assert proposal.action == ActionName.CLICK_INWORLD_USE_LATER
    point, _ = click_request(proposal.action, received)
    assert 462 / 1280 <= point.x <= 622 / 1280
    assert 589 / 720 <= point.y <= 622 / 720
    assert policy.daily_gift_state.value == 'UNKNOWN'
    assert policy.inworld_gift_confirmation is None


def test_inworld_received_title_without_use_later_never_enables_close():
    image = Image.open(ASSETS / 'samples/inworld_gift_received_live.png').convert('RGB')
    image.paste('black', (450, 570, 635, 640))
    observation = analyze_image(image, 'inworld-incomplete', 1,
                                profile_id='dst-1280x720-linux-v1')
    assert observation.screen != DSTScreen.IN_WORLD_GIFT_RECEIVED


def test_live_post_claim_world_has_no_gift_popup():
    observation = analyze_image(
        Image.open(ASSETS / "samples/inworld_gift_after_live.png").convert("RGB"),
        "live-post-claim", 1,
    )
    assert observation.production_ready
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    assert observation.screen_confidence >= .94
    assert not any(d.detected for d in observation.detections
                   if d.kind in {"inworld_gift_received_title", "inworld_gift_use_later"})


def test_managed_gift_detection_is_saved_before_a_claim_proposal():
    observation = analyze_image(
        Image.open(ASSETS / 'samples/gift_icon_active_in_world_live.png').convert('RGB'),
        'managed-before-claim', 1, profile_id='dst-1280x720-linux-v1',
    )
    saved = []
    class Evidence:
        ready = True
        def observe(self, observation):
            saved.append(observation.source_frame_id)
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    policy.claim_evidence = Evidence()
    policy.propose(observation)
    next_frame = replace(observation, source_frame_id='managed-before-claim-2', source_sequence=2)
    assert policy.propose(next_frame) is None
    third_frame = replace(observation, source_frame_id='managed-before-claim-3', source_sequence=3)
    proposal = policy.propose(third_frame)
    assert proposal.action == ActionName.CLICK_GIFT_ICON
    assert saved[-1] == third_frame.source_frame_id


def test_received_gift_with_disabled_use_now_still_closes_through_use_later():
    from runtime_agent.gameworker.transitions import action_precondition_error
    image = Image.open(ASSETS / 'samples/inworld_gift_received_live.png').convert('RGB')
    image.paste('gray', (650, 580, 820, 640))
    observation = analyze_image(image, 'unequippable-gift', 1, profile_id='dst-1280x720-linux-v1')
    assert observation.screen == DSTScreen.IN_WORLD_GIFT_RECEIVED
    assert observation.production_ready
    assert action_precondition_error(ActionName.CLICK_INWORLD_USE_LATER, observation) is None


def test_post_claim_world_remains_ready_with_occupied_first_inventory_slots():
    image = Image.open(ASSETS / 'samples/inworld_gift_after_live.png').convert('RGB')
    image.paste('black', (140, 0, 245, 100))
    for left in (205, 250, 295):
        image.paste('green', (left, 684, left + 30, 714))
    observation = analyze_image(image, 'occupied-slots-no-gift', 1,
                                profile_id='dst-1280x720-linux-v1')
    assert observation.production_ready
    assert observation.screen == DSTScreen.IN_WORLD_IDLE
    assert next(d for d in observation.detections if d.kind == 'world_inventory_frame').detected


def test_use_later_label_survives_button_border_changes_and_close_retry_is_bounded():
    image = Image.open(ASSETS / 'samples/inworld_gift_received_live.png').convert('RGB')
    label = image.crop((502, 597, 582, 615))
    image.paste('gray', (462, 589, 622, 622))
    image.paste(label, (502, 597))
    received = analyze_image(image, 'received-hover', 1, profile_id='dst-1280x720-linux-v1')
    assert received.screen == DSTScreen.IN_WORLD_GIFT_RECEIVED
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(received) is None
    first = replace(received, source_sequence=2, source_frame_id='received-hover-2')
    assert policy.propose(first) is None
    policy._gift_clicked_monotonic = received.observed_monotonic - 10.0
    assert policy.propose(first).action == ActionName.CLICK_INWORLD_USE_LATER
    timeout = ActionResult('close-1', ActionName.CLICK_INWORLD_USE_LATER,
                           ActionStatus.TIMED_OUT, .5, 1, 1, 1, 'popup remains')
    policy.on_verified(first, timeout)
    assert not policy.intervention_required
    second = replace(received, source_sequence=3, source_frame_id='received-hover-3')
    assert policy.propose(second).action == ActionName.CLICK_INWORLD_USE_LATER
    policy.on_verified(second, replace(timeout, action_id='close-2'))
    assert policy.intervention_required
    assert policy.propose(replace(second, source_sequence=4)) is None


def test_verified_pending_gift_cannot_be_clicked():
    from runtime_agent.gameworker.transitions import action_precondition_error
    observation = analyze_image(
        Image.open(ASSETS / "samples/gift_icon_active_in_world_live.png").convert("RGB"),
        "pending-station-claim", 1, profile_id="dst-1280x720-linux-v1",
    )
    detections = tuple(
        replace(item, metadata=tuple((k, v) for k, v in item.metadata if k != "availability")
                + (("availability", "IN_WORLD_GIFT_PENDING"),))
        if item.kind == "gift_icon" else item
        for item in observation.detections
    )
    observation = replace(observation, detections=detections)
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    policy.propose(observation)
    before_click = replace(observation, source_sequence=2,
                           source_frame_id="pending-station-claim-click")
    proposal = policy.propose(before_click)
    assert proposal is None or proposal.action != ActionName.CLICK_GIFT_ICON
    assert action_precondition_error(ActionName.CLICK_GIFT_ICON, before_click) is not None
    try:
        click_request(ActionName.CLICK_GIFT_ICON, before_click)
    except ValueError as exc:
        assert 'claimable gift detection is unavailable' in str(exc)
    else:
        raise AssertionError('gray gift must not supply a click target')


def test_unclaimable_gift_precondition_defers_without_stopping_play():
    observation = analyze_image(
        Image.open(ASSETS / 'samples/gift_icon_active_in_world_live.png').convert('RGB'),
        'blocked-gift', 1, profile_id='dst-1280x720-linux-v1')
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    policy._gift_icon_click_attempts = 1
    result = ActionResult('blocked', ActionName.CLICK_GIFT_ICON,
                          ActionStatus.SAFETY_BLOCKED, 0.0, 1, 1, 1,
                          'fresh claimable gift detection is unavailable')
    policy.on_action_result(observation, result)
    assert not policy.intervention_required
    assert policy.claim_not_actionable
    assert policy._gift_retry_at > time.monotonic()
    assert policy.inworld_gift_confirmation is None


def test_last_gift_transport_timeout_defers_but_input_failure_still_stops():
    observation = analyze_image(
        Image.open(ASSETS / 'samples/gift_icon_active_in_world_live.png').convert('RGB'),
        'timeout-gift', 1, profile_id='dst-1280x720-linux-v1')
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    policy._gift_icon_click_attempts = 3
    timeout = ActionResult('timeout', ActionName.CLICK_GIFT_ICON,
                           ActionStatus.TIMED_OUT, 0.2, 1, 1, 1,
                           'executor wait timed out')
    policy.on_action_result(observation, timeout)
    assert not policy.intervention_required
    assert policy.claim_not_actionable
    assert policy.inworld_gift_confirmation is None
    policy.on_action_result(observation, replace(timeout, status=ActionStatus.FAILED,
                                                reason='transport connection lost'))
    assert policy.intervention_required


def test_gift_timeout_on_unknown_frame_keeps_observing_without_claim_confirmation():
    observation = analyze_image(
        Image.open(ASSETS / 'samples/gift_icon_active_in_world_live.png').convert('RGB'),
        'unknown-after-gift', 1, profile_id='dst-1280x720-linux-v1')
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    policy._gift_icon_click_attempts = 3
    policy._awaiting_reward_transition = True
    result = ActionResult('gift', ActionName.CLICK_GIFT_ICON, ActionStatus.TIMED_OUT,
                          .5, 1, 1, 1, 'verified transition deadline elapsed')
    policy.on_verified(replace(observation, screen=DSTScreen.UNKNOWN), result)
    assert not policy.intervention_required
    assert not policy._awaiting_reward_transition
    assert policy.claim_not_actionable
    assert policy._gift_retry_at > time.monotonic()
    assert policy.inworld_gift_confirmation is None
    policy.on_action_failure(replace(result, status=ActionStatus.FAILED,
                                     reason='input transport failed'))
    assert policy.intervention_required
