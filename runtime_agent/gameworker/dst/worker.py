from __future__ import annotations

import time
from datetime import datetime, timezone
from threading import Lock

from runtime_agent.gameworker.actions import ActionName, GameActions
from runtime_agent.gameworker.activity import ActivityController
from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.capture import CaptureError, X11ScreenCapture
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.diagnostics import WorkerDiagnostics
from runtime_agent.gameworker.input import (
    DeadmanSafety,
    InputController,
    InputError,
    InputLease,
)
from runtime_agent.gameworker.navigation import (
    NavigationController,
    RecoveryController,
    StuckDetector,
)
from runtime_agent.gameworker.state import WorkerState, WorkerStateMachine
from runtime_agent.gameworker.vision import (
    AssetRegistry,
    FrameChangeMonitor,
    VisionDetector,
)


class DSTGameWorker:
    plugin = "DSTGameWorker"
    version = "0.1.0"

    def __init__(self, config: WorkerConfig):
        self.config = config
        self.machine = WorkerStateMachine(
            WorkerState.DISABLED
            if config.mode == WorkerMode.DISABLED
            else WorkerState.INITIALIZING
        )
        self.mode = config.mode
        self.context: WorkerContext | None = None
        self.capture: X11ScreenCapture | None = None
        self.input: InputController | None = None
        self.deadman: DeadmanSafety | None = None
        self.actions: GameActions | None = None
        self.activity = ActivityController()
        self.navigation: NavigationController | None = None
        self.recovery: RecoveryController | None = None
        self.vision: VisionDetector | None = None
        self.monitor = FrameChangeMonitor()
        self.diagnostics = WorkerDiagnostics(
            config.diagnostic_directory,
            enabled=config.diagnostic_capture,
            ring_size=config.diagnostic_ring_size,
            max_bytes=config.diagnostic_max_bytes,
        )
        self._last_tick_at: str | None = None
        self._last_observation_at: str | None = None
        self._last_action: str | None = None
        self._last_action_at: float | None = None
        self._would_execute: str | None = None
        self._error_code: str | None = None
        self._last_observation: dict = {}
        self._last_capture_at = 0.0
        self._unknown_count = 0
        self._sequence = 0
        self._game_ready = False
        self._shutting_down = False
        self._lock = Lock()
        self._metrics_at = time.monotonic()
        self._pause_seconds = 0.0
        self._active_seconds = 0.0
        self._actions_count = 0
        self._recoveries = 0

    def prepare(self, context: WorkerContext) -> WorkerReport:
        self.context = context
        if self.mode == WorkerMode.DISABLED:
            return self.status()
        if not context.runtime_verified:
            self._error_code = "WORKER_DISABLED"
            if self.machine.state == WorkerState.INITIALIZING:
                self.machine.transition(
                    WorkerState.WAITING_FOR_GAME, "runtime verification required"
                )
            return self.status()
        try:
            self.capture = X11ScreenCapture(
                context.display,
                max_width=self.config.capture_max_width,
                max_height=self.config.capture_max_height,
            )
            lease = InputLease()
            self.input = InputController(
                context.display,
                lease=lease,
                max_actions_per_second=self.config.max_actions_per_second,
                max_key_presses_per_second=self.config.max_key_presses_per_second,
            )
            self.deadman = DeadmanSafety(self.input, self.config.deadman_timeout)
            self.deadman.start()
            self.actions = GameActions(
                self.input,
                self.deadman,
                self.config.bindings,
                mode=self.mode,
                action_timeout=self.config.action_timeout,
            )
            self.navigation = NavigationController(
                self.actions,
                StuckDetector(attempts=max(2, self.config.recovery_attempts)),
                self.config.action_timeout * 4,
            )
            self.recovery = RecoveryController(
                self.actions, max_attempts=self.config.recovery_attempts
            )
            self.vision = VisionDetector(
                AssetRegistry(self.config.assets_manifest),
                default_threshold=self.config.vision_threshold,
            )
            if self.machine.state in {WorkerState.INITIALIZING, WorkerState.STOPPED}:
                self.machine.transition(
                    WorkerState.WAITING_FOR_GAME, "worker dependencies prepared"
                )
            self._error_code = None
        except Exception:  # noqa: BLE001 - plugin boundary converts crashes to canonical state
            self._error_code = "WORKER_DISPLAY_UNAVAILABLE"
            self.machine.transition(WorkerState.ERROR, "worker preparation failed")
        return self.status()

    def set_runtime_verified(self, verified: bool) -> None:
        if self.context is not None and verified != self.context.runtime_verified:
            self.context = WorkerContext(
                account_id=self.context.account_id,
                runtime_id=self.context.runtime_id,
                display=self.context.display,
                runtime_verified=verified,
                metadata=self.context.metadata,
            )
        if (
            verified
            and self.mode != WorkerMode.DISABLED
            and self.capture is None
            and self.context
        ):
            self.prepare(self.context)
            if self._game_ready and self.machine.state == WorkerState.WAITING_FOR_GAME:
                self.machine.transition(
                    WorkerState.OBSERVING, "runtime verification confirmed"
                )

    def set_mode(self, mode: WorkerMode) -> WorkerReport:
        previous = self.mode
        self.mode = mode
        if self.actions:
            self.actions.set_mode(mode)
        if mode == WorkerMode.DISABLED:
            if self.input:
                self.input.release_all()
            if self.machine.state not in {
                WorkerState.DISABLED,
                WorkerState.SHUTTING_DOWN,
                WorkerState.STOPPED,
            }:
                if self.machine.state == WorkerState.PAUSED:
                    self.machine.transition(
                        WorkerState.DISABLED, "worker mode disabled"
                    )
                else:
                    self.machine.transition(WorkerState.PAUSED, "worker mode disabled")
                    self.machine.transition(
                        WorkerState.DISABLED, "worker mode disabled"
                    )
        elif previous == WorkerMode.DISABLED and self.machine.state in {
            WorkerState.DISABLED,
            WorkerState.PAUSED,
        }:
            if self.machine.state == WorkerState.DISABLED:
                self.machine.transition(WorkerState.INITIALIZING, "worker mode enabled")
            if self.context:
                self.prepare(self.context)
            if self._game_ready and self.machine.state == WorkerState.WAITING_FOR_GAME:
                self.machine.transition(WorkerState.OBSERVING, "DST is already ready")
        return self.status()

    def on_game_ready(self, context: WorkerContext) -> WorkerReport:
        self.context = context
        self._game_ready = True
        if self.mode == WorkerMode.DISABLED:
            return self.status()
        if not context.runtime_verified:
            self._error_code = "WORKER_DISABLED"
            return self.status()
        if self.capture is None:
            self.prepare(context)
        if self.machine.state == WorkerState.WAITING_FOR_GAME:
            self.machine.transition(WorkerState.OBSERVING, "DST readiness reported")
        return self.status()

    def tick(self, context: WorkerContext) -> WorkerReport:
        self.context = context
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        if self._shutting_down or self.mode == WorkerMode.DISABLED:
            return self.status()
        if not context.runtime_verified or not self._game_ready:
            if self.input:
                self.input.release_all()
            self._error_code = "WORKER_DISABLED"
            return self.status()
        if self.machine.state in {
            WorkerState.PAUSED,
            WorkerState.NEEDS_ATTENTION,
            WorkerState.ERROR,
        }:
            return self.status()
        if self.deadman and self.deadman.tripped:
            self._error_code = "WORKER_DEADMAN_TIMEOUT"
            self.machine.transition(
                WorkerState.NEEDS_ATTENTION, "input deadman released held inputs"
            )
            return self.status()
        now = time.monotonic()
        interval = self._observation_interval()
        if now - self._last_capture_at < interval:
            return self.status()
        self._last_capture_at = now
        frame = None
        try:
            if self.capture is None or self.vision is None or self.actions is None:
                raise CaptureError("worker capture is not prepared")
            frame = self.capture.capture()
            digest, change, frozen = self.monitor.update(frame, now)
            observation = self.vision.observe(
                frame, screen_hash=digest, screen_change=change, frozen=frozen
            )
            self._last_observation = observation.as_dict()
            self._last_observation_at = observation.timestamp
            recently_acted = (
                self._last_action_at is not None and now - self._last_action_at < 60
            )
            if (
                "SCREEN_FROZEN" in observation.diagnostic_flags
                and self.mode == WorkerMode.ACTIVE
                and recently_acted
            ):
                self.actions.cancel()
                self._error_code = "WORKER_UNKNOWN_SCREEN"
                self.machine.transition(
                    WorkerState.NEEDS_ATTENTION, "screen change monitor timed out"
                )
                self._diagnose(frame)
                return self.status()
            if "UNKNOWN" in observation.diagnostic_flags:
                return self._handle_unknown(
                    frame, unconfigured="UNCONFIGURED" in observation.diagnostic_flags
                )
            self._unknown_count = 0
            if (
                self.recovery
                and observation.screen_change is not None
                and observation.screen_change >= 0.01
            ):
                self.recovery.reset()
            action = self.activity.next_action(observation)
            self._would_execute = action if action != ActionName.NONE else None
            if action == ActionName.NONE:
                self.machine.transition(WorkerState.WAITING, "no safe action selected")
                return self.status()
            target = (
                WorkerState.INTERACTING
                if action in {ActionName.INTERACT, ActionName.CANCEL}
                else WorkerState.IDLE_ACTIVITY
            )
            self.machine.transition(target, f"selected {action}")
            if (
                action
                in {
                    ActionName.MOVE_FORWARD,
                    ActionName.MOVE_BACKWARD,
                    ActionName.TURN_LEFT,
                    ActionName.TURN_RIGHT,
                }
                and self.navigation
            ):
                navigation_status, result = self.navigation.step(observation, action)
                if navigation_status in {"STUCK", "TIMEOUT"}:
                    self._error_code = "WORKER_STUCK"
                    self.machine.transition(
                        WorkerState.RECOVERING,
                        f"navigation {navigation_status.lower()}",
                    )
                    self._recoveries += 1
                    recovery_status, result = (
                        self.recovery.recover()
                        if self.recovery
                        else ("EXHAUSTED", None)
                    )
                    if recovery_status == "EXHAUSTED":
                        self._error_code = "WORKER_RECOVERY_EXHAUSTED"
                        self.machine.transition(
                            WorkerState.NEEDS_ATTENTION, "stuck recovery exhausted"
                        )
                        self._diagnose(frame)
                        return self.status()
            else:
                result = self.actions.execute(action)
            if result is None:
                return self.status()
            self._record_action(result)
            self.machine.transition(WorkerState.OBSERVING, "bounded action complete")
            return self.status()
        except CaptureError:
            self._fail("WORKER_CAPTURE_FAILED", frame)
        except InputError:
            self._fail("WORKER_INPUT_FAILED", frame)
        except Exception:  # noqa: BLE001 - plugin boundary must not kill the agent
            self._fail("WORKER_CRASHED", frame)
        return self.status()

    def _handle_unknown(self, frame, *, unconfigured: bool) -> WorkerReport:
        assert self.actions is not None
        self.actions.release_all()
        self._unknown_count += 1
        self._error_code = (
            "WORKER_ASSET_MISSING" if unconfigured else "WORKER_VISION_LOW_CONFIDENCE"
        )
        if unconfigured:
            self.machine.transition(
                WorkerState.NEEDS_ATTENTION, "vision assets are unconfigured"
            )
            self._diagnose(frame)
            return self.status()
        if self._unknown_count <= self.config.recovery_attempts:
            self.machine.transition(
                WorkerState.RECOVERING, "unknown screen; bounded observation retry"
            )
            self._recoveries += 1
            # Unknown-screen recovery never sends input. It only re-observes.
            self.machine.transition(
                WorkerState.OBSERVING, "recovery observation scheduled"
            )
        else:
            self._error_code = "WORKER_RECOVERY_EXHAUSTED"
            self.machine.transition(
                WorkerState.NEEDS_ATTENTION, "unknown-screen recovery exhausted"
            )
            self._diagnose(frame)
        return self.status()

    def _observation_interval(self) -> float:
        state = self.machine.state
        if state == WorkerState.PAUSED:
            return max(10.0, self.config.observation_interval * 5)
        if state in {WorkerState.NAVIGATING, WorkerState.RECOVERING}:
            return max(0.2, self.config.observation_interval / 2)
        return self.config.observation_interval

    def _record_action(self, result) -> None:
        action, dry_run = result.action, result.dry_run
        self._last_action = (
            f"{action} {result.duration:.2f}s {result.result}"
            f"{' (dry run)' if dry_run else ''}"
        )[:80]
        self._last_action_at = time.monotonic()
        self._would_execute = action if dry_run else None
        if not dry_run:
            self._actions_count += 1

    def _fail(self, code: str, frame=None) -> None:
        if self.actions:
            self.actions.cancel()
        self._error_code = code
        if self.machine.state not in {
            WorkerState.ERROR,
            WorkerState.SHUTTING_DOWN,
            WorkerState.STOPPED,
        }:
            self.machine.transition(WorkerState.ERROR, code)
        if frame is not None:
            self._diagnose(frame)

    def _diagnose(self, frame) -> None:
        self._sequence += 1
        self.diagnostics.capture_error(
            frame, self._last_observation, self.machine.history(), self._sequence
        )

    def pause(self) -> WorkerReport:
        if self.actions:
            self.actions.cancel()
        elif self.input:
            self.input.release_all()
        if self.machine.state not in {
            WorkerState.PAUSED,
            WorkerState.SHUTTING_DOWN,
            WorkerState.STOPPED,
        }:
            self.machine.transition(
                WorkerState.PAUSED, "manual pause/input ownership revoked"
            )
        self._error_code = "WORKER_PAUSED"
        self._last_action = "PAUSE"
        return self.status()

    def resume(self) -> WorkerReport:
        if self.mode == WorkerMode.DISABLED:
            self._error_code = "WORKER_DISABLED"
            return self.status()
        if not self.context or not self.context.runtime_verified:
            self._error_code = "WORKER_DISABLED"
            return self.status()
        if self.machine.state in {WorkerState.PAUSED, WorkerState.NEEDS_ATTENTION}:
            self.machine.transition(WorkerState.OBSERVING, "explicit operator resume")
        if self.actions:
            self.actions.reset_cancel()
        self._error_code = None
        return self.status()

    def status(self) -> WorkerReport:
        state = self.machine.state
        now = time.monotonic()
        with self._lock:
            elapsed = max(0.0, now - self._metrics_at)
            self._metrics_at = now
            if state == WorkerState.PAUSED:
                self._pause_seconds += elapsed
            elif self.mode == WorkerMode.ACTIVE and state not in {
                WorkerState.DISABLED,
                WorkerState.STOPPED,
                WorkerState.SHUTTING_DOWN,
                WorkerState.NEEDS_ATTENTION,
                WorkerState.ERROR,
            }:
                self._active_seconds += elapsed
            active_seconds = self._active_seconds
            pause_seconds = self._pause_seconds
        healthy = state not in {WorkerState.ERROR, WorkerState.NEEDS_ATTENTION}
        return WorkerReport(
            plugin=self.plugin,
            version=self.version,
            config_version=self.config.profile_version,
            mode=self.mode,
            state=state,
            healthy=healthy,
            last_tick_at=self._last_tick_at,
            last_action=self._last_action,
            last_observation_at=self._last_observation_at,
            error_code=self._error_code,
            would_execute=self._would_execute,
            telemetry={
                "worker_active_seconds": round(active_seconds, 3),
                "pause_seconds": round(pause_seconds, 3),
                "actions_count": self._actions_count,
                "recoveries": self._recoveries,
            },
            details={
                "profile": self.config.profile,
                "observation": self._last_observation,
                "transitions": self.machine.history()[-10:],
            },
        )

    def shutdown(self) -> WorkerReport:
        self._shutting_down = True
        if self.machine.state not in {WorkerState.SHUTTING_DOWN, WorkerState.STOPPED}:
            self.machine.transition(WorkerState.SHUTTING_DOWN, "worker shutdown")
        if self.actions:
            self.actions.cancel()
        if self.deadman:
            self.deadman.close()
        elif self.input:
            self.input.release_all()
        if self.capture:
            self.capture.close()
        if self.machine.state != WorkerState.STOPPED:
            self.machine.transition(WorkerState.STOPPED, "worker resources released")
        return self.status()
