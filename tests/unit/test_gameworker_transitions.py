"""Real saved frames exercise the generic action outcome contract."""
import time
from dataclasses import replace

import pytest
from PIL import Image
from test_dst_behavior import ASSETS, analyze_image

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
from runtime_agent.gameworker.fixed_ui import (
    DST_FIXED_1280X720,
    FIXED_UI_ACTION_TARGETS,
)
from runtime_agent.gameworker.transitions import (
    CONTRACTS,
    ActionLifecycle,
    click_request,
)
from runtime_agent.gameworker.vision import (
    Detection,
    DSTScreen,
    FrameChangeMonitor,
    ObservationValidity,
)


def frame(name, sequence):
    return analyze_image(Image.open(ASSETS / "samples" / name).convert("RGB"),
                         f"frame-{sequence}", sequence)


def mods_disabled_frame(sequence):
    image = Image.new("RGB", (1280, 720), "black")
    image.paste(Image.open(ASSETS / "mods_disabled_title.png"), (490, 64))
    image.paste(Image.open(ASSETS / "mods_disabled_continue.png"), (424, 553))
    return analyze_image(image, f"mods-disabled-{sequence}", sequence)


def hovered_survivor(sequence):
    image = Image.open(ASSETS / "samples/character_selection_live.png").convert("RGB")
    image.paste(
        Image.open(ASSETS / "character_select_wilson_hover.png").convert("RGB"),
        (376, 135),
    )
    return analyze_image(image, f"hovered-{sequence}", sequence)


def sent(action):
    return ActionResult(f"action-{action.value}", action, ActionStatus.SENT,
                        .5, 1, 1, 1)


def active_gift_frame(sequence):
    observation = frame("gift_icon_gray_in_world_live.png", sequence)
    icon = next(d for d in observation.detections if d.kind == "world_present_banner")
    detections = tuple(item for item in observation.detections if item.kind != "gift_icon")
    detections += (
        Detection(
            "gift_icon", True, .99, bounds=icon.bounds,
            detector_id="gift-icon-test", verified=True,
            metadata=(("availability", "GIFT_AVAILABLE"),),
        ),
    )
    return replace(observation, detections=detections)


def gift_reward_panel(before, *, sequence, markers=True):
    icon = next(d for d in before.detections if d.kind == "gift_icon")
    detections = tuple(
        item for item in before.detections if item.kind != "gift_icon"
    )
    if markers:
        detections += (
            Detection("login_reward_title", True, .99, bounds=icon.bounds,
                      verified=True),
            Detection("login_reward_open_button", True, .99,
                      bounds=icon.bounds, verified=True),
        )
    return replace(
        before,
        timestamp=f"gift-panel-{sequence}",
        observation_generation=sequence,
        source_frame_id=f"gift-panel-{sequence}",
        source_sequence=sequence,
        source_captured_monotonic=before.source_captured_monotonic + .2,
        observed_monotonic=before.observed_monotonic + .2,
        fresh_until=before.fresh_until + .2,
        screen=DSTScreen.LOGIN_REWARD_AVAILABLE,
        screen_confidence=.99,
        detections=detections,
    )


def test_active_gift_click_uses_current_detected_bounds_center():
    observation = active_gift_frame(1)
    icon = next(d for d in observation.detections if d.kind == "gift_icon")
    point, viewport = click_request(ActionName.CLICK_GIFT_ICON, observation)
    assert icon.bounds is not None
    assert point.x == (icon.bounds.left + icon.bounds.right) / 2
    assert point.y == (icon.bounds.top + icon.bounds.bottom) / 2
    assert viewport.width == observation.frame_width


def test_gift_click_requires_fresh_active_detection_and_reacquisition():
    stale = replace(active_gift_frame(1), fresh_until=0)
    with pytest.raises(ValueError, match="stale"):
        click_request(ActionName.CLICK_GIFT_ICON, stale)
    fresh = active_gift_frame(2)
    point, _ = click_request(ActionName.CLICK_GIFT_ICON, fresh)
    assert 0 < point.x < .25 and 0 < point.y < .2


