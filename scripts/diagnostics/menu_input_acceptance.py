"""Scoped, bounded real DST menu input acceptance test; worker service stays disabled."""

from __future__ import annotations

import json
import subprocess
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


def runtime_metric(name):
    return int(
        subprocess.check_output(
            ["systemctl", "show", "-p", name, "--value", "dst-runtime-agent"]
        )
        .decode()
        .strip()
    )


def main():
    environment = DisplayEnvironment(":99", xauthority="/home/dst/.Xauthority")
    assets = Path(__file__).resolve().parents[2] / "runtime_agent/gameworker/dst/assets"
    detector = VisionDetector(
        AssetRegistry(assets / "manifest.json"), default_threshold=0.8
    )
    capture = X11ScreenCapture(
        environment,
        max_width=1280,
        max_height=720,
        timeout=8,
        runtime_id=1,
        runtime_generation=1,
        worker_generation=1,
    )
    controller = None
    deadman = None
    actions = None
    lifecycle = ActionLifecycle()
    generation = 0
    capture_times = []
    perception_times = []
    cpu_before = runtime_metric("CPUUsageNSec")
    memory_before = runtime_metric("MemoryCurrent")
    test_started = time.monotonic()

    def observe():
        nonlocal generation
        capture_started = time.monotonic()
        frame = capture.capture()
        capture_finished = time.monotonic()
        generation += 1
        observation = detector.analyze(
            frame,
            calibration=CalibrationProfile("dst", 1, 1280, 720, verified=True),
            observation_generation=generation,
            max_frame_age=4,
            deadline=time.monotonic() + 3,
        )
        analyzed = time.monotonic()
        capture_times.append(capture_finished)
        perception_times.append(observation.perception_latency)
        emit(
            event="observation",
            sequence=frame.sequence,
            screen=observation.screen.value,
            confidence=round(observation.screen_confidence, 6),
            ready=observation.production_ready,
            capture_ms=round((capture_finished - capture_started) * 1000, 1),
            perception_ms=round(observation.perception_latency * 1000, 1),
            cycle_ms=round((analyzed - capture_started) * 1000, 1),
            observed_monotonic=observation.observed_monotonic,
        )
        return observation

    def stable(screen):
        deadline = time.monotonic() + 25
        count = 0
        last = None
        while time.monotonic() < deadline:
            last = observe()
            count = (
                count + 1
                if (
                    last.production_ready
                    and last.screen == screen
                    and last.screen_confidence >= 0.94
                )
                else 0
            )
            if count >= 2:
                return last
            time.sleep(0.25)
        raise RuntimeError(
            f"source screen {screen.value} unverified: {last.screen.value}"
        )

    def action(name, source):
        before = stable(source)
        point, viewport = click_request(name, before)
        sent = actions.execute(name, target=point, viewport=viewport)
        emit(
            event="transport",
            action=name.value,
            status=sent.status.value,
            reason=sent.reason,
        )
        verifying = lifecycle.begin(sent, before)
        if verifying.status != ActionStatus.VERIFYING:
            raise RuntimeError(f"action did not enter verification: {verifying}")
        sent_at = time.monotonic()
        first_target_at = None
        verification_capture_times = []
        while True:
            observation = observe()
            verification_capture_times.append(observation.observed_monotonic)
            if (
                observation.screen in lifecycle.pending.contract.targets
                and first_target_at is None
            ):
                first_target_at = observation.observed_monotonic
            result = lifecycle.observe(observation)
            if result is not None:
                emit(
                    event="verified",
                    action=name.value,
                    status=result.status.value,
                    screen=observation.screen.value,
                    confidence=round(observation.screen_confidence, 6),
                    reason=result.reason,
                    action_to_first_destination_s=(
                        round(first_target_at - sent_at, 3)
                        if first_target_at is not None
                        else None
                    ),
                    action_to_verified_s=round(time.monotonic() - sent_at, 3),
                    verification_capture_fps=round(
                        (len(verification_capture_times) - 1)
                        / max(
                            0.001,
                            verification_capture_times[-1]
                            - verification_capture_times[0],
                        ),
                        3,
                    )
                    if len(verification_capture_times) > 1
                    else 0.0,
                )
                if result.status != ActionStatus.SUCCEEDED:
                    raise RuntimeError(result.reason)
                return observation
            time.sleep(0.1)

    try:
        initial = observe()
        if initial.screen not in {
            DSTScreen.MAIN_MENU,
            DSTScreen.OPTIONS_DISCARD_CONFIRM,
        }:
            raise RuntimeError(f"unexpected initial screen {initial.screen.value}")
        controller = InputController(
            environment,
            lease=InputLease(),
            max_actions_per_second=2,
            max_key_presses_per_second=6,
        )
        deadman = DeadmanSafety(controller, 12)
        deadman.start()
        actions = GameActions(
            controller,
            deadman,
            InputBindings(),
            mode=WorkerMode.ACTIVE,
            action_timeout=5,
            runtime_generation=1,
            worker_generation=1,
            runtime_id=1,
            allowed_actions=frozenset(
                {
                    ActionName.CLICK_OPTIONS,
                    ActionName.CLICK_BACK,
                    ActionName.DISCARD_OPTIONS,
                }
            ),
        )
        if initial.screen == DSTScreen.OPTIONS_DISCARD_CONFIRM:
            action(ActionName.DISCARD_OPTIONS, DSTScreen.OPTIONS_DISCARD_CONFIRM)
        stable(DSTScreen.MAIN_MENU)
        opened = action(ActionName.CLICK_OPTIONS, DSTScreen.MAIN_MENU)
        if opened.screen != DSTScreen.OPTIONS:
            raise RuntimeError("Options transition absent")
        backed = action(ActionName.CLICK_BACK, DSTScreen.OPTIONS)
        if backed.screen == DSTScreen.OPTIONS_DISCARD_CONFIRM:
            backed = action(
                ActionName.DISCARD_OPTIONS, DSTScreen.OPTIONS_DISCARD_CONFIRM
            )
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
        elapsed = time.monotonic() - test_started
        cpu_after = runtime_metric("CPUUsageNSec")
        memory_after = runtime_metric("MemoryCurrent")
        emit(
            event="resource_summary",
            elapsed_s=round(elapsed, 3),
            capture_samples=len(capture_times),
            capture_fps=round(
                (len(capture_times) - 1)
                / max(0.001, capture_times[-1] - capture_times[0]),
                3,
            )
            if len(capture_times) > 1
            else 0.0,
            perception_ms_avg=round(
                sum(perception_times) / max(1, len(perception_times)) * 1000, 1
            ),
            cpu_cores_avg=round((cpu_after - cpu_before) / 1e9 / elapsed, 3),
            memory_before_mib=round(memory_before / 1048576, 1),
            memory_after_close_mib=round(memory_after / 1048576, 1),
        )


if __name__ == "__main__":
    main()
