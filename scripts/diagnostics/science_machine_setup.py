"""One supervised Farm 01 console spawn through the canonical input driver."""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.runtime.display import DisplayEnvironment
from runtime_agent.gameworker.actions import ActionName, ActionStatus, GameActions
from runtime_agent.gameworker.capture import X11ScreenCapture
from runtime_agent.gameworker.config import InputBindings, WorkerMode
from runtime_agent.gameworker.geometry import CalibrationProfile
from runtime_agent.gameworker.input import DeadmanSafety, InputController, InputLease
from runtime_agent.gameworker.transitions import ActionLifecycle, click_request
from runtime_agent.gameworker.vision import AssetRegistry, DSTScreen, VisionDetector

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "runtime_agent/gameworker/dst/assets"
CONTROL = Path("/tmp/dst-science-operator.json")
ATTEMPT = Path("/tmp/dst-science-machine-attempted")
FRAME = Path("/tmp/dst-science-review.png")
ROUTE = {
    DSTScreen.MAIN_MENU: ActionName.CLICK_HOST_GAME,
    DSTScreen.HOST_GAME_WORLD_LIST: ActionName.SELECT_EXISTING_WORLD,
    DSTScreen.HOST_GAME_WORLD_SELECTED: ActionName.START_EXISTING_WORLD,
    DSTScreen.CHARACTER_SELECTION: ActionName.SELECT_SURVIVOR,
    DSTScreen.CHARACTER_SELECTION_HOVERED: ActionName.SELECT_SURVIVOR,
    DSTScreen.CHARACTER_LOADOUT: ActionName.START_SURVIVOR,
}
COMMAND = 'c_spawn("researchlab")'


def emit(**values):
    print(json.dumps(values, sort_keys=True), flush=True)