def test_gift_icon_transport_success_waits_for_specific_reward_ui_transition():
    before = active_gift_frame(1)
    clock = [before.observed_monotonic + .001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.CLICK_GIFT_ICON), before).status == ActionStatus.VERIFYING

    no_transition = replace(
        before,
        source_frame_id="gift-still-visible",
        source_sequence=2,
        source_captured_monotonic=clock[0] + .1,
        observed_monotonic=clock[0] + .1,
        fresh_until=clock[0] + 3,
    )
    assert lifecycle.observe(no_transition) is None
    clock[0] += 12.1
    result = lifecycle.poll()
    assert result is not None and result.status == ActionStatus.TIMED_OUT


def test_gift_open_succeeds_only_with_reward_panel_and_open_anchor():
    before = active_gift_frame(1)
    clock = [before.observed_monotonic + .001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.CLICK_GIFT_ICON), before).status == ActionStatus.VERIFYING
    panel = gift_reward_panel(before, sequence=2)
    result = lifecycle.observe(panel)
    assert result is not None and result.status == ActionStatus.SUCCEEDED
    assert "LOGIN_REWARD_AVAILABLE" in result.reason


def test_gift_open_without_specific_ui_anchors_times_out_without_success():
    before = active_gift_frame(1)
    clock = [before.observed_monotonic + .001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.CLICK_GIFT_ICON), before).status == ActionStatus.VERIFYING
    panel = gift_reward_panel(before, sequence=2, markers=False)
    assert lifecycle.observe(panel) is None
    clock[0] += 12.1
    assert lifecycle.poll().status == ActionStatus.TIMED_OUT


def test_survivor_hover_is_known_but_requires_loadout_to_complete():
    selection = frame("character_selection_live.png", 1)
    hover = hovered_survivor(2)
    assert selection.screen == DSTScreen.CHARACTER_SELECTION
    assert hover.screen == DSTScreen.CHARACTER_SELECTION_HOVERED
    assert hover.screen_confidence >= .94
    target, _ = click_request(ActionName.SELECT_SURVIVOR, hover)
    assert .28 < target.x < .38 and .18 < target.y < .33

    clock = [selection.observed_monotonic + .0001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.SELECT_SURVIVOR), selection).status == ActionStatus.VERIFYING
    assert lifecycle.observe(hover) is None
    assert lifecycle.observe(replace(hover, source_sequence=3, source_frame_id="hovered-3")) is None
    loadout = frame("character_loadout_live.png", 4)
    assert lifecycle.observe(loadout) is None
    assert lifecycle.observe(replace(loadout, source_sequence=5, source_frame_id="loadout-5")).status == ActionStatus.SUCCEEDED


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


def test_host_game_click_uses_fixed_profile_point_independent_of_anchor_bounds():
    menu = frame("main_menu_after_reward.png", 1)
    point, viewport = click_request(ActionName.CLICK_HOST_GAME, menu)
    assert point == DST_FIXED_1280X720.point("HOST_GAME", 1280, 720)
    without_anchors = replace(menu, detections=())
    assert click_request(ActionName.CLICK_HOST_GAME, without_anchors)[0] == point
    assert viewport.width == 1280 and viewport.height == 720


def test_host_game_fresh_unchanged_menu_decisively_ends_verification():
    menu = frame("main_menu_after_reward.png", 1)
    clock = [menu.observed_monotonic + .001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.CLICK_HOST_GAME), menu).status == ActionStatus.VERIFYING

    first = replace(
        menu, source_frame_id="host-unchanged-2", source_sequence=2,
        observed_monotonic=clock[0] + .01, screen_change=.001,
    )
    clock[0] = first.observed_monotonic + .001
    assert lifecycle.observe(first) is None
    second = replace(
        first, source_frame_id="host-unchanged-3", source_sequence=3,
        observed_monotonic=clock[0] + .01,
    )
    clock[0] = second.observed_monotonic + .001
    result = lifecycle.observe(second)
    assert result is not None and result.status == ActionStatus.TIMED_OUT
    assert result.reason == "fresh unchanged MAIN_MENU proves Host Game click had no effect"
    assert lifecycle.pending is None


