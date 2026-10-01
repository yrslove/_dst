from __future__ import annotations

import pickle
import threading
import time

import pytest

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.actions import (
    Action,
    ActionExecutor,
    ActionName,
    ActionStatus,
)
from runtime_agent.gameworker.config import InputBindings, WorkerConfig, WorkerMode
from runtime_agent.gameworker.dst.worker import DSTGameWorker
from runtime_agent.gameworker.fixed_ui import DST_FIXED_1280X720
from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport
from runtime_agent.gameworker.input import (
    DeadmanSafety,
    FakeInputDriver,
    InputController,
    InputError,
    InputLease,
    emergency_release_all,
)


def action(
    action_id: str,
    name: ActionName = ActionName.INTERACT,
    *,
    duration: float | None = None,
    runtime_generation: int = 7,
    worker_generation: int = 3,
    runtime_id: int = 2,
    deadline: float | None = None,
) -> Action:
    if deadline is None:
        deadline = time.monotonic() + 0.9
    return Action(
        action_id,
        name,
        runtime_generation,
        worker_generation,
        runtime_id=runtime_id,
        duration=duration,
        deadline=deadline,
    )


def executor(
    *,
    mode: WorkerMode = WorkerMode.ACTIVE,
    queue_size: int = 4,
    driver: FakeInputDriver | None = None,
    deadman_timeout: float = 1.0,
    allowed_actions: frozenset[ActionName] | None = None,
):
    driver = driver or FakeInputDriver()
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=100,
        max_key_presses_per_second=100,
        driver=driver,
    )
    deadman = DeadmanSafety(controller, deadman_timeout)
    value = ActionExecutor(
        controller,
        deadman,
        InputBindings(),
        runtime_generation=7,
        worker_generation=3,
        runtime_id=2,
        mode=mode,
        action_timeout=1.0,
        queue_size=queue_size,
        allowed_actions=allowed_actions,
    )
    return value, controller, driver, deadman


def test_action_and_result_are_pickle_safe_and_duplicate_is_idempotent():
    value, _controller, driver, _deadman = executor()
    request = pickle.loads(pickle.dumps(action("same")))

    first = value.execute(request)
    second = value.execute(request)
    restored = pickle.loads(pickle.dumps(first))
    value.shutdown()

    assert first.status == ActionStatus.SENT
    assert second == first == restored
    assert [event.operation for event in driver.events].count("key_down") == 1


def test_canonical_pause_and_resume_use_the_bound_cancel_key():
    value, controller, driver, _deadman = executor()
    paused = value.execute(action("pause-world", ActionName.PAUSE_WORLD))
    resumed = value.execute(action("resume-world", ActionName.RESUME_WORLD))
    value.shutdown()

    assert paused.status == resumed.status == ActionStatus.SENT
    assert [
        (event.operation, event.value)
        for event in driver.events
        if event.value == "Escape"
    ] == [
        ("key_down", "Escape"), ("key_up", "Escape"),
        ("key_down", "Escape"), ("key_up", "Escape"),
    ]
    assert not controller.has_held_inputs


def test_open_crafting_menu_uses_the_canonical_build_key():
    value, controller, driver, _deadman = executor()
    result = value.execute(action("crafting-menu", ActionName.OPEN_CRAFTING_MENU))
    value.shutdown()

    assert result.status == ActionStatus.SENT
    assert [
        (event.operation, event.value)
        for event in driver.events
        if event.operation in {"mouse_move", "focus_game", "key_down", "key_up"}
    ] == [
        ("mouse_move", (959, 360)),
        ("focus_game", "DST"),
        ("key_down", "b"),
        ("key_up", "b"),
    ]
    assert not controller.has_held_inputs


def test_pause_resume_and_movement_capabilities_are_available_without_validation():
    worker = DSTGameWorker(
        WorkerConfig(
            plugin="dst",
            mode=WorkerMode.ACTIVE,
            validation_flow_enabled=False,
        )
    )
    value, controller, _driver, _deadman = executor(
        allowed_actions=worker._permitted_active_actions()
    )

    results = (
        value.execute(action("explicit-pause", ActionName.PAUSE_WORLD)),
        value.execute(action("explicit-resume", ActionName.RESUME_WORLD)),
        value.execute(action("explicit-move", ActionName.MOVE_FORWARD, duration=0.2)),
    )
    value.shutdown()

    assert [result.status for result in results] == [
        ActionStatus.SENT,
        ActionStatus.SENT,
        ActionStatus.SENT,
    ]
    assert not controller.has_held_inputs


