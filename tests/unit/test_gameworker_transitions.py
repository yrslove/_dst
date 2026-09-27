"""Real saved frames exercise the generic action outcome contract."""
from dataclasses import replace

from PIL import Image
from test_dst_behavior import ASSETS, analyze_image

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.transitions import ActionLifecycle, click_request


def frame(name, sequence):
    return analyze_image(Image.open(ASSETS / "samples" / name).convert("RGB"),
                         f"frame-{sequence}", sequence)


def sent(action):
    return ActionResult(f"action-{action.value}", action, ActionStatus.SENT,
                        .5, 1, 1, 1)


def test_saved_menu_roundtrip_requires_two_fresh_frames_per_transition():
    menu = frame("main_menu_after_reward.png", 1)
    clock = [menu.observed_monotonic + .0001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    point, viewport = click_request(ActionName.CLICK_OPTIONS, menu)
    assert 0 < point.x < .18 and viewport.width == 1280
    assert lifecycle.begin(sent(ActionName.CLICK_OPTIONS), menu).status == ActionStatus.VERIFYING
    assert lifecycle.observe(menu) is None  # pre-action frame is never success
    options = frame("options_live.png", 2)
    assert lifecycle.observe(options) is None
    assert lifecycle.observe(options) is None  # duplicate sequence is not a second frame
    options2 = replace(options, source_sequence=3, source_frame_id="frame-3")
    assert lifecycle.observe(options2).status == ActionStatus.SUCCEEDED

    clock[0] = options2.observed_monotonic + .0001
    assert lifecycle.begin(sent(ActionName.CLICK_BACK), options2).status == ActionStatus.VERIFYING
    dialog = frame("options_discard_confirm.png", 4)
    assert lifecycle.observe(dialog) is None
    dialog2 = replace(dialog, source_sequence=5, source_frame_id="frame-5")
    assert lifecycle.observe(dialog2).status == ActionStatus.SUCCEEDED

    clock[0] = dialog2.observed_monotonic + .0001
    assert lifecycle.begin(sent(ActionName.DISCARD_OPTIONS), dialog2).status == ActionStatus.VERIFYING
    returned = frame("main_menu_after_reward.png", 6)
    assert lifecycle.observe(returned) is None
    returned2 = replace(returned, source_sequence=7, source_frame_id="frame-7")
    assert lifecycle.observe(returned2).status == ActionStatus.SUCCEEDED
    assert lifecycle.pending is None


def test_transport_success_wrong_screen_and_timeout_never_succeed():
    menu = frame("main_menu_after_reward.png", 1)
    clock = [menu.observed_monotonic + .0001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.CLICK_OPTIONS), menu).status == ActionStatus.VERIFYING
    menu2 = replace(menu, source_sequence=2, source_frame_id="frame-2",
                    observed_monotonic=clock[0] + .001)
    assert lifecycle.observe(menu2) is None
    assert lifecycle.current().status == ActionStatus.VERIFYING
    clock[0] += 46
    assert lifecycle.poll().status == ActionStatus.TIMED_OUT
    assert lifecycle.pending is None
