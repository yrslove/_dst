import time
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image

from runtime_agent.gameworker.actions import (
    Action,
    ActionName,
    ActionResult,
    ActionStatus,
)
from runtime_agent.gameworker.activity import (
    ActivityController,
    DailyGiftState,
    InWorldGiftState,
)
from runtime_agent.gameworker.geometry import CalibrationProfile
from runtime_agent.gameworker.gift_icon import UNKNOWN, classify_icon, hover_response
from runtime_agent.gameworker.transitions import (
    action_precondition_error,
    click_request,
)
from runtime_agent.gameworker.vision import (
    AssetRegistry,
    Detection,
    ObservationValidity,
    VisionDetector,
)
from tests.unit.test_dst_behavior import ASSETS
from tests.unit.test_gameworker_actions import executor
from tests.unit.test_gameworker_perception import make_frame


def analyze_image(image, frame_id, sequence):
    now = time.monotonic()
    frame = make_frame(sequence, captured_monotonic=now, image=image)
    return VisionDetector(
        AssetRegistry(ASSETS / "manifest.json"), default_threshold=0.8
    ).analyze(
        replace(frame, frame_id=frame_id),
        calibration=CalibrationProfile(
            "dst-1280x720-linux-v1", 1, 1280, 720, verified=True
        ),
        observation_generation=1,
        max_frame_age=3,
        deadline=now + 3,
    )


def gray_observation():
    image = Image.open(ASSETS / "samples/gift_icon_gray_in_world_live.png").convert(
        "RGB"
    )
    return image, analyze_image(image, "gift-gray-live", 1)


def active_observation(sequence=1, *, fresh_until=None):
    _, observation = gray_observation()
    detections = tuple(
        replace(
            item,
            metadata=tuple(
                (key, "GIFT_AVAILABLE" if key == "availability" else value)
                for key, value in item.metadata
            ),
        )
        if item.kind == "gift_icon"
        else item
        for item in observation.detections
    )
    return replace(
        observation,
        detections=detections,
        source_sequence=sequence,
        source_frame_id=f"active-gift-{sequence}",
        fresh_until=(observation.fresh_until if fresh_until is None else fresh_until),
    )


def test_gray_live_icon_is_pending_until_giftmachine_is_enabled():
    _, observation = gray_observation()
    icon = next(d for d in observation.detections if d.kind == "gift_icon")
    assert icon.detected and icon.verified and icon.confidence >= 0.94
    assert dict(icon.metadata)["availability"] == "IN_WORLD_GIFT_PENDING"
    policy = ActivityController()
    policy.propose(observation)
    assert policy.inworld_gift_state == InWorldGiftState.PENDING_STATION
    assert policy.daily_gift_state == DailyGiftState.UNKNOWN
    assert policy.daily_gift_confirmation is None


def test_gray_gift_opens_inventory_before_any_gift_click():
    _, observation = gray_observation()
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(observation) is None  # two-frame hysteresis
    next_frame = replace(
        observation, source_frame_id="gray-live-next", source_sequence=2
    )
    proposal = policy.propose(next_frame)
    assert proposal is not None and proposal.action == ActionName.OPEN_CRAFTING_MENU
    assert policy.inworld_gift_state == InWorldGiftState.PENDING_STATION
    assert policy.daily_gift_state == DailyGiftState.UNKNOWN
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0
    assert action_precondition_error(ActionName.CLICK_GIFT_ICON, next_frame)
    assert action_precondition_error(ActionName.OPEN_CRAFTING_MENU, next_frame) is None


def test_verified_crafting_open_clears_pending_action_state():
    _, observation = gray_observation()
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    policy.propose(observation)
    next_frame = replace(
        observation, source_frame_id="gray-live-craft", source_sequence=2
    )
    proposal = policy.propose(next_frame)
    assert proposal is not None and proposal.action == ActionName.OPEN_CRAFTING_MENU
    verifying = ActionResult(
        "crafting-open", ActionName.OPEN_CRAFTING_MENU,
        ActionStatus.VERIFYING, 0.1, 1, 1, 1,
    )
    policy.on_action_result(next_frame, verifying)
    policy.on_verified(
        next_frame,
        ActionResult(
            "crafting-open", ActionName.OPEN_CRAFTING_MENU,
            ActionStatus.SUCCEEDED, 0.1, 1, 1, 1,
        ),
    )
    assert not policy._awaiting_reward_transition
    assert not policy.intervention_required


def test_missing_icon_is_unknown():
    image, _ = gray_observation()
    image.paste((25, 25, 25), (147, 0, 256, 101))
    observation = analyze_image(image, "gift-missing", 1)
    icon = next(d for d in observation.detections if d.kind == "gift_icon")
    assert not icon.detected
    assert dict(icon.metadata)["availability"] == UNKNOWN


def test_ambiguous_chroma_and_hover_alone_remain_unknown():
    image, observation = gray_observation()
    icon = next(d for d in observation.detections if d.kind == "gift_icon")
    template = np.asarray(Image.open(ASSETS / "world_present_banner.png").convert("L"))
    sample = np.asarray(image).copy()
    sample[10:68, 168:226, 0] = np.clip(
        sample[10:68, 168:226, 0].astype(int) + 22, 0, 255
    )
    evidence = classify_icon(
        Image.fromarray(sample), icon, template, hover_verified=True
    )
    assert evidence["availability"] == UNKNOWN
    assert evidence["active_reference_verified"] is False