def test_concurrent_duplicate_delivery_executes_input_once():
    value, _controller, driver, _deadman = executor()
    request = action("concurrent-duplicate")
    barrier = threading.Barrier(8)
    results = []

    def deliver():
        barrier.wait()
        results.append(value.execute(request))

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    value.shutdown()

    assert len(results) == 8
    assert len(set(results)) == 1
    assert [event.operation for event in driver.events].count("key_down") == 1


def test_generation_deadline_and_duration_validation_are_terminal_without_input():
    value, _controller, driver, _deadman = executor()

    results = (
        value.execute(action("old-runtime", runtime_generation=6)),
        value.execute(action("old-worker", worker_generation=2)),
        value.execute(action("old-runtime-id", runtime_id=1)),
        value.execute(action("expired", deadline=time.monotonic() - 1)),
        value.execute(action("no-duration", ActionName.MOVE_FORWARD)),
        value.execute(action("too-long", ActionName.MOVE_FORWARD, duration=2.0)),
    )
    value.shutdown()

    assert [result.status for result in results] == [
        ActionStatus.STALE_GENERATION,
        ActionStatus.STALE_GENERATION,
        ActionStatus.STALE_GENERATION,
        ActionStatus.TIMED_OUT,
        ActionStatus.REJECTED,
        ActionStatus.REJECTED,
    ]
    assert all(result.terminal for result in results)
    assert driver.events == []


def test_input_controller_tracks_and_independently_releases_all_inputs():
    driver = FakeInputDriver()
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=10,
        max_key_presses_per_second=10,
        driver=driver,
    )
    controller.key_down("w")
    controller.key_down("a")
    controller.mouse_down(1)
    driver.fail_next("key_up")

    released = controller.release_all(reason="test")
    again = controller.release_all(reason="idempotency")

    assert not released
    assert again
    assert controller.pressed_keys == frozenset()
    assert controller.pressed_mouse_buttons == frozenset()
    assert not controller.has_held_inputs
    assert not controller.uncertain_inputs
    assert driver.pressed_keys == set()
    assert driver.pressed_mouse_buttons == set()
    assert sum(event.operation == "key_up" for event in driver.events) == 2


def test_key_down_failure_attempts_cleanup_and_clears_logical_state():
    driver = FakeInputDriver(fail_operations={"key_down"})
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=10,
        max_key_presses_per_second=10,
        driver=driver,
    )

    with pytest.raises(InputError):
        controller.key_down("w")

    assert controller.pressed_keys == frozenset()
    assert [event.operation for event in driver.events] == ["key_up"]


def test_observe_suppresses_gameplay_but_release_all_remains_available():
    value, controller, driver, _deadman = executor(mode=WorkerMode.OBSERVE)

    suppressed = value.execute(action("observe"))
    released = value.execute(action("release", ActionName.RELEASE_ALL))
    value.shutdown()

    assert suppressed.status == ActionStatus.SUPPRESSED
    assert suppressed.dry_run
    assert released.status == ActionStatus.COMPLETED
    assert not controller.has_held_inputs
    assert driver.events == []


def test_revoke_during_click_settle_prevents_new_button_input():
    focused = threading.Event()

    class DelayedDriver(FakeInputDriver):
        settle_seconds = .5

        def focus_game_at_pointer(self):
            super().focus_game_at_pointer()
            focused.set()

    driver = DelayedDriver()
    controller = InputController(lease=InputLease(), max_actions_per_second=10,
                                 max_key_presses_per_second=10, driver=driver)
    failures = []

    def click():
        try:
            controller.click(NormalizedPoint(.1, .7), Viewport(1280, 720))
        except InputError as exc:
            failures.append(exc)

    thread = threading.Thread(target=click)
    thread.start()
    assert focused.wait(1)
    controller.revoke()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert failures
    assert [event.operation for event in driver.events] == [
        "mouse_move", "mouse_move", "focus_game"
    ]
    assert "mouse_down" not in {event.operation for event in driver.events}
    assert not controller.has_held_inputs


def test_cancel_running_movement_releases_key_and_returns_terminal_result():
    entered = threading.Event()
    driver = FakeInputDriver(
        before_operation=lambda operation: entered.set()
        if operation == "key_down"
        else None
    )
    value, controller, _driver, _deadman = executor(driver=driver)
    ticket = value.submit(action("move", ActionName.MOVE_FORWARD, duration=0.8))
    assert entered.wait(1)

    assert value.cancel_action("move")
    result = ticket.wait(1)
    value.shutdown()

    assert result is not None
    assert result.status == ActionStatus.CANCELLED
    assert not controller.has_held_inputs
    assert driver.pressed_keys == set()