def test_host_game_world_list_succeeds_without_classifying_source_as_no_effect():
    menu = frame("main_menu_after_reward.png", 1)
    clock = [menu.observed_monotonic + .001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.CLICK_HOST_GAME), menu).status == ActionStatus.VERIFYING
    target = frame("host_game_world_list_live.png", 2)
    assert lifecycle.observe(target) is None
    target2 = replace(
        target, source_frame_id="host-world-list-3", source_sequence=3,
        observed_monotonic=target.observed_monotonic + .02,
    )
    clock[0] = target2.observed_monotonic + .001
    result = lifecycle.observe(target2)
    assert result is not None and result.status == ActionStatus.SUCCEEDED
    assert result.reason.startswith("perception verified transition to HOST_GAME_WORLD_LIST")


def test_host_game_loading_state_fails_closed_without_a_retry_signal():
    menu = frame("main_menu_after_reward.png", 1)
    lifecycle = ActionLifecycle(clock=lambda: menu.observed_monotonic + .1)
    assert lifecycle.begin(sent(ActionName.CLICK_HOST_GAME), menu).status == ActionStatus.VERIFYING
    loading = replace(
        menu, screen=DSTScreen.LOADING, source_frame_id="host-loading",
        source_sequence=2, observed_monotonic=menu.observed_monotonic + .2,
    )
    result = lifecycle.observe(loading)
    assert result is not None and result.status == ActionStatus.FAILED
    assert "unexpected state LOADING" in result.reason


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


def test_movement_requires_fresh_meaningful_frame_change_and_fails_on_pause():
    alive = analyze_image(
        Image.open(ASSETS / "samples/in_world_wilson_live.png").convert("RGB"),
        "movement-before",
        1,
    )
    before = replace(alive, screen_change=0.0)
    clock = [before.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.MOVE_FORWARD), before).status == ActionStatus.VERIFYING
    still = replace(
        before,
        source_frame_id="movement-still",
        source_sequence=2,
        observation_generation=2,
        source_captured_monotonic=before.source_captured_monotonic + 0.1,
        observed_monotonic=before.observed_monotonic + 0.1,
        fresh_until=before.fresh_until + 0.1,
        screen_change=0.001,
        gameplay_change=0.001,
    )
    clock[0] = still.observed_monotonic + 0.001
    assert lifecycle.observe(still) is None
    changed = replace(
        still,
        source_frame_id="movement-changed",
        source_sequence=3,
        observation_generation=3,
        source_captured_monotonic=still.source_captured_monotonic + 0.1,
        observed_monotonic=still.observed_monotonic + 0.1,
        fresh_until=still.fresh_until + 0.1,
        screen_change=0.02,
        gameplay_change=0.02,
    )
    clock[0] = changed.observed_monotonic + 0.001
    verified = lifecycle.observe(changed)
    assert verified.status == ActionStatus.SUCCEEDED
    assert "gameplay ROI change=0.020000" in verified.reason

    clock[0] = changed.observed_monotonic + 0.01
    assert lifecycle.begin(sent(ActionName.MOVE_BACKWARD), changed).status == ActionStatus.VERIFYING
    paused = frame("in_world_auto_paused_live.png", 4)
    paused = replace(
        paused,
        source_captured_monotonic=changed.source_captured_monotonic + 0.2,
        observed_monotonic=changed.observed_monotonic + 0.2,
        fresh_until=changed.fresh_until + 0.2,
    )
    clock[0] = paused.observed_monotonic + 0.001
    result = lifecycle.observe(paused)
    assert result.status == ActionStatus.FAILED
    assert "PAUSED" in result.reason
    assert lifecycle.pending is None