def test_stale_frame_cannot_produce_availability():
    _, observation = gray_observation()
    policy = ActivityController()
    policy.propose(replace(observation, fresh_until=0))
    assert policy.daily_gift_state != DailyGiftState.NO_REWARD_AVAILABLE
    policy.propose(replace(observation, validity=ObservationValidity.STALE))
    assert policy.daily_gift_confirmation is None


def test_hover_uses_canonical_detected_center_and_never_clicks():
    _, observation = gray_observation()
    target, viewport = click_request(ActionName.HOVER_GIFT_ICON, observation)
    value, controller, driver, _ = executor()
    try:
        result = value.execute(
            Action(
                "gift-hover",
                ActionName.HOVER_GIFT_ICON,
                7,
                3,
                runtime_id=2,
                deadline=time.monotonic() + 1,
                parameters=(
                    ("x", target.x),
                    ("y", target.y),
                    ("width", viewport.width),
                    ("height", viewport.height),
                ),
            )
        )
        assert result.status == ActionStatus.SENT
        assert [e.operation for e in driver.events] == ["mouse_move"]
        assert driver.events[0].value == viewport.point(target)
        assert not controller.has_held_inputs
    finally:
        value.shutdown()


def test_active_gift_click_uses_detected_center_through_canonical_input():
    observation = active_observation()
    icon = next(d for d in observation.detections if d.kind == "gift_icon")
    target, viewport = click_request(ActionName.CLICK_GIFT_ICON, observation)
    assert icon.bounds is not None
    assert target.x == (icon.bounds.left + icon.bounds.right) / 2
    assert target.y == (icon.bounds.top + icon.bounds.bottom) / 2
    value, controller, driver, _ = executor()
    try:
        result = value.execute(
            Action(
                "gift-click",
                ActionName.CLICK_GIFT_ICON,
                7,
                3,
                runtime_id=2,
                deadline=time.monotonic() + 0.8,
                parameters=(
                    ("x", target.x),
                    ("y", target.y),
                    ("width", viewport.width),
                    ("height", viewport.height),
                    ("evidence_sequence", observation.source_sequence),
                ),
            )
        )
        assert result.status == ActionStatus.SENT
        assert [event.operation for event in driver.events].count("mouse_down") == 1
        assert [event.operation for event in driver.events].count("mouse_up") == 1
        assert all(event.operation not in {"key_down", "key_press"} for event in driver.events)
        assert not controller.has_held_inputs
    finally:
        value.shutdown()


def test_stale_active_gift_evidence_is_rejected_before_click():
    stale = active_observation(fresh_until=0)
    with pytest.raises(ValueError, match="stale"):
        click_request(ActionName.CLICK_GIFT_ICON, stale)
    assert ActivityController().propose(stale) is None


def test_active_gift_retry_is_bounded_and_reacquires_fresh_evidence():
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(active_observation(1)) is None
    proposal = policy.propose(active_observation(2))
    assert proposal is not None and proposal.action == ActionName.CLICK_GIFT_ICON
    sent = Action(
        "gift-open", ActionName.CLICK_GIFT_ICON, 1, 1, runtime_id=1
    )
    verifying = ActionResult(
        sent.action_id, sent.name, ActionStatus.VERIFYING, 0.1, 1, 1, 1
    )
    policy.on_action_result(active_observation(2), verifying)
    policy.on_verified(
        active_observation(3), replace(verifying, status=ActionStatus.TIMED_OUT)
    )
    retry = policy.propose(active_observation(4))
    assert retry is not None and retry.action == ActionName.CLICK_GIFT_ICON
    policy.on_action_result(active_observation(4), verifying)
    policy.on_verified(
        active_observation(5), replace(verifying, status=ActionStatus.TIMED_OUT)
    )
    assert policy.intervention_required
    assert policy.propose(active_observation(6)) is None


def test_opening_gift_ui_does_not_confirm_daily_gift():
    policy = ActivityController()
    opening = ActionResult(
        "gift-icon-open", ActionName.CLICK_GIFT_ICON,
        ActionStatus.SUCCEEDED, 0.1, 1, 1, 1,
    )
    policy.on_verified(active_observation(2), opening)
    assert policy.daily_gift_state == DailyGiftState.GIFT_UI_OPEN
    assert policy.daily_gift_confirmation is None
    assert policy.counters["gift_claimed"] == 0
    policy.set_production_actions_enabled(True)
    policy.propose(active_observation(3))
    assert policy.propose(active_observation(4)) is None


def test_uncertain_identity_hovers_once_without_claim_semantics():
    _, observation = gray_observation()
    detections = tuple(
        replace(d, confidence=0.90, metadata=(("availability", UNKNOWN),))
        if d.kind == "gift_icon"
        else d
        for d in observation.detections
    )
    observation = replace(observation, detections=detections)
    policy = ActivityController()
    policy.set_production_actions_enabled(True)
    assert policy.propose(observation) is None
    second = replace(observation, source_sequence=2, source_frame_id="gift-hover-2")
    assert policy.propose(second).action == ActionName.HOVER_GIFT_ICON
    assert policy.propose(replace(second, source_sequence=3)) is None
    assert policy.inworld_gift_state == InWorldGiftState.AVAILABILITY_UNKNOWN
    assert policy.daily_gift_confirmation is None


def test_hover_requires_local_text_like_response():
    before = Image.new("L", (282, 94), 40)
    after = before.copy()
    assert not hover_response(before, after)
    # Test the secondary UI-change gate only; this is not an active icon fixture.
    for i in range(8):
        after.paste(200, (10 + i * 15, 10, 15 + i * 15, 25))
    assert hover_response(before, after)
    assert (
        Detection("gift_hover_response", True, 1.0, verified=True).kind != "gift_icon"
    )