def test_opposite_movement_preempts_without_conflicting_held_keys():
    entered = threading.Event()
    driver = FakeInputDriver(
        before_operation=lambda operation: entered.set()
        if operation == "key_down"
        else None
    )
    value, controller, _driver, _deadman = executor(driver=driver)
    left = value.submit(action("left", ActionName.TURN_LEFT, duration=0.8))
    assert entered.wait(1)
    right = value.submit(action("right", ActionName.TURN_RIGHT, duration=0.02))

    left_result = left.wait(1)
    right_result = right.wait(1)
    value.shutdown()

    assert left_result is not None and left_result.status == ActionStatus.PREEMPTED
    assert right_result is not None and right_result.status == ActionStatus.SENT
    assert not controller.has_held_inputs
    operations = [(event.operation, event.value) for event in driver.events]
    assert operations.index(("key_up", "a")) < operations.index(("key_down", "d"))


def test_game_lost_preempts_running_action_and_blocks_new_input():
    entered = threading.Event()
    driver = FakeInputDriver(
        before_operation=lambda operation: entered.set()
        if operation == "key_down"
        else None
    )
    value, controller, _driver, _deadman = executor(driver=driver)
    running = value.submit(action("running", ActionName.MOVE_FORWARD, duration=0.8))
    assert entered.wait(1)

    value.update_safety(
        configured_mode=WorkerMode.ACTIVE,
        effective_mode=WorkerMode.OBSERVE,
        runtime_verified=True,
        game_ready=False,
        healthy=True,
        paused=True,
        stopping=False,
    )
    result = running.wait(1)
    blocked = value.execute(action("after-loss"))
    value.shutdown()

    assert result is not None and result.status == ActionStatus.PREEMPTED
    assert blocked.status == ActionStatus.GAME_NOT_READY
    assert not controller.has_held_inputs
    assert driver.pressed_keys == set()


def test_deadline_during_execution_times_out_and_releases_key():
    value, controller, driver, _deadman = executor()
    request = action(
        "short-deadline",
        ActionName.MOVE_FORWARD,
        duration=0.5,
        deadline=time.monotonic() + 0.04,
    )

    result = value.execute(request)
    value.shutdown()

    assert result.status == ActionStatus.TIMED_OUT
    assert not controller.has_held_inputs
    assert driver.pressed_keys == set()


def test_full_normal_queue_cannot_starve_release_all():
    entered = threading.Event()
    driver = FakeInputDriver(
        before_operation=lambda operation: entered.set()
        if operation == "key_down"
        else None
    )
    value, controller, _driver, _deadman = executor(queue_size=1, driver=driver)
    first = value.submit(action("first", ActionName.MOVE_FORWARD, duration=0.8))
    assert entered.wait(1)
    queued = value.submit(action("queued"))
    rejected = value.submit(action("overflow"))

    released = value.execute(action("emergency", ActionName.RELEASE_ALL))
    first_result = first.wait(1)
    queued_result = queued.wait(1)
    after_stop = value.execute(action("after-emergency"))
    value.shutdown()

    assert released.status == ActionStatus.COMPLETED
    assert first_result is not None and first_result.status == ActionStatus.PREEMPTED
    assert queued_result is not None and queued_result.status == ActionStatus.PREEMPTED
    assert rejected.wait(0).status == ActionStatus.REJECTED
    assert not controller.has_held_inputs
    assert after_stop.status == ActionStatus.SAFETY_BLOCKED


def test_deadman_releases_held_key_blocks_input_and_resets_explicitly():
    driver = FakeInputDriver()
    controller = InputController(
        lease=InputLease(),
        max_actions_per_second=10,
        max_key_presses_per_second=10,
        driver=driver,
    )
    deadman = DeadmanSafety(controller, 0.04)
    deadman.start()
    controller.key_down("w")

    assert deadman._tripped.wait(1)
    assert not controller.has_held_inputs
    assert controller.revoked
    deadman.reset()
    controller.activate()
    controller.key_down("a")
    controller.key_up("a")
    deadman.close()

    assert not deadman.tripped
    assert driver.pressed_keys == set()


