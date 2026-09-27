from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from threading import Lock

from runtime_agent.gameworker.actions import (
    Action,
    ActionName,
    ActionResult,
    ActionStatus,
    GameActions,
    ObserveActions,
)
from runtime_agent.gameworker.activity import ActivityController
from runtime_agent.gameworker.base import WorkerContext, WorkerReport
from runtime_agent.gameworker.capture import CaptureError, Frame, X11ScreenCapture
from runtime_agent.gameworker.config import WorkerConfig, WorkerMode
from runtime_agent.gameworker.diagnostics import WorkerDiagnostics
from runtime_agent.gameworker.geometry import CalibrationProfile
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
from runtime_agent.gameworker.perception import ObservePipeline
from runtime_agent.gameworker.recording import (
    RecordingEventType,
    RecordingLimits,
    SessionRecorder,
)
from runtime_agent.gameworker.replay import (
    ReplayCaptureSource,
    ReplayRunner,
    ReplayTimingMode,
)
from runtime_agent.gameworker.state import WorkerState, WorkerStateMachine
from runtime_agent.gameworker.vision import AssetRegistry, VisionDetector

logger = logging.getLogger("runtime_agent.gameworker.dst")


class DSTGameWorker:
    plugin = "DSTGameWorker"
    version = "0.1.0"
    IDLE_OBSERVATION_INTERVAL_SECONDS = 4.0
    VERIFY_OBSERVATION_INTERVAL_SECONDS = 0.15
    VERIFY_TICK_INTERVAL_SECONDS = 0.1

    def __init__(self, config: WorkerConfig, *, worker_generation: int = 0):
        self.config = config
        self.worker_generation = max(1, worker_generation)
        self.machine = WorkerStateMachine(
            WorkerState.DISABLED
            if config.mode == WorkerMode.DISABLED
            else WorkerState.INITIALIZING
        )
        self.mode = config.mode
        self.context: WorkerContext | None = None
        self.capture = None
        self.input: InputController | None = None
        self.deadman: DeadmanSafety | None = None
        self.actions: GameActions | None = None
        self.activity = ActivityController()
        self.navigation: NavigationController | None = None
        self.recovery: RecoveryController | None = None
        self.vision: VisionDetector | None = None
        self.pipeline: ObservePipeline | None = None
        self.recorder: SessionRecorder | None = None
        self.replay: ReplayRunner | None = None
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
        self._unknown_since: float | None = None
        self._sequence = 0
        self._observation_generation = 0
        self._game_ready = False
        self._shutting_down = False
        self._lock = Lock()
        self._metrics_at = time.monotonic()
        self._pause_seconds = 0.0
        self._active_seconds = 0.0
        self._actions_count = 0
        self._recoveries = 0
        self._capture_errors = 0
        self._perception_errors = 0
        self._cleanup_failed = False
        self._recording_error: str | None = None
        self._replay_exhausted = False

    @staticmethod
    def _release_partial(
        capture,
        input_controller,
        deadman,
        actions=None,
        pipeline=None,
        recorder=None,
        recorder_timeout: float | None = None,
    ) -> bool:
        failed = False
        resources = (
            (actions, "shutdown" if hasattr(actions, "shutdown") else "cancel"),
            (pipeline, "close"),
            (recorder, "close"),
            (deadman, "close"),
            (input_controller, "close"),
            (capture, "close"),
        )
        for resource, operation in resources:
            if resource is None:
                continue
            try:
                if recorder is resource:
                    result = resource.close(timeout=recorder_timeout)
                else:
                    result = getattr(resource, operation)()
                if result is False:
                    failed = True
            except Exception:
                failed = True
                logger.exception("worker partial resource cleanup failed")
        return not failed

    def _release_resources(self) -> bool:
        failed = not self._release_partial(
            self.capture,
            self.input,
            self.deadman,
            self.actions,
            self.pipeline,
            self.recorder,
            self.config.recording_shutdown_timeout,
        )
        self.deadman = None
        self.input = None
        self.capture = None
        self.actions = None
        self.navigation = None
        self.recovery = None
        self.vision = None
        self.pipeline = None
        self.recorder = None
        self.replay = None
        return not failed

    def prepare(self, context: WorkerContext) -> WorkerReport:
        self.context = context
        if self.mode == WorkerMode.DISABLED:
            return self.status()
        if self.mode == WorkerMode.REPLAY:
            return self._prepare_replay(context)
        if not context.runtime_verified:
            self._error_code = "WORKER_DISABLED"
            if self.machine.state == WorkerState.INITIALIZING:
                self.machine.transition(
                    WorkerState.WAITING_FOR_GAME, "runtime verification required"
                )
            return self.status()
        capture = None
        input_controller = None
        deadman = None
        actions = None
        pipeline = None
        recorder = None
        try:
            capture = X11ScreenCapture(
                context.display,
                max_width=self.config.capture_max_width,
                max_height=self.config.capture_max_height,
                runtime_id=context.runtime_id,
                runtime_generation=context.runtime_generation,
                worker_generation=max(1, self.worker_generation),
                timeout=self.config.capture_timeout,
            )
            navigation = None
            recovery = None
            if self.mode == WorkerMode.ACTIVE:
                lease = InputLease()
                input_controller = InputController(
                    context.display,
                    lease=lease,
                    max_actions_per_second=self.config.max_actions_per_second,
                    max_key_presses_per_second=self.config.max_key_presses_per_second,
                    subprocess_timeout=self.config.input_subprocess_timeout,
                )
                deadman = DeadmanSafety(input_controller, self.config.deadman_timeout)
                actions = GameActions(
                    input_controller,
                    deadman,
                    self.config.bindings,
                    mode=self.mode,
                    action_timeout=self.config.action_timeout,
                    runtime_generation=context.runtime_generation,
                    worker_generation=self.worker_generation,
                    runtime_id=context.runtime_id,
                    queue_size=self.config.action_queue_size,
                    allowed_actions=frozenset({ActionName.CLICK_REWARD_OPEN}),
                )
                navigation = NavigationController(
                    actions,
                    StuckDetector(attempts=max(2, self.config.recovery_attempts)),
                    self.config.action_timeout * 4,
                )
                recovery = RecoveryController(
                    actions, max_attempts=self.config.recovery_attempts
                )
            else:
                actions = ObserveActions(
                    runtime_id=context.runtime_id,
                    runtime_generation=context.runtime_generation,
                    worker_generation=self.worker_generation,
                )
            vision = VisionDetector(
                AssetRegistry(self.config.assets_manifest),
                default_threshold=self.config.vision_threshold,
            )
            calibration = CalibrationProfile(
                self.config.profile,
                self.config.profile_version,
                self.config.capture_max_width,
                self.config.capture_max_height,
                verified=self.config.calibration_verified,
            )
            if self.config.recording_enabled:
                try:
                    recorder = SessionRecorder(
                        self.config.recording_root,
                        runtime_instance_id=(
                            f"runtime-{context.runtime_id}"
                            f"-g{context.runtime_generation}"
                        ),
                        runtime_id=context.runtime_id,
                        runtime_generation=context.runtime_generation,
                        worker_generation=self.worker_generation,
                        worker_mode=self.mode,
                        capture_source={
                            "kind": "x11-pillow",
                            "version": 1,
                            "max_width": self.config.capture_max_width,
                            "max_height": self.config.capture_max_height,
                        },
                        calibration={
                            "profile_id": calibration.profile_id,
                            "version": calibration.version,
                            "verified": calibration.verified,
                            "expected_width": calibration.expected_width,
                            "expected_height": calibration.expected_height,
                        },
                        perception={
                            "engine": type(vision).__name__,
                            "engine_version": 1,
                            "registry_version": 1,
                            "assets_configured": vision.registry.configured,
                            "assets_verified": vision.registry.production_ready,
                        },
                        limits=RecordingLimits(
                            max_duration_seconds=self.config.recording_max_duration,
                            max_frames=self.config.recording_max_frames,
                            max_bytes=self.config.recording_max_bytes,
                            queue_size=self.config.recording_queue_size,
                            frame_interval_seconds=self.config.recording_frame_interval,
                            shutdown_timeout_seconds=(
                                self.config.recording_shutdown_timeout
                            ),
                        ),
                    )
                    recorder.record_event(RecordingEventType.WORKER_STARTED)
                    if self._game_ready:
                        recorder.record_event(RecordingEventType.GAME_READY)
                    self._recording_error = None
                except Exception as exc:
                    recorder = None
                    self._recording_error = type(exc).__name__
                    logger.warning("worker recording could not start", exc_info=True)
            pipeline = ObservePipeline(
                capture,
                vision,
                self.activity,
                actions,
                calibration,
                runtime_generation=context.runtime_generation,
                worker_generation=max(1, self.worker_generation),
                max_frame_age=self.config.max_frame_age,
                max_observation_age=self.config.max_observation_age,
                perception_timeout=self.config.perception_timeout,
                planner_timeout=self.config.planner_timeout,
                recorder=recorder,
            )
            if deadman is not None:
                deadman.start()
            self.capture = capture
            self.input = input_controller
            self.deadman = deadman
            self.actions = actions
            self.navigation = navigation
            self.recovery = recovery
            self.vision = vision
            self.pipeline = pipeline
            self.recorder = recorder
            if self._game_ready:
                pipeline.on_game_ready()
            if self.machine.state in {WorkerState.INITIALIZING, WorkerState.STOPPED}:
                self.machine.transition(
                    WorkerState.WAITING_FOR_GAME, "worker dependencies prepared"
                )
            self._error_code = None
            self._sync_action_mode()
        except Exception:  # noqa: BLE001 - plugin boundary converts crashes to canonical state
            self._release_partial(
                capture,
                input_controller,
                deadman,
                actions,
                pipeline,
                recorder,
                self.config.recording_shutdown_timeout,
            )
            self._error_code = "WORKER_DISPLAY_UNAVAILABLE"
            self.machine.transition(WorkerState.ERROR, "worker preparation failed")
        return self.status()

    def _prepare_replay(self, context: WorkerContext) -> WorkerReport:
        source = None
        runner = None
        try:
            if self.config.replay_session_path is None:
                raise ValueError("REPLAY session path is not configured")
            source = ReplayCaptureSource(
                self.config.replay_session_path,
                runtime_id=context.runtime_id,
                runtime_generation=context.runtime_generation,
                worker_generation=self.worker_generation,
                timing_mode=ReplayTimingMode(self.config.replay_timing_mode),
            )
            vision = VisionDetector(
                AssetRegistry(self.config.assets_manifest),
                default_threshold=self.config.vision_threshold,
                clock=source.clock,
                wall_clock=source.clock.now_iso,
            )
            calibration = CalibrationProfile(
                self.config.profile,
                self.config.profile_version,
                self.config.capture_max_width,
                self.config.capture_max_height,
                verified=self.config.calibration_verified,
            )
            runner = ReplayRunner(
                source,
                vision,
                self.activity,
                calibration,
                max_frame_age=self.config.max_frame_age,
                max_observation_age=self.config.max_observation_age,
                perception_timeout=self.config.perception_timeout,
                planner_timeout=self.config.planner_timeout,
            )
            self.capture = source
            self.vision = vision
            self.pipeline = runner.pipeline
            self.replay = runner
            self._game_ready = True
            self._replay_exhausted = False
            self._error_code = None
            if self.machine.state in {WorkerState.INITIALIZING, WorkerState.STOPPED}:
                if self.machine.state == WorkerState.STOPPED:
                    self.machine.transition(
                        WorkerState.INITIALIZING, "REPLAY resources starting"
                    )
                self.machine.transition(
                    WorkerState.WAITING_FOR_GAME, "REPLAY session validated"
                )
            if self.machine.state == WorkerState.WAITING_FOR_GAME:
                self.machine.transition(WorkerState.OBSERVING, "REPLAY ready")
        except Exception as exc:  # noqa: BLE001 - plugin boundary
            if runner is not None:
                runner.close()
            elif source is not None:
                source.close()
            self._error_code = "WORKER_REPLAY_INVALID"
            if self.machine.state != WorkerState.ERROR:
                self.machine.transition(WorkerState.ERROR, type(exc).__name__)
        return self.status()

    def set_runtime_verified(self, verified: bool) -> None:
        if self.context is not None and verified != self.context.runtime_verified:
            self.context = WorkerContext(
                account_id=self.context.account_id,
                runtime_id=self.context.runtime_id,
                display=self.context.display,
                runtime_verified=verified,
                state=self.context.state,
                metadata=self.context.metadata,
                runtime_generation=self.context.runtime_generation,
            )
        if not verified and self.mode != WorkerMode.REPLAY:
            if self.mode == WorkerMode.DISABLED:
                if self.actions:
                    self.actions.cancel()
                elif self.input:
                    self.input.release_all()
            else:
                self.pause()
            self._error_code = "WORKER_DISABLED"
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
        self._sync_action_mode()

    def set_mode(self, mode: WorkerMode) -> WorkerReport:
        previous = self.mode
        if mode != previous and {mode, previous} == {
            WorkerMode.ACTIVE,
            WorkerMode.OBSERVE,
        }:
            if not self._release_resources():
                self._cleanup_failed = True
                self._error_code = "WORKER_SHUTDOWN_FAILED"
                self.pause()
                return self.status()
            self.mode = mode
            self.activity = ActivityController()
            if self.context:
                self.prepare(self.context)
            self._sync_action_mode()
            return self.status()
        if mode != previous and WorkerMode.REPLAY in {mode, previous}:
            if self.machine.state not in {
                WorkerState.SHUTTING_DOWN,
                WorkerState.STOPPED,
            }:
                self.shutdown()
            self._shutting_down = False
            self._cleanup_failed = False
            self.mode = mode
            if mode == WorkerMode.DISABLED:
                self.machine.transition(WorkerState.DISABLED, "worker mode disabled")
            else:
                self.machine.transition(
                    WorkerState.INITIALIZING, "worker input source changed"
                )
                if self.context:
                    self.prepare(self.context)
            return self.status()
        self.mode = mode
        if mode == WorkerMode.DISABLED:
            if previous != WorkerMode.DISABLED and not self._release_resources():
                self._cleanup_failed = True
                self._error_code = "WORKER_SHUTDOWN_FAILED"
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
            self.activity = ActivityController()
            if self.machine.state == WorkerState.DISABLED:
                self.machine.transition(WorkerState.INITIALIZING, "worker mode enabled")
            if self.context:
                self.prepare(self.context)
            if self._game_ready and self.machine.state == WorkerState.WAITING_FOR_GAME:
                self.machine.transition(WorkerState.OBSERVING, "DST is already ready")
        self._sync_action_mode()
        return self.status()

    def on_game_ready(self, context: WorkerContext) -> WorkerReport:
        self.context = context
        if self.mode == WorkerMode.REPLAY:
            return self.status()
        self._game_ready = True
        if self.recorder:
            self.recorder.record_event(RecordingEventType.GAME_READY)
        if self.mode == WorkerMode.DISABLED:
            return self.status()
        if not context.runtime_verified:
            self._error_code = "WORKER_DISABLED"
            return self.status()
        pipeline_was_prepared = self.pipeline is not None
        if self.capture is None:
            self.prepare(context)
        if self.machine.state == WorkerState.WAITING_FOR_GAME:
            self.machine.transition(WorkerState.OBSERVING, "DST readiness reported")
        if self.pipeline and pipeline_was_prepared:
            self.pipeline.on_game_ready()
        self._sync_action_mode()
        return self.status()

    def on_game_lost(self) -> WorkerReport:
        if self.mode == WorkerMode.REPLAY:
            return self.status()
        self._game_ready = False
        if self.recorder:
            self.recorder.record_event(RecordingEventType.GAME_LOST)
        if self.pipeline:
            self.pipeline.on_game_lost()
        report = self.pause()
        self._sync_action_mode()
        return report

    def execute_action(self, action: Action) -> ActionResult:
        """Canonical planner-to-executor boundary inside this worker generation."""
        if self.mode == WorkerMode.REPLAY and self.replay is not None:
            result = self.replay.actions.execute_action(action)
            self._record_action(result)
            return result
        if self.actions is None:
            return ActionResult(
                action.action_id,
                action.name,
                ActionStatus.GAME_NOT_READY,
                0.0,
                action.runtime_generation,
                action.worker_generation,
                runtime_id=action.runtime_id,
                reason="worker input resources are not prepared",
            )
        self._sync_action_mode()
        result = self.actions.execute_action(action)
        if self.recorder:
            self.recorder.record_action(action, result)
        self._record_action(result)
        return result

    def tick(self, context: WorkerContext) -> WorkerReport:
        self.context = context
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        if self._shutting_down or self.mode == WorkerMode.DISABLED:
            return self.status()
        if self.mode == WorkerMode.REPLAY:
            return self._tick_replay()
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
        try:
            if self.pipeline is None or self.actions is None:
                raise CaptureError("worker capture is not prepared")
            outcome = self.pipeline.tick()
            observation = outcome.observation
            if observation is not None:
                self._last_observation = observation.as_dict()
                self._last_observation_at = observation.timestamp
                if observation.production_ready:
                    self._unknown_since = None
                elif self.mode == WorkerMode.ACTIVE and self._unknown_since is None:
                    self._unknown_since = now
            self._would_execute = (
                outcome.proposal.action if outcome.proposal is not None else None
            )
            if outcome.action_result is not None:
                self._record_action(outcome.action_result)
            if outcome.status == "CAPTURE_FAILED":
                self._capture_errors += 1
                self._error_code = "WORKER_CAPTURE_FAILED"
                if self.recorder:
                    self.recorder.record_event(
                        RecordingEventType.CAPTURE_ERROR,
                        frame_id=outcome.frame_id,
                        payload={"status": outcome.status, "reason": outcome.reason},
                    )
            elif outcome.status in {"PERCEPTION_FAILED", "PERCEPTION_TIMEOUT"}:
                self._perception_errors += 1
                self._error_code = "WORKER_PERCEPTION_FAILED"
                if self.recorder:
                    self.recorder.record_event(
                        RecordingEventType.PERCEPTION_ERROR,
                        frame_id=outcome.frame_id,
                        payload={"status": outcome.status, "reason": outcome.reason},
                    )
            elif outcome.status in {
                "STALE_FRAME",
                "STALE_GENERATION",
                "STALE_OBSERVATION",
                "DISCARDED",
            }:
                self._error_code = "WORKER_PERCEPTION_STALE"
            elif outcome.status == "UNKNOWN":
                self._error_code = "WORKER_PERCEPTION_UNVERIFIED"
            else:
                self._error_code = None
            if self.mode == WorkerMode.ACTIVE and (
                self.activity.intervention_required
                or (self._unknown_since is not None and now - self._unknown_since > 20)
            ):
                self.activity.counters["recovery_failures"] += 1
                try:
                    if self.capture is not None:
                        self._diagnose(self.capture.capture())
                except CaptureError:
                    logger.warning("unknown-screen diagnostic capture failed")
                self.set_mode(WorkerMode.DISABLED)
                self._error_code = "WORKER_INTERVENTION_REQUIRED"
                return self.status()
            if self.machine.state not in {
                WorkerState.PAUSED,
                WorkerState.NEEDS_ATTENTION,
                WorkerState.ERROR,
            }:
                self.machine.transition(
                    WorkerState.OBSERVING
                    if outcome.action_result is not None
                    else WorkerState.WAITING,
                    f"perception {outcome.status.lower()}",
                )
            return self.status()
        except CaptureError:
            self._fail("WORKER_CAPTURE_FAILED")
        except InputError:
            self._fail("WORKER_INPUT_FAILED")
        except Exception:  # noqa: BLE001 - plugin boundary must not kill the agent
            self._fail("WORKER_CRASHED")
        return self.status()

    def _tick_replay(self) -> WorkerReport:
        if self.machine.state in {
            WorkerState.PAUSED,
            WorkerState.NEEDS_ATTENTION,
            WorkerState.ERROR,
        }:
            return self.status()
        if self.replay is None:
            self._error_code = "WORKER_REPLAY_INVALID"
            return self.status()
        try:
            outcome = self.replay.step()
            if outcome is None:
                self._replay_exhausted = True
                self._error_code = None
                if self.machine.state != WorkerState.WAITING:
                    self.machine.transition(WorkerState.WAITING, "REPLAY exhausted")
                return self.status()
            observation = outcome.observation
            if observation is not None:
                self._last_observation = observation.as_dict()
                self._last_observation_at = observation.timestamp
            self._would_execute = (
                outcome.proposal.action if outcome.proposal is not None else None
            )
            if outcome.action_result is not None:
                self._record_action(outcome.action_result)
            if outcome.status in {"CAPTURE_FAILED", "PERCEPTION_FAILED"}:
                self._error_code = (
                    "WORKER_CAPTURE_FAILED"
                    if outcome.status == "CAPTURE_FAILED"
                    else "WORKER_PERCEPTION_FAILED"
                )
            else:
                self._error_code = None
            if self.machine.state not in {WorkerState.PAUSED, WorkerState.ERROR}:
                self.machine.transition(WorkerState.OBSERVING, "REPLAY frame processed")
        except Exception:  # noqa: BLE001 - replay failures stay inside worker boundary
            self._error_code = "WORKER_REPLAY_FAILED"
            if self.machine.state != WorkerState.ERROR:
                self.machine.transition(WorkerState.ERROR, "REPLAY processing failed")
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
        if self.pipeline is not None and self.pipeline.verification_pending:
            return self.VERIFY_OBSERVATION_INTERVAL_SECONDS
        configured = min(5.0, max(2.0, self.config.observation_interval))
        return max(self.IDLE_OBSERVATION_INTERVAL_SECONDS, configured)

    @property
    def next_tick_interval(self) -> float:
        if self.pipeline is not None and self.pipeline.verification_pending:
            return min(self.config.tick_interval, self.VERIFY_TICK_INTERVAL_SECONDS)
        return self.config.tick_interval

    def _record_action(self, result) -> None:
        action, dry_run = result.action, result.dry_run
        self._last_action = (
            f"{action} {result.duration:.2f}s {result.result}"
            f"{' (dry run)' if dry_run else ''}"
        )[:80]
        self._last_action_at = time.monotonic()
        self._would_execute = action if dry_run else None
        if result.status == ActionStatus.COMPLETED:
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
            frame.image() if isinstance(frame, Frame) else frame,
            self._last_observation,
            self.machine.history(),
            self._sequence,
        )

    def pause(self) -> WorkerReport:
        if self.mode == WorkerMode.DISABLED:
            # DISABLED is already the safest state: the worker owns no capture
            # or input resources, and an autostart-off pause must not relabel it.
            self._release_resources()
            if self.machine.state not in {
                WorkerState.DISABLED,
                WorkerState.SHUTTING_DOWN,
                WorkerState.STOPPED,
            }:
                self.machine.transition(WorkerState.DISABLED, "worker remains disabled")
            self._error_code = "WORKER_DISABLED"
            self._sync_action_mode()
            return self.status()
        if self.recorder:
            self.recorder.record_event(RecordingEventType.WORKER_PAUSED)
        if self.pipeline:
            self.pipeline.invalidate()
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
        self._sync_action_mode()
        return self.status()

    def resume(self) -> WorkerReport:
        if self.mode == WorkerMode.DISABLED:
            self._error_code = "WORKER_DISABLED"
            return self.status()
        if self.mode != WorkerMode.REPLAY and (
            not self.context or not self.context.runtime_verified
        ):
            self._error_code = "WORKER_DISABLED"
            return self.status()
        if self.machine.state == WorkerState.ERROR:
            shutdown_report = self.shutdown()
            if not shutdown_report.healthy:
                return shutdown_report
            self._shutting_down = False
            self._cleanup_failed = False
            self.machine.transition(WorkerState.INITIALIZING, "explicit operator retry")
            self.prepare(self.context)
            if self._game_ready and self.machine.state == WorkerState.WAITING_FOR_GAME:
                self.machine.transition(WorkerState.OBSERVING, "DST is already ready")
            if self.machine.state == WorkerState.ERROR:
                return self.status()
        if self.machine.state in {WorkerState.PAUSED, WorkerState.NEEDS_ATTENTION}:
            self.machine.transition(WorkerState.OBSERVING, "explicit operator resume")
        if self.actions:
            self.actions.reset_cancel()
        if self.recorder:
            self.recorder.record_event(RecordingEventType.WORKER_RESUMED)
        self._sync_action_mode()
        self._error_code = None
        return self.status()

    def _effective_mode(self) -> WorkerMode:
        if self.mode != WorkerMode.ACTIVE:
            return self.mode
        if (
            self.context is None
            or not self.context.runtime_verified
            or not self._game_ready
            or self.capture is None
            or self.machine.state
            in {
                WorkerState.DISABLED,
                WorkerState.INITIALIZING,
                WorkerState.WAITING_FOR_GAME,
                WorkerState.PAUSED,
                WorkerState.NEEDS_ATTENTION,
                WorkerState.ERROR,
                WorkerState.SHUTTING_DOWN,
                WorkerState.STOPPED,
            }
        ):
            return WorkerMode.OBSERVE
        return WorkerMode.ACTIVE

    def _sync_action_mode(self) -> None:
        if self.actions:
            state = self.machine.state
            self.actions.set_safety(
                configured_mode=self.mode,
                effective_mode=self._effective_mode(),
                runtime_verified=bool(
                    self.context is not None and self.context.runtime_verified
                ),
                game_ready=self._game_ready,
                healthy=state not in {WorkerState.ERROR, WorkerState.NEEDS_ATTENTION},
                paused=state == WorkerState.PAUSED,
                stopping=self._shutting_down
                or state in {WorkerState.SHUTTING_DOWN, WorkerState.STOPPED},
            )

    def status(self) -> WorkerReport:
        state = self.machine.state
        now = time.monotonic()
        with self._lock:
            elapsed = max(0.0, now - self._metrics_at)
            self._metrics_at = now
            if state == WorkerState.PAUSED:
                self._pause_seconds += elapsed
            elif self._effective_mode() == WorkerMode.ACTIVE and state not in {
                WorkerState.DISABLED,
                WorkerState.STOPPED,
                WorkerState.SHUTTING_DOWN,
                WorkerState.NEEDS_ATTENTION,
                WorkerState.ERROR,
            }:
                self._active_seconds += elapsed
            active_seconds = self._active_seconds
            pause_seconds = self._pause_seconds
        healthy = (
            state not in {WorkerState.ERROR, WorkerState.NEEDS_ATTENTION}
            and not self._cleanup_failed
        )
        recording = (
            self.recorder.snapshot()
            if self.recorder
            else {
                "state": "DISABLED"
                if not self.config.recording_enabled
                else "UNAVAILABLE",
                "recorded_frames": 0,
                "dropped_recording_frames": 0,
                "events_written": 0,
                "write_failures": int(self._recording_error is not None),
            }
        )
        pipeline_health = self.pipeline.health() if self.pipeline else {}
        return WorkerReport(
            plugin=self.plugin,
            version=self.version,
            config_version=self.config.profile_version,
            mode=self._effective_mode(),
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
                "behavior_counters": dict(self.activity.counters),
                "recoveries": self._recoveries,
                "capture_errors": self._capture_errors,
                "perception_errors": self._perception_errors,
                "perception_latency_seconds": self._last_observation.get(
                    "perception_latency"
                ),
                "last_frame_id": self._last_observation.get("source_frame_id"),
                "observation_validity": self._last_observation.get("validity"),
                "capture_success_total": pipeline_health.get(
                    "capture_success_total", 0
                ),
                "capture_failure_total": pipeline_health.get(
                    "capture_failure_total", 0
                ),
                "frames_stale_total": pipeline_health.get("frames_stale_total", 0),
                "perception_success_total": pipeline_health.get(
                    "perception_success_total", 0
                ),
                "perception_failure_total": pipeline_health.get(
                    "perception_failure_total", 0
                ),
                "observe_suppressed_actions_total": pipeline_health.get(
                    "observe_suppressed_actions_total", 0
                ),
                "recording_frames_written": recording.get("recorded_frames", 0),
                "recording_frames_dropped": recording.get(
                    "dropped_recording_frames", 0
                ),
                "replay_frames_processed": (
                    self.replay.frames_processed if self.replay else 0
                ),
                "perception_health": pipeline_health or None,
            },
            details={
                "profile": self.config.profile,
                "requested_mode": self.mode,
                "observation": self._last_observation,
                "behavior_state": self.activity.state.value,
                "last_behavior_decision": self.activity.decisions[-1]
                if self.activity.decisions
                else None,
                "permitted_active_actions": [ActionName.CLICK_REWARD_OPEN.value],
                "transitions": self.machine.history()[-10:],
                "diagnostics": {
                    "worker_mode": self._effective_mode(),
                    "worker_state": state,
                    "runtime_generation": (
                        self.context.runtime_generation if self.context else None
                    ),
                    "worker_generation": self.worker_generation,
                    "capture_health": pipeline_health,
                    "last_frame_id": self._last_observation.get("source_frame_id"),
                    "observation_validity": self._last_observation.get("validity"),
                    "observation_confidence": self._last_observation.get(
                        "worker_confidence"
                    ),
                    "last_proposed_action": self._would_execute,
                    "last_action_result": self._last_action,
                    "input_safety": {
                        "driver_present": self.input is not None,
                        "held_inputs": self.input.has_held_inputs
                        if self.input
                        else False,
                        "deadman_tripped": self.deadman.tripped
                        if self.deadman
                        else False,
                        "replay_input_forbidden": self.mode == WorkerMode.REPLAY,
                    },
                    "recording": recording,
                    "replay": {
                        "active": self.replay is not None,
                        "exhausted": self._replay_exhausted,
                        "frames_processed": self.replay.frames_processed
                        if self.replay
                        else 0,
                        "lifecycle_events": self.replay.lifecycle_events[-10:]
                        if self.replay
                        else [],
                    },
                },
            },
        )

    def shutdown(self) -> WorkerReport:
        self._shutting_down = True
        if self.recorder:
            self.recorder.record_event(RecordingEventType.SHUTDOWN)
        if self.machine.state not in {WorkerState.SHUTTING_DOWN, WorkerState.STOPPED}:
            self.machine.transition(WorkerState.SHUTTING_DOWN, "worker shutdown")
        self._cleanup_failed = not self._release_resources()
        if self._cleanup_failed:
            self._error_code = "WORKER_CLEANUP_FAILED"
        if self.machine.state != WorkerState.STOPPED:
            reason = (
                "worker cleanup completed with errors"
                if self._cleanup_failed
                else "worker resources released"
            )
            self.machine.transition(WorkerState.STOPPED, reason)
        return self.status()
