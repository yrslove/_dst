"""Real saved frames exercise the generic action outcome contract."""
from dataclasses import replace

from PIL import Image
from test_dst_behavior import ASSETS, analyze_image

from runtime_agent.gameworker.actions import ActionName, ActionResult, ActionStatus
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


def test_host_game_click_uses_the_center_of_the_rendered_anchor_text():
    menu = frame("main_menu_after_reward.png", 1)
    anchor = next(
        item for item in menu.detections if item.kind == "main_menu_host_game"
    )
    point, viewport = click_request(ActionName.CLICK_HOST_GAME, menu)
    assert anchor.bounds is not None
    assert point.x == anchor.bounds.left + 0.4 * (
        anchor.bounds.right - anchor.bounds.left
    )
    assert point.y == (anchor.bounds.top + anchor.bounds.bottom) / 2
    assert viewport.width == 1280 and viewport.height == 720


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
    assert lifecycle.observe(changed).status == ActionStatus.SUCCEEDED

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
    )
    target, viewport = click_request(ActionName.SELECT_SURVIVOR, selected)
    anchor = next(
        item for item in selected.detections
        if item.kind == "character_select_wilson_icon"
    )
    assert anchor.bounds is not None
    assert anchor.bounds.left < target.x < anchor.bounds.right
    assert anchor.bounds.top < target.y < anchor.bounds.bottom
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
    assert lifecycle.observe(moved).status == ActionStatus.SUCCEEDED


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
    assert 0.25 < target.x < 0.45 and 0.24 < target.y < 0.34