def test_shutdown_preempts_running_and_queued_actions_exactly_once():
    entered = threading.Event()
    driver = FakeInputDriver(
        before_operation=lambda operation: entered.set()
        if operation == "key_down"
        else None
    )
    value, controller, _driver, _deadman = executor(driver=driver)
    running = value.submit(action("running", ActionName.MOVE_FORWARD, duration=0.8))
    assert entered.wait(1)
    queued = value.submit(action("queued"))

    value.shutdown()

    assert running.wait(0) is not None
    assert queued.wait(0) is not None
    assert not controller.has_held_inputs
    assert driver.pressed_keys == set()


def test_worker_config_validates_stage2_bounds():
    for config in (
        WorkerConfig(action_queue_size=0),
        WorkerConfig(input_subprocess_timeout=0),
    ):
        try:
            config.validate()
        except ValueError:
            continue
        raise AssertionError("invalid Stage 2 configuration was accepted")


def test_reward_close_click_uses_canonical_executor_and_allowed_anchor():
    value, _controller, driver, _deadman = executor(
        allowed_actions=frozenset({ActionName.CLICK_REWARD_CLOSE})
    )
    request = Action(
        "reward-close",
        ActionName.CLICK_REWARD_CLOSE,
        7,
        3,
        runtime_id=2,
        deadline=time.monotonic() + 0.9,
        parameters=(
            ("x", 0.5),
            ("y", 0.88),
            ("width", 1280),
            ("height", 720),
        ),
    )

    result = value.execute(request)
    value.shutdown()

    assert result.status == ActionStatus.SENT
    assert [event.operation for event in driver.events] == [
        "mouse_move",
        "mouse_move",
        "focus_game",
        "mouse_down",
        "mouse_up",
    ]
    assert driver.events[0].value == (656, 633)
    assert driver.events[1].value == (640, 633)


def test_each_allowed_host_game_attempt_refreshes_pointer_and_focus():
    value, _controller, driver, _deadman = executor(
        allowed_actions=frozenset({ActionName.CLICK_HOST_GAME})
    )
    for action_id in ("host-first", "host-guarded-retry"):
        result = value.execute(Action(
            action_id,
            ActionName.CLICK_HOST_GAME,
            7,
            3,
            runtime_id=2,
            deadline=time.monotonic() + 0.9,
            parameters=(("x", .1), ("y", .5), ("width", 1280), ("height", 720)),
        ))
        assert result.status == ActionStatus.SENT
    value.shutdown()

    assert [event.operation for event in driver.events] == [
        "mouse_move", "mouse_move", "focus_game", "mouse_down", "mouse_up",
        "mouse_move", "mouse_move", "focus_game", "mouse_down", "mouse_up",
    ]


def test_fixed_ui_click_rejects_mismatched_display_geometry_before_input():
    value, _controller, driver, _deadman = executor(
        allowed_actions=frozenset({ActionName.CLICK_HOST_GAME})
    )
    result = value.execute(Action(
        "host-wrong-geometry",
        ActionName.CLICK_HOST_GAME,
        7,
        3,
        runtime_id=2,
        deadline=time.monotonic() + 0.9,
        parameters=(
            ("x", 0.0953125),
            ("y", 0.5465277777777777),
            ("width", 1920),
            ("height", 1080),
        ),
    ))
    value.shutdown()

    assert result.status == ActionStatus.REJECTED
    assert result.reason == "invalid action anchor"
    assert driver.events == []


def test_start_existing_world_guard_accepts_saved_live_resume_anchor():
    value, _controller, driver, _deadman = executor(
        allowed_actions=frozenset({ActionName.START_EXISTING_WORLD})
    )
    point = NormalizedPoint(0.828515625, 0.9527777777777777)
    viewport = Viewport(1280, 720)
    request = Action(
        "resume-world",
        ActionName.START_EXISTING_WORLD,
        7,
        3,
        runtime_id=2,
        deadline=time.monotonic() + 0.9,
        parameters=(
            ("x", point.x),
            ("y", point.y),
            ("width", viewport.width),
            ("height", viewport.height),
        ),
    )

    result = value.execute(request)
    value.shutdown()

    assert result.status == ActionStatus.SENT
    assert driver.events[0].operation == "mouse_move"
    assert driver.events[1].value == viewport.point(point)


def test_mods_disabled_confirmation_uses_the_fixed_profile_continue_point():
    value, controller, driver, _deadman = executor(
        allowed_actions=frozenset({ActionName.CONFIRM_MODS_DISABLED})
    )
    point = DST_FIXED_1280X720.point("MODS_DISABLED_CONTINUE", 1280, 720)
    request = Action(
        "confirm-missing-mod",
        ActionName.CONFIRM_MODS_DISABLED,
        7,
        3,
        runtime_id=2,
        deadline=time.monotonic() + 0.9,
        parameters=(("x", point.x), ("y", point.y), ("width", 1280), ("height", 720)),
    )

    result = value.execute(request)
    value.shutdown()

    assert result.status == ActionStatus.SENT
    assert driver.events[1].value == Viewport(1280, 720).point(point)
    assert not controller.has_held_inputs