def await_operator(phase: str, timeout: float = 900, keepalive=None) -> dict:
    deadline = time.monotonic() + timeout
    next_keepalive = time.monotonic() + 2
    while time.monotonic() < deadline:
        now = time.monotonic()
        if keepalive is not None and now >= next_keepalive:
            keepalive()
            next_keepalive = now + 2
        try:
            value = json.loads(CONTROL.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            time.sleep(0.2)
            continue
        if value.get("phase") == phase:
            CONTROL.unlink(missing_ok=True)
            return value
        time.sleep(0.2)
    raise TimeoutError(f"operator checkpoint expired: {phase}")


def tap(controller, deadman, key):
    deadman.touch()
    controller.key_press(key)
    deadman.touch()


def type_shifted(controller, deadman, key):
    deadman.touch()
    controller.key_down("Shift_L")
    try:
        deadman.touch()
        controller.key_press(key)
    finally:
        controller.key_up("Shift_L")
        deadman.touch()


def type_command(controller, deadman):
    shifted = {
        "_": "underscore", "(": "parenleft",
        ")": "parenright", '"': "quotedbl",
    }
    for character in COMMAND:
        if character in shifted:
            type_shifted(controller, deadman, shifted[character])
        else:
            tap(controller, deadman, character)


def main():
    if ATTEMPT.exists():
        raise RuntimeError("one-shot spawn was already attempted; refusing to repeat")
    CONTROL.unlink(missing_ok=True)
    env = DisplayEnvironment(":99")
    detector = VisionDetector(AssetRegistry(ASSETS / "manifest.json"), default_threshold=0.8)
    capture = X11ScreenCapture(
        env, max_width=1280, max_height=720, timeout=8,
        runtime_id=1, runtime_generation=1, worker_generation=1,
    )
    controller = InputController(
        env, lease=InputLease(), max_actions_per_second=2,
        max_key_presses_per_second=6,
    )
    deadman = DeadmanSafety(controller, 12)
    deadman.start()
    actions = GameActions(
        controller, deadman, InputBindings(), mode=WorkerMode.ACTIVE,
        action_timeout=5, runtime_generation=1, worker_generation=1,
        runtime_id=1, allowed_actions=frozenset(ROUTE.values()),
    )
    generation = 0

    def observe():
        nonlocal generation
        frame = capture.capture()
        generation += 1
        observation = detector.analyze(
            frame,
            calibration=CalibrationProfile("dst", 1, 1280, 720, verified=True),
            observation_generation=generation,
            max_frame_age=4,
            deadline=time.monotonic() + 3,
        )
        emit(
            event="observe", sequence=frame.sequence,
            screen=observation.screen.value, ready=observation.production_ready,
            confidence=round(observation.screen_confidence, 4),
        )
        return frame, observation

    def stable(screen=None):
        deadline = time.monotonic() + 150
        previous = None
        count = 0
        while time.monotonic() < deadline:
            frame, observation = observe()
            valid = (
                observation.production_ready
                and observation.screen_confidence >= 0.94
                and observation.screen not in {DSTScreen.UNKNOWN, DSTScreen.LOADING}
                and (screen is None or observation.screen == screen)
            )
            count = count + 1 if valid and observation.screen == previous else 1 if valid else 0
            previous = observation.screen if valid else None
            if count >= 2:
                return frame, observation
            time.sleep(0.4)
        raise TimeoutError(f"no stable verified screen: {screen}")

    def click_action(name, before):
        target, viewport = click_request(name, before)
        sent = actions.execute(name, target=target, viewport=viewport)
        lifecycle = ActionLifecycle()
        pending = lifecycle.begin(sent, before)
        emit(event="action", action=name.value, transport=sent.status.value)
        if pending.status != ActionStatus.VERIFYING:
            raise RuntimeError(f"action rejected: {pending.reason}")
        deadline = time.monotonic() + 125
        while time.monotonic() < deadline:
            _, observation = observe()
            result = lifecycle.observe(observation)
            if result is not None:
                emit(event="transition", action=name.value, result=result.status.value,
                     screen=observation.screen.value)
                if result.status != ActionStatus.SUCCEEDED:
                    raise RuntimeError(result.reason)
                return observation
            time.sleep(0.4)
        raise TimeoutError(f"transition timed out: {name.value}")

    try:
        frame, observation = stable()
        host_retried = False
        survivor_retried = False
        for _ in range(8):
            if observation.screen == DSTScreen.PAUSED:
                raise RuntimeError("world is paused; stop and use the normal resume path")
            if observation.screen == DSTScreen.IN_WORLD_IDLE:
                break
            action = ROUTE.get(observation.screen)
            if action is None:
                raise RuntimeError(f"unsupported entry screen: {observation.screen.value}")
            try:
                observation = click_action(action, observation)
            except RuntimeError as exc:
                if str(exc) != "verified transition deadline elapsed":
                    raise
                if action == ActionName.CLICK_HOST_GAME and not host_retried:
                    retry_screen = DSTScreen.MAIN_MENU
                    anchor_name = "main_menu_host_game"
                elif (
                    action == ActionName.SELECT_SURVIVOR
                    and not survivor_retried
                ):
                    retry_screen = observation.screen
                    anchor_name = (
                        "character_select_wilson_hover"
                        if observation.screen == DSTScreen.CHARACTER_SELECTION_HOVERED
                        else "character_select_wilson_icon"
                    )
                else:
                    raise
                original_sequence = observation.source_sequence
                _, retry_observation = stable(retry_screen)
                retry_anchor = next(
                    (
                        item for item in retry_observation.detections
                        if item.kind == anchor_name
                        and item.detected and item.verified
                        and item.bounds is not None and item.confidence >= 0.94
                    ),
                    None,
                )
                if (
                    retry_observation.source_sequence <= original_sequence
                    or retry_observation.screen_change is None
                    or retry_observation.screen_change >= 0.02
                    or retry_anchor is None
                ):
                    raise RuntimeError(
                        f"{action.value} retry withheld: fresh unchanged anchor absent"
                    ) from exc
                emit(event="safe_retry", action=action.value,
                     sequence=retry_observation.source_sequence,
                     screen_change=retry_observation.screen_change)
                host_retried |= action == ActionName.CLICK_HOST_GAME
                survivor_retried |= action == ActionName.SELECT_SURVIVOR
                observation = click_action(action, retry_observation)
            frame, observation = stable()
        if observation.screen != DSTScreen.IN_WORLD_IDLE:
            raise RuntimeError(f"world entry did not finish: {observation.screen.value}")

        frame.image().save(FRAME)
        emit(event="checkpoint", phase="choose_ground", frame=str(FRAME))
        point = await_operator("ground")
        from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport

        ground_point = NormalizedPoint(float(point["x"]), float(point["y"]))
        viewport = Viewport(1280, 720)
        controller.mouse_move(
            ground_point,
            viewport,
        )
        controller.driver.focus_game_at_pointer()

        def keepalive():
            controller.mouse_move(ground_point, viewport)

        frame, _ = observe()
        frame.image().save(FRAME)
        emit(event="checkpoint", phase="verify_ground", frame=str(FRAME))
        await_operator(
            "ground_verified", keepalive=keepalive
        )

        type_shifted(controller, deadman, "grave")
        time.sleep(0.5)
        frame, _ = observe()
        frame.image().save(FRAME)
        emit(event="checkpoint", phase="verify_console", frame=str(FRAME))
        console = await_operator(
            "console", keepalive=keepalive
        )
        if console.get("visible") is not True:
            raise RuntimeError("console opening was not visually confirmed")
        if console.get("remote") is False:
            tap(controller, deadman, "Control_L")
            time.sleep(0.4)
            frame, _ = observe()
            frame.image().save(FRAME)
            emit(event="checkpoint", phase="verify_remote", frame=str(FRAME))
            remote = await_operator(
                "remote_verified", keepalive=keepalive
            )
            if remote.get("visible") is False and remote.get("remote") is True:
                type_shifted(controller, deadman, "grave")
                time.sleep(0.5)
                frame, _ = observe()
                frame.image().save(FRAME)
                emit(event="checkpoint", phase="verify_reopened_remote", frame=str(FRAME))
                console = await_operator(
                    "console_reopened",
                    keepalive=keepalive,
                )
            elif remote.get("visible") is True and remote.get("remote") is True:
                console = remote
            else:
                raise RuntimeError("remote console state was not visually verified")
            if console.get("visible") is not True or console.get("remote") is not True:
                raise RuntimeError("reopened console is not verified Remote")
        elif console.get("remote") is not True:
            raise RuntimeError("Remote mode was not visually confirmed")

        type_command(controller, deadman)
        frame, _ = observe()
        frame.image().save(FRAME)
        emit(event="checkpoint", phase="verify_command", frame=str(FRAME),
             expected=COMMAND)
        await_operator(
            "submit", keepalive=keepalive
        )

        ATTEMPT.write_text(f"{time.time_ns()}\n", encoding="ascii")
        tap(controller, deadman, "Return")
        time.sleep(1.0)
        frame, _ = observe()
        frame.image().save(FRAME)
        emit(event="checkpoint", phase="verify_spawn", frame=str(FRAME))
        console = await_operator(
            "close_console", keepalive=keepalive
        )
        if console.get("visible") is True:
            tap(controller, deadman, "Escape")
            time.sleep(0.5)
        elif console.get("visible") is not False:
            raise RuntimeError("console visibility is ambiguous; do not send another key")
        frame, observation = stable(DSTScreen.IN_WORLD_IDLE)
        frame.image().save(FRAME)
        emit(event="checkpoint", phase="verify_station_and_gift", frame=str(FRAME),
             screen=observation.screen.value)
        await_operator(
            "setup_verified", keepalive=keepalive
        )
        emit(event="setup_complete", command_attempted=True)
    finally:
        try:
            actions.set_mode(WorkerMode.DISABLED)
            actions.shutdown()
        finally:
            deadman.close()
            controller.close()
            capture.close()


if __name__ == "__main__":
    main()