def test_interaction_requires_verified_prompt_and_prompt_disappearance():
    alive = frame("in_world_wilson_live.png", 1)
    clock = [alive.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    blocked = lifecycle.begin(sent(ActionName.INTERACT), alive)
    assert blocked.status == ActionStatus.SAFETY_BLOCKED
    assert "target" in blocked.reason

    prompt = Detection(
        "interaction_prompt", True, 0.99, detector_id="fixture", verified=True
    )
    target = replace(
        alive,
        interaction_prompt_visible=prompt,
        detections=alive.detections + (prompt,),
    )
    assert lifecycle.begin(sent(ActionName.INTERACT), target).status == ActionStatus.VERIFYING
    gone = Detection(
        "interaction_prompt", False, 0.99, detector_id="fixture", verified=True
    )
    after = replace(
        target,
        source_frame_id="interaction-after",
        source_sequence=2,
        observation_generation=2,
        source_captured_monotonic=target.source_captured_monotonic + 0.1,
        observed_monotonic=target.observed_monotonic + 0.1,
        fresh_until=target.fresh_until + 0.1,
        interaction_prompt_visible=gone,
        detections=tuple(
            gone if item.kind == "interaction_prompt" else item
            for item in target.detections
        ),
    )
    clock[0] = after.observed_monotonic + 0.001
    assert lifecycle.observe(after) is None
    after_again = replace(
        after,
        source_frame_id="interaction-after-again",
        source_sequence=3,
        observation_generation=3,
        source_captured_monotonic=after.source_captured_monotonic + 0.1,
        observed_monotonic=after.observed_monotonic + 0.1,
        fresh_until=after.fresh_until + 0.1,
    )
    clock[0] = after_again.observed_monotonic + 0.001
    assert lifecycle.observe(after_again).status == ActionStatus.SUCCEEDED


def test_reward_close_action_is_verified_by_live_result_to_main_menu_frames():
    reward_result = frame("login_reward_result_live.png", 1)
    assert reward_result.validity == ObservationValidity.VALID
    assert reward_result.screen == DSTScreen.REWARD_RESULT
    point, viewport = click_request(ActionName.CLICK_REWARD_CLOSE, reward_result)
    assert 0.4 < point.x < 0.6 and 0.8 < point.y < 0.95
    assert viewport.width == 1280 and viewport.height == 720

    clock = [reward_result.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert (
        lifecycle.begin(sent(ActionName.CLICK_REWARD_CLOSE), reward_result).status
        == ActionStatus.VERIFYING
    )
    main_menu = frame("main_menu_after_reward.png", 2)
    assert lifecycle.observe(main_menu) is None
    main_menu_again = replace(
        main_menu,
        source_sequence=3,
        source_frame_id="frame-3",
    )
    assert lifecycle.observe(main_menu_again).status == ActionStatus.SUCCEEDED
    assert lifecycle.pending is None


def test_selected_survivor_click_is_verified_by_loadout_screen():
    selected = frame("character_selection_live.png", 1)
    assert selected.screen == DSTScreen.CHARACTER_SELECTION
    assert ActionName.SELECT_SURVIVOR in CONTRACTS
    assert CONTRACTS[ActionName.SELECT_SURVIVOR].anchors == (
        "character_select_wilson_icon",
        "character_select_wilson_hover",
    )
    target, viewport = click_request(ActionName.SELECT_SURVIVOR, selected)
    icon = next(
        item for item in selected.detections
        if item.kind == "character_select_wilson_icon" and item.detected
    )
    assert icon.bounds is not None
    assert target.x == pytest.approx((icon.bounds.left + icon.bounds.right) / 2)
    assert target.y == pytest.approx((icon.bounds.top + icon.bounds.bottom) / 2)
    assert viewport.width == 1280 and viewport.height == 720

    clock = [selected.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert (
        lifecycle.begin(sent(ActionName.SELECT_SURVIVOR), selected).status
        == ActionStatus.VERIFYING
    )
    loadout = replace(
        selected,
        screen=DSTScreen.CHARACTER_LOADOUT,
        source_sequence=2,
        source_frame_id="character-loadout-1",
        observation_generation=2,
        observed_monotonic=selected.observed_monotonic + 0.01,
        fresh_until=selected.fresh_until + 0.01,
    )
    assert lifecycle.observe(loadout) is None
    loadout_again = replace(
        loadout,
        source_sequence=3,
        source_frame_id="character-loadout-2",
        observed_monotonic=loadout.observed_monotonic + 0.01,
        fresh_until=loadout.fresh_until + 0.01,
    )
    result = lifecycle.observe(loadout_again)
    assert result.status == ActionStatus.SUCCEEDED
    assert "CHARACTER_LOADOUT" in result.reason
    assert lifecycle.pending is None


def test_resume_world_transition_accepts_character_selection():
    selected_world = frame("host_game_world_selected_live.png", 1)
    assert selected_world.screen == DSTScreen.HOST_GAME_WORLD_SELECTED
    clock = [selected_world.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert (
        lifecycle.begin(sent(ActionName.START_EXISTING_WORLD), selected_world).status
        == ActionStatus.VERIFYING
    )

    character_select = frame("character_selection_live.png", 2)
    assert character_select.screen == DSTScreen.CHARACTER_SELECTION
    assert lifecycle.observe(character_select) is None
    character_select_again = replace(
        character_select,
        source_sequence=3,
        source_frame_id="frame-3",
    )
    result = lifecycle.observe(character_select_again)
    assert result.status == ActionStatus.SUCCEEDED
    assert "CHARACTER_SELECTION" in result.reason
    assert lifecycle.pending is None


def test_movement_requires_fresh_world_frames_with_visible_change():
    world = frame("in_world_wilson_live.png", 1)
    clock = [world.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.MOVE_FORWARD), world).status == ActionStatus.VERIFYING
    unchanged = replace(world, source_sequence=2, source_frame_id="still-2",
                        observed_monotonic=clock[0] + 0.01, screen_change=0.0,
                        gameplay_change=0.0)
    assert lifecycle.observe(unchanged) is None
    assert lifecycle.observe(replace(unchanged, source_sequence=3,
                                     source_frame_id="still-3")) is None
    moved = replace(unchanged, source_sequence=4, source_frame_id="moved-4",
                    screen_change=0.001, gameplay_change=0.01)
    verified = lifecycle.observe(moved)
    assert verified.status == ActionStatus.SUCCEEDED
    assert "gameplay ROI change=0.010000" in verified.reason


def test_canonical_pause_and_resume_require_fresh_perceived_states():
    world = frame("in_world_wilson_live.png", 1)
    paused = frame("in_world_auto_paused_live.png", 2)
    clock = [world.observed_monotonic + 0.001]
    lifecycle = ActionLifecycle(clock=lambda: clock[0])
    assert lifecycle.begin(sent(ActionName.PAUSE_WORLD), world).status == ActionStatus.VERIFYING
    assert lifecycle.observe(world) is None
    clock[0] = paused.observed_monotonic + 0.001
    assert lifecycle.observe(paused) is None
    paused_again = replace(paused, source_sequence=3, source_frame_id="paused-3")
    clock[0] = paused_again.observed_monotonic + 0.001
    assert lifecycle.observe(paused_again).status == ActionStatus.SUCCEEDED

    assert lifecycle.begin(sent(ActionName.RESUME_WORLD), paused_again).status == ActionStatus.VERIFYING
    resumed = frame("in_world_wilson_live.png", 4)
    resumed = replace(resumed, observed_monotonic=paused_again.observed_monotonic + 0.1)
    clock[0] = resumed.observed_monotonic + 0.001
    assert lifecycle.observe(resumed) is None
    resumed_again = replace(
        resumed, source_sequence=5, source_frame_id="resumed-5",
        observed_monotonic=resumed.observed_monotonic + 0.1,
    )
    clock[0] = resumed_again.observed_monotonic + 0.001
    assert lifecycle.observe(resumed_again).status == ActionStatus.SUCCEEDED


def test_live_auto_paused_screen_is_not_classified_as_alive():
    paused = frame("in_world_auto_paused_live.png", 1)
    assert paused.validity == ObservationValidity.VALID
    assert paused.screen == DSTScreen.PAUSED
    assert paused.screen_confidence >= 0.94
    assert frame("in_world_wilson_live.png", 2).screen == DSTScreen.IN_WORLD_IDLE


def test_gameplay_change_ignores_static_hud_region_and_tracks_world_roi():
    monitor = FrameChangeMonitor()
    base = Image.new("RGB", (128, 72), (20, 20, 20))
    _digest, first_screen_change, _frozen, first_gameplay_change = monitor.update(
        base, 1.0
    )
    assert first_screen_change is None and first_gameplay_change is None

    hud_only = base.copy()
    hud_only.paste((220, 220, 220), (0, 0, 10, 10))
    _digest, hud_change, _frozen, hud_gameplay_change = monitor.update(hud_only, 1.1)
    assert hud_change is not None and hud_change > 0
    assert hud_gameplay_change == 0

    world_shift = hud_only.copy()
    world_shift.paste((230, 230, 230), (50, 30, 80, 55))
    _digest, _screen_change, _frozen, gameplay_change = monitor.update(
        world_shift, 1.2
    )
    assert gameplay_change is not None and gameplay_change > 0.006


def test_reentry_world_list_uses_stable_farm_name_anchor():
    listed = frame("host_game_world_list_reentry_live.png", 1)
    assert listed.screen == DSTScreen.HOST_GAME_WORLD_LIST
    target, _ = click_request(ActionName.SELECT_EXISTING_WORLD, listed)
    assert target == DST_FIXED_1280X720.point("FARM_01", 1280, 720)


def test_supported_fixed_ui_profile_and_anchored_survivor_targets():
    frames = {
        ActionName.CLICK_HOST_GAME: frame("main_menu_after_reward.png", 1),
        ActionName.SELECT_EXISTING_WORLD: frame("host_game_world_list_live.png", 2),
        ActionName.START_EXISTING_WORLD: frame("host_game_world_selected_live.png", 3),
        ActionName.CONFIRM_MODS_DISABLED: mods_disabled_frame(4),
    }
    assert set(FIXED_UI_ACTION_TARGETS) == {action.value for action in frames}
    for action, observation in frames.items():
        expected = DST_FIXED_1280X720.point(
            FIXED_UI_ACTION_TARGETS[action.value], 1280, 720
        )
        point, viewport = click_request(action, replace(observation, detections=()))
        assert point == expected
        assert viewport.width == 1280 and viewport.height == 720
        assert 0 <= point.x <= 1 and 0 <= point.y <= 1

    for action, observation, anchor_name in (
        (ActionName.SELECT_SURVIVOR, frame("character_selection_live.png", 5), "character_select_wilson_icon"),
        (ActionName.START_SURVIVOR, frame("character_loadout_live.png", 6), "character_loadout_go_button"),
    ):
        anchor = next(item for item in observation.detections if item.kind == anchor_name)
        assert anchor.detected and anchor.verified and anchor.bounds is not None
        point, viewport = click_request(action, observation)
        assert point.x == pytest.approx((anchor.bounds.left + anchor.bounds.right) / 2)
        assert point.y == pytest.approx((anchor.bounds.top + anchor.bounds.bottom) / 2)
        assert viewport.width == 1280 and viewport.height == 720


def test_fixed_ui_targets_work_without_validation_flow_and_fail_closed_on_geometry():
    from runtime_agent.gameworker.activity import ActivityController

    menu = frame("main_menu_after_reward.png", 1)
    policy = ActivityController(validation_flow_enabled=False)
    assert policy.propose(menu) is None
    assert click_request(ActionName.CLICK_HOST_GAME, menu)[0] == (
        DST_FIXED_1280X720.point("HOST_GAME", 1280, 720)
    )
    with pytest.raises(ValueError, match="requires 1280x720"):
        click_request(ActionName.CLICK_HOST_GAME, replace(menu, frame_width=1920))


def test_start_survivor_uses_verified_anchor_and_fresh_verification():
    loadout = frame("character_loadout_live.png", 1)
    point, viewport = click_request(ActionName.START_SURVIVOR, loadout)
    go = next(item for item in loadout.detections if item.kind == "character_loadout_go_button")
    assert go.detected and go.verified and go.bounds is not None
    assert point.x == pytest.approx((go.bounds.left + go.bounds.right) / 2)
    assert point.y == pytest.approx((go.bounds.top + go.bounds.bottom) / 2)
    assert viewport.width == 1280 and viewport.height == 720

    lifecycle = ActionLifecycle(clock=lambda: loadout.observed_monotonic + 0.001)
    assert lifecycle.begin(sent(ActionName.START_SURVIVOR), loadout).status == ActionStatus.VERIFYING
    assert lifecycle.observe(loadout) is None

    loading = frame("dst_world_loading_live.png", 2)
    assert lifecycle.observe(loading) is None
    in_world = frame("in_world_wilson_live.png", 3)
    assert lifecycle.observe(in_world) is None
    in_world_again = replace(in_world, source_sequence=4, source_frame_id="in-world-4")
    assert lifecycle.observe(in_world_again).status == ActionStatus.SUCCEEDED


def test_survivor_selection_requires_fresh_observation_and_anchor():
    selection = frame("character_selection_live.png", 1)
    lifecycle = ActionLifecycle(clock=lambda: selection.observed_monotonic + 0.001)
    assert lifecycle.begin(sent(ActionName.SELECT_SURVIVOR), selection).status == ActionStatus.VERIFYING
    assert lifecycle.observe(selection) is None  # pre-action frame cannot verify
    stale = replace(selection, source_sequence=2, source_frame_id="stale-selection", fresh_until=0)
    assert lifecycle.observe(stale) is None

    no_anchor = replace(
        selection,
        detections=tuple(item for item in selection.detections if item.kind != "character_select_wilson_icon"),
    )
    with pytest.raises(ValueError, match="verified action anchor"):
        click_request(ActionName.SELECT_SURVIVOR, no_anchor)


def test_dead_and_reset_pending_are_never_verified_as_alive():
    death = frame("death_world_reset_live.png", 1)
    assert death.screen == DSTScreen.WORLD_RESET_PENDING
    assert death.screen != DSTScreen.IN_WORLD_IDLE
    from runtime_agent.gameworker.activity import ActivityController

    policy = ActivityController(validation_flow_enabled=False)
    policy.set_production_actions_enabled(True)
    assert policy.propose(death) is None
    dead = replace(
        death,
        screen=DSTScreen.DEAD,
        source_sequence=2,
        source_frame_id="explicit-dead-state",
    )
    assert dead.screen == DSTScreen.DEAD
    assert dead.screen != DSTScreen.IN_WORLD_IDLE
    assert policy.propose(dead) is None


def test_start_world_accepts_mod_warning_then_verifies_its_single_confirmation():
    selected = frame("host_game_world_selected_live.png", 1)
    lifecycle = ActionLifecycle(clock=lambda: time.monotonic() + 0.001)
    assert lifecycle.begin(sent(ActionName.START_EXISTING_WORLD), selected).status == ActionStatus.VERIFYING
    modal = mods_disabled_frame(2)
    modal2 = replace(modal, source_sequence=3, source_frame_id="mods-disabled-3")
    assert lifecycle.observe(modal) is None
    result = lifecycle.observe(modal2)
    assert result is not None and result.status == ActionStatus.SUCCEEDED

    assert lifecycle.begin(sent(ActionName.CONFIRM_MODS_DISABLED), modal2).status == ActionStatus.VERIFYING
    selected4 = frame("host_game_world_selected_live.png", 4)
    selected5 = frame("host_game_world_selected_live.png", 5)
    assert lifecycle.observe(selected4) is None
    result = lifecycle.observe(selected5)
    assert result is not None and result.status == ActionStatus.SUCCEEDED