def test_parent_emergency_release_attempts_every_binding_independently(monkeypatch):
    calls = []

    class Driver:
        def __init__(self, _environment):
            pass

        def key_up(self, key):
            calls.append(("key", key))
            if key == "a":
                raise InputError("one release failed")

        def mouse_up(self, button):
            calls.append(("mouse", button))

        def close(self):
            pass

    monkeypatch.setattr("runtime_agent.gameworker.input.XpraInputDriver", Driver)

    emergency_release_all(DisplayEnvironment(":99"), InputBindings())

    assert {value for kind, value in calls if kind == "key"} == {
        "w",
        "s",
        "a",
        "d",
        "space",
        "tab",
        "Escape",
    }
    assert [value for kind, value in calls if kind == "mouse"] == [1, 2, 3, 4, 5]


def test_emergency_release_unavailable_bridge_does_not_fail_shutdown(monkeypatch):
    attempts = []

    def unavailable(_environment):
        attempts.append("start")
        raise InputError("xpra input server connection lost: BlockingIOError")

    monkeypatch.setattr("runtime_agent.gameworker.input.XpraInputDriver", unavailable)
    emergency_release_all(DisplayEnvironment(":99"), InputBindings())
    assert attempts == ["start"]


def test_emergency_release_stops_writing_once_channel_closes(monkeypatch):
    drivers = []

    class Driver:
        closed = False

        def __init__(self, _environment):
            self.calls = []
            drivers.append(self)

        def key_up(self, key):
            self.calls.append(key)
            self.closed = True
            raise InputError("xpra input operation failed: BlockingIOError")

        def close(self):
            self.closed = True

    monkeypatch.setattr("runtime_agent.gameworker.input.XpraInputDriver", Driver)
    emergency_release_all(DisplayEnvironment(":99"), InputBindings())
    assert drivers[0].calls == ["Escape"]
    assert drivers[0].closed


def test_controller_close_discards_uncertain_input_after_broken_transport():
    class BrokenDriver(FakeInputDriver):
        closed = False

        def key_up(self, _key):
            self.closed = True
            raise InputError("xpra input operation failed: BlockingIOError")

        def close(self):
            self.closed = True

    driver = BrokenDriver()
    controller = InputController(
        lease=InputLease(), max_actions_per_second=10,
        max_key_presses_per_second=10, driver=driver,
    )
    controller.key_down("w")
    assert not controller.close()
    assert controller.revoked
    assert not controller.has_held_inputs
    assert not controller.uncertain_inputs
    assert driver.closed
    assert controller.close()
    with pytest.raises(InputError, match="revoked"):
        controller.key_down("a")


def test_next_controller_uses_fresh_driver_after_cleanup(monkeypatch):
    drivers = []

    class Driver(FakeInputDriver):
        def __init__(self, _environment, *, timeout):
            super().__init__()
            self.closed = False
            drivers.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr("runtime_agent.gameworker.input.XpraInputDriver", Driver)
    options = {
        "environment": DisplayEnvironment(":99"), "lease": InputLease(),
        "max_actions_per_second": 10, "max_key_presses_per_second": 10,
    }
    first = InputController(**options)
    first.close()
    second = InputController(**options)
    second.click(NormalizedPoint(0.1, 0.7), Viewport(1280, 720))
    second.close()

    assert len(drivers) == 2
    assert drivers[0] is not drivers[1]
    assert drivers[0].closed and drivers[1].closed
    assert [event.operation for event in drivers[1].events][-2:] == [
        "mouse_down", "mouse_up",
    ]


def test_fresh_channel_acquisition_failure_sends_no_gameplay_input(monkeypatch):
    calls = []

    def unavailable(_environment, *, timeout):
        calls.append("acquire")
        raise InputError("private xpra input channel unavailable")

    monkeypatch.setattr("runtime_agent.gameworker.input.XpraInputDriver", unavailable)
    with pytest.raises(InputError, match="unavailable"):
        InputController(
            DisplayEnvironment(":99"), lease=InputLease(),
            max_actions_per_second=10, max_key_presses_per_second=10,
        )
    assert calls == ["acquire"]
