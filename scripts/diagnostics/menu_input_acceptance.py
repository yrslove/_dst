"""Scoped, bounded real DST menu input acceptance test; worker service stays disabled."""
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


def emit(**values):
    print(json.dumps(values, sort_keys=True), flush=True)


def main():
    environment = DisplayEnvironment(":99", xauthority="/home/dst/.Xauthority")
    assets = Path(__file__).resolve().parents[2] / "runtime_agent/gameworker/dst/assets"
    detector = VisionDetector(AssetRegistry(assets / "manifest.json"), default_threshold=.8)
    capture = X11ScreenCapture(environment, max_width=1280, max_height=720,
                               timeout=8, runtime_id=1, runtime_generation=1,
                               worker_generation=1)
    controller = None
    deadman = None
    actions = None
    lifecycle = ActionLifecycle()
    generation = 0
    def observe():
        nonlocal generation
        frame = capture.capture()
        generation += 1
        observation = detector.analyze(
            frame, calibration=CalibrationProfile("dst", 1, 1280, 720, verified=True),
            observation_generation=generation, max_frame_age=4,
            deadline=time.monotonic() + 3,
        )
        emit(event="observation", sequence=frame.sequence, screen=observation.screen.value,
             confidence=round(observation.screen_confidence, 6),
             ready=observation.production_ready)
        return observation

    def stable(screen):
        deadline = time.monotonic() + 25
        count = 0
        last = None
        while time.monotonic() < deadline:
            last = observe()
            count = count + 1 if (last.production_ready and last.screen == screen
                                  and last.screen_confidence >= .94) else 0
            if count >= 2:
                return last
            time.sleep(.25)
        raise RuntimeError(f"source screen {screen.value} unverified: {last.screen.value}")

    def action(name, source):
        before = stable(source)
        point, viewport = click_request(name, before)
        sent = actions.execute(name, target=point, viewport=viewport)
        emit(event="transport", action=name.value, status=sent.status.value, reason=sent.reason)
        verifying = lifecycle.begin(sent, before)
        if verifying.status != ActionStatus.VERIFYING:
            raise RuntimeError(f"action did not enter verification: {verifying}")
        while True:
            observation = observe()
            result = lifecycle.observe(observation)
            if result is not None:
                emit(event="verified", action=name.value, status=result.status.value,
                     screen=observation.screen.value,
                     confidence=round(observation.screen_confidence, 6),
                     reason=result.reason)
                if result.status != ActionStatus.SUCCEEDED:
                    raise RuntimeError(result.reason)
                return observation
            time.sleep(.25)

    try:
        initial = observe()
        if initial.screen not in {DSTScreen.MAIN_MENU, DSTScreen.OPTIONS_DISCARD_CONFIRM}:
            raise RuntimeError(f"unexpected initial screen {initial.screen.value}")
        controller = InputController(environment, lease=InputLease(),
                                     max_actions_per_second=2,
                                     max_key_presses_per_second=6)
        deadman = DeadmanSafety(controller, 12)
        deadman.start()
        actions = GameActions(controller, deadman, InputBindings(),
                              mode=WorkerMode.ACTIVE, action_timeout=5,
                              runtime_generation=1, worker_generation=1, runtime_id=1,
                              allowed_actions=frozenset({ActionName.CLICK_OPTIONS,
                                                         ActionName.CLICK_BACK,
                                                         ActionName.DISCARD_OPTIONS}))
        if initial.screen == DSTScreen.OPTIONS_DISCARD_CONFIRM:
            action(ActionName.DISCARD_OPTIONS, DSTScreen.OPTIONS_DISCARD_CONFIRM)
        stable(DSTScreen.MAIN_MENU)
        opened = action(ActionName.CLICK_OPTIONS, DSTScreen.MAIN_MENU)
        if opened.screen != DSTScreen.OPTIONS:
            raise RuntimeError("Options transition absent")
        backed = action(ActionName.CLICK_BACK, DSTScreen.OPTIONS)
        if backed.screen == DSTScreen.OPTIONS_DISCARD_CONFIRM:
            backed = action(ActionName.DISCARD_OPTIONS, DSTScreen.OPTIONS_DISCARD_CONFIRM)
        if backed.screen != DSTScreen.MAIN_MENU:
            raise RuntimeError("Main menu transition absent")
        emit(event="acceptance", status="SUCCEEDED", final_screen=backed.screen.value)
    finally:
        if actions is not None:
            actions.set_mode(WorkerMode.DISABLED)
            actions.shutdown()
        if deadman is not None:
            deadman.close()
        if controller is not None:
            controller.close()
        capture.close()


if __name__ == "__main__":
    main()
