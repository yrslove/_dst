import time
from dataclasses import replace

import numpy as np
from PIL import Image

from runtime_agent.gameworker.actions import Action, ActionName, ActionStatus
from runtime_agent.gameworker.activity import ActivityController, DailyGiftState
from runtime_agent.gameworker.geometry import CalibrationProfile
from runtime_agent.gameworker.gift_icon import UNKNOWN, classify_icon, hover_response
from runtime_agent.gameworker.transitions import click_request
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


def test_gray_live_icon_is_unavailable():
    _, observation = gray_observation()
    icon = next(d for d in observation.detections if d.kind == "gift_icon")
    assert icon.detected and icon.verified and icon.confidence >= 0.94
    assert dict(icon.metadata)["availability"] == "NO_REWARD_AVAILABLE"
    policy = ActivityController()
    policy.propose(observation)
    assert policy.daily_gift_state == DailyGiftState.NO_REWARD_AVAILABLE
    assert policy.daily_gift_confirmation is None


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
    assert policy.daily_gift_state == DailyGiftState.GIFT_AVAILABILITY_UNKNOWN
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
