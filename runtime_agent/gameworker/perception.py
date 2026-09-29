from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from runtime_agent.gameworker.actions import (
    ActionName,
    ActionResult,
    ActionStatus,
)
from runtime_agent.gameworker.activity import ActionProposal, Planner
from runtime_agent.gameworker.capture import (
    CaptureError,
    CaptureSource,
    LatestFrameSlot,
)
from runtime_agent.gameworker.config import WorkerMode
from runtime_agent.gameworker.geometry import CalibrationProfile
from runtime_agent.gameworker.transitions import (
    CONTRACTS,
    ActionLifecycle,
    action_precondition_error,
    click_request,
)
from runtime_agent.gameworker.vision import (
    GameObservation,
    ObservationStore,
    PerceptionEngine,
)

logger = logging.getLogger("runtime_agent.gameworker.perception")


class ActionSink(Protocol):
    def execute(
        self,
        action: ActionName,
        *,
        duration: float | None = None,
        target=None,
        viewport=None,
        valid_until: float | None = None,
        evidence_sequence: int | None = None,
    ) -> ActionResult: ...


class RecordingSink(Protocol):
    def record_frame(self, frame) -> bool: ...
    def record_observation(self, observation: GameObservation) -> bool: ...
    def record_proposal(
        self, proposal: ActionProposal, *, frame_id: str, observation_id: str
    ) -> bool: ...
    def record_action_result(
        self,
        result: ActionResult,
        *,
        frame_id: str | None = None,
        observation_id: str | None = None,
        proposal: ActionProposal | None = None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class PipelineOutcome:
    status: str
    frame_id: str | None = None
    observation: GameObservation | None = None
    proposal: ActionProposal | None = None
    action_result: ActionResult | None = None
    reason: str | None = None


class ObservePipeline:
    """Strict single-flight, capacity-one perception/planner coordinator."""

    def __init__(
        self,
        capture: CaptureSource,
        engine: PerceptionEngine,
        planner: Planner,
        actions: ActionSink,
        calibration: CalibrationProfile,
        *,
        runtime_generation: int,
        worker_generation: int,
        max_frame_age: float,
        max_observation_age: float,
        perception_timeout: float,
        planner_timeout: float,
        clock=time.monotonic,
        recorder: RecordingSink | None = None,
    ):
        if runtime_generation < 1 or worker_generation < 1:
            raise ValueError("pipeline generations must be positive")
        if (
            min(
                max_frame_age,
                max_observation_age,
                perception_timeout,
                planner_timeout,
            )
            <= 0
        ):
            raise ValueError("pipeline time bounds must be positive")
        self.capture = capture
        self.engine = engine
        self.planner = planner
        self.actions = actions
        self.calibration = calibration
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self.max_frame_age = max_frame_age
        self.max_observation_age = max_observation_age
        self.perception_timeout = perception_timeout
        self.planner_timeout = planner_timeout
        self._clock = clock
        self.recorder = recorder
        self.action_lifecycle = ActionLifecycle(clock=clock)
        self._slot = LatestFrameSlot()
        self._store = ObservationStore(runtime_generation, worker_generation)
        self._lock = Lock()
        self._tick_lock = Lock()
        self._closed = False
        self._game_ready = False
        self._session = 0
        self._ready_after = float("inf")
        self._observation_generation = 0
        self._last_capture_success: float | None = None
        self._last_frame_age: float | None = None
        self._capture_errors = 0
        self._last_valid_observation: float | None = None
        self._last_perception_latency: float | None = None
        self._last_capture_latency: float | None = None
        self._last_planner_latency: float | None = None
        self._last_cycle_latency: float | None = None
        self._capture_success_total = 0
        self._frames_stale_total = 0
        self._perception_success_total = 0
        self._perception_failure_total = 0
        self._observe_suppressed_actions_total = 0

    @property
    def latest_observation(self) -> GameObservation | None:
        return self._store.current()

    @property
    def verification_pending(self) -> bool:
        return self.action_lifecycle.pending is not None

    def health(self) -> dict[str, float | int | None]:
        with self._lock:
            return {
                "last_capture_success_monotonic": self._last_capture_success,
                "last_frame_age_seconds": self._last_frame_age,
                "capture_errors": self._capture_errors,
                "last_valid_observation_monotonic": self._last_valid_observation,
                "perception_latency_seconds": self._last_perception_latency,
                "capture_latency_seconds": self._last_capture_latency,
                "planner_latency_seconds": self._last_planner_latency,
                "observation_cycle_latency_seconds": self._last_cycle_latency,
                "capture_success_total": self._capture_success_total,
                "capture_failure_total": self._capture_errors,
                "frames_stale_total": self._frames_stale_total,
                "perception_success_total": self._perception_success_total,
                "perception_failure_total": self._perception_failure_total,
                "observe_suppressed_actions_total": self._observe_suppressed_actions_total,
            }

    def on_game_ready(self) -> None:
        with self._lock:
            self._session += 1
            self._game_ready = True
            self._ready_after = self._clock()
            self._store.invalidate()

    def on_game_lost(self) -> None:
        self.action_lifecycle.abort(reason="game readiness lost")
        with self._lock:
            self._session += 1
            self._game_ready = False
            self._ready_after = float("inf")
            self._store.invalidate()

    def invalidate(self) -> None:
        """Discard prior perception while retaining current game-ready knowledge."""
        self.action_lifecycle.abort(reason="perception invalidated")
        with self._lock:
            self._session += 1
            self._ready_after = self._clock() if self._game_ready else float("inf")
            self._store.invalidate()

    def tick(self) -> PipelineOutcome:
        if not self._tick_lock.acquire(blocking=False):
            return PipelineOutcome("BUSY", reason="single-flight tick already running")
        try:
            return self._run_tick()
        finally:
            self._tick_lock.release()

    def _run_tick(self) -> PipelineOutcome:
        cycle_started = self._clock()
        with self._lock:
            if self._closed:
                return PipelineOutcome("CLOSED", reason="pipeline closed")
            session = self._session
            game_ready = self._game_ready
            ready_after = self._ready_after
        capture_started = self._clock()
        expired = self.action_lifecycle.poll()
        if expired is not None:
            failure = getattr(self.planner, "on_action_failure", None)
            if failure is not None:
                failure(expired)
            if self.recorder is not None:
                observation = self.latest_observation
                self.recorder.record_action_result(
                    expired,
                    frame_id=observation.source_frame_id if observation else None,
                    observation_id=(
                        f"observation-{observation.observation_generation}"
                        if observation
                        else None
                    ),
                )
            return PipelineOutcome(
                "ACTION_FAILED", action_result=expired, reason=expired.reason
            )
        try:
            captured = self.capture.capture()
        except CaptureError as exc:
            with self._lock:
                self._capture_errors += 1
            logger.warning(
                "capture failed runtime_generation=%s worker_generation=%s failure=%s",
                self.runtime_generation,
                self.worker_generation,
                exc.failure,
            )
            return PipelineOutcome("CAPTURE_FAILED", reason=f"{exc.failure}: {exc}")
        except Exception as exc:  # noqa: BLE001 - capture plugin boundary
            with self._lock:
                self._capture_errors += 1
            logger.warning(
                "capture failed runtime_generation=%s worker_generation=%s error=%s",
                self.runtime_generation,
                self.worker_generation,
                type(exc).__name__,
            )
            return PipelineOutcome("CAPTURE_FAILED", reason=type(exc).__name__)
        capture_finished = self._clock()
        with self._lock:
            self._last_capture_latency = max(0.0, capture_finished - capture_started)
            self._capture_success_total += 1
        if self.recorder is not None:
            try:
                self.recorder.record_frame(captured)
            except Exception:
                logger.warning("recording rejected a captured frame", exc_info=True)
        self._slot.publish(captured)
        frame = self._slot.take()
        assert frame is not None
        frame_age = frame.age(self._clock())
        with self._lock:
            self._last_capture_success = self._clock()
            self._last_frame_age = frame_age
        with self._lock:
            if self._closed or session != self._session:
                return PipelineOutcome(
                    "DISCARDED", frame.frame_id, reason="session changed"
                )
        if (
            frame.runtime_generation != self.runtime_generation
            or frame.worker_generation != self.worker_generation
        ):
            return PipelineOutcome("STALE_GENERATION", frame.frame_id)
        if not frame.is_fresh(self.max_frame_age, self._clock()):
            with self._lock:
                self._frames_stale_total += 1
            return PipelineOutcome("STALE_FRAME", frame.frame_id)
        if not game_ready:
            return PipelineOutcome("GAME_NOT_READY", frame.frame_id)
        if frame.captured_monotonic <= ready_after:
            return PipelineOutcome("AWAITING_FRESH_FRAME", frame.frame_id)
        self._observation_generation += 1
        deadline = self._clock() + self.perception_timeout
        try:
            observation = self.engine.analyze(
                frame,
                calibration=self.calibration,
                observation_generation=self._observation_generation,
                max_frame_age=self.max_frame_age,
                deadline=deadline,
            )
        except Exception as exc:  # noqa: BLE001 - detector failure isolation
            with self._lock:
                self._perception_failure_total += 1
            return PipelineOutcome(
                "PERCEPTION_FAILED", frame.frame_id, reason=type(exc).__name__
            )
        with self._lock:
            if self._closed or session != self._session:
                return PipelineOutcome(
                    "DISCARDED", frame.frame_id, reason="session changed"
                )
        if self._clock() > deadline:
            with self._lock:
                self._perception_failure_total += 1
            return PipelineOutcome(
                "PERCEPTION_TIMEOUT", frame.frame_id, observation=observation
            )
        if (
            observation.source_frame_id != frame.frame_id
            or observation.source_sequence != frame.sequence
            or observation.source_captured_monotonic != frame.captured_monotonic
            or observation.runtime_id != frame.runtime_id
            or observation.runtime_generation != frame.runtime_generation
            or observation.worker_generation != frame.worker_generation
        ):
            return PipelineOutcome(
                "INVALID_OBSERVATION",
                frame.frame_id,
                observation=observation,
                reason="observation provenance does not match source frame",
            )
        with self._lock:
            self._last_perception_latency = observation.perception_latency
            self._perception_success_total += 1
            if observation.valid:
                self._last_valid_observation = observation.observed_monotonic
        observation_id = f"observation-{observation.observation_generation}"
        if self.recorder is not None:
            try:
                self.recorder.record_observation(observation)
            except Exception:
                logger.warning("recording rejected an observation", exc_info=True)
        logger.info(
            "perception result runtime_id=%s runtime_generation=%s "
            "worker_generation=%s frame_sequence=%s frame_age=%.3f "
            "validity=%s latency=%.3f",
            frame.runtime_id,
            frame.runtime_generation,
            frame.worker_generation,
            frame.sequence,
            frame_age,
            observation.validity,
            observation.perception_latency,
        )
        if not self._store.publish(observation):
            return PipelineOutcome(
                "DISCARDED",
                frame.frame_id,
                observation=observation,
                reason="late observation",
            )
        now = self._clock()
        with self._lock:
            self._last_cycle_latency = max(0.0, now - cycle_started)
        if (
            now > observation.fresh_until
            or observation.observed_monotonic + self.max_observation_age < now
        ):
            return PipelineOutcome("STALE_OBSERVATION", frame.frame_id, observation)
        if self.action_lifecycle.pending is not None:
            verified_result = self.action_lifecycle.observe(observation)
            result = verified_result or self.action_lifecycle.current()
            assert result is not None
            if verified_result is not None:
                callback = getattr(self.planner, "on_verified", None)
                if callback is not None:
                    callback(observation, verified_result)
            if self.recorder is not None:
                self.recorder.record_action_result(
                    result,
                    frame_id=frame.frame_id,
                    observation_id=observation_id,
                )
            return PipelineOutcome(
                "ACTION_VERIFIED"
                if result.status == ActionStatus.SUCCEEDED
                else "ACTION_FAILED"
                if result.terminal
                else "ACTION_VERIFYING",
                frame.frame_id,
                observation,
                action_result=result,
            )
        if not observation.production_ready:
            on_unknown = getattr(self.planner, "on_unknown", None)
            if on_unknown is not None:
                on_unknown(observation)
            return PipelineOutcome("UNKNOWN", frame.frame_id, observation)
        planner_started = self._clock()
        planner_deadline = planner_started + self.planner_timeout
        try:
            proposal = self.planner.propose(observation)
        except Exception as exc:  # noqa: BLE001 - planner failure isolation
            return PipelineOutcome(
                "PLANNER_FAILED", frame.frame_id, observation, reason=type(exc).__name__
            )
        with self._lock:
            if self._closed or session != self._session:
                return PipelineOutcome(
                    "DISCARDED", frame.frame_id, observation, reason="session changed"
                )
        if self._clock() > planner_deadline:
            return PipelineOutcome("PLANNER_TIMEOUT", frame.frame_id, observation)
        planner_finished = self._clock()
        with self._lock:
            self._last_planner_latency = max(0.0, planner_finished - planner_started)
            self._last_cycle_latency = max(0.0, planner_finished - cycle_started)
        if proposal is None:
            return PipelineOutcome("NO_ACTION", frame.frame_id, observation)
        if self.recorder is not None:
            try:
                self.recorder.record_proposal(
                    proposal,
                    frame_id=frame.frame_id,
                    observation_id=observation_id,
                )
            except Exception:
                logger.warning("recording rejected a planner proposal", exc_info=True)
        if proposal.action in CONTRACTS:
            contract = CONTRACTS[proposal.action]
            target = viewport = None
            precondition_error = (
                None
                if getattr(self.actions, "input_free", False)
                or getattr(self.actions, "mode", None) == WorkerMode.OBSERVE
                else action_precondition_error(proposal.action, observation)
            )
            if precondition_error is not None:
                result = ActionResult(
                    f"precondition-{observation.observation_generation}",
                    proposal.action,
                    ActionStatus.SAFETY_BLOCKED,
                    0.0,
                    self.runtime_generation,
                    self.worker_generation,
                    observation.runtime_id,
                    precondition_error,
                )
                on_result = getattr(self.planner, "on_action_result", None)
                if on_result is not None:
                    result = on_result(observation, result)
                return PipelineOutcome(
                    "ACTION_FAILED",
                    frame.frame_id,
                    observation,
                    proposal,
                    result,
                    reason=precondition_error,
                )
            if contract.anchors:
                try:
                    target, viewport = click_request(proposal.action, observation)
                except ValueError as exc:
                    result = ActionResult(
                        f"anchor-missing-{observation.observation_generation}",
                        proposal.action,
                        ActionStatus.REJECTED,
                        0.0,
                        self.runtime_generation,
                        self.worker_generation,
                        observation.runtime_id,
                        str(exc),
                    )
                    on_result = getattr(self.planner, "on_action_result", None)
                    if on_result is not None:
                        result = on_result(observation, result)
                    return PipelineOutcome(
                        "ACTION_FAILED",
                        frame.frame_id,
                        observation,
                        proposal,
                        result,
                        reason=str(exc),
                    )
            if proposal.action == ActionName.HOVER_GIFT_ICON:
                arm = getattr(self.engine, "arm_gift_hover", None)
                if arm is not None:
                    arm(frame, observation)
            result = self.actions.execute(
                proposal.action,
                duration=proposal.duration,
                target=target,
                viewport=viewport,
                **(
                    {
                        "valid_until": observation.fresh_until,
                        "evidence_sequence": observation.source_sequence,
                    }
                    if proposal.action == ActionName.CLICK_GIFT_ICON
                    else {}
                ),
            )
        else:
            result = self.actions.execute(proposal.action, duration=proposal.duration)
        if result.status == ActionStatus.SENT:
            result = self.action_lifecycle.begin(result, observation)
        on_result = getattr(self.planner, "on_action_result", None)
        if on_result is not None:
            result = on_result(observation, result)
        if result.status.value == "SUPPRESSED":
            with self._lock:
                self._observe_suppressed_actions_total += 1
        if self.recorder is not None:
            try:
                self.recorder.record_action_result(
                    result,
                    frame_id=frame.frame_id,
                    observation_id=observation_id,
                    proposal=proposal,
                )
            except Exception:
                logger.warning("recording rejected an action result", exc_info=True)
        logger.info(
            "perception proposal frame=%s runtime_generation=%s worker_generation=%s action=%s result=%s",
            frame.frame_id,
            frame.runtime_generation,
            frame.worker_generation,
            proposal.action,
            result.status,
        )
        return PipelineOutcome(
            "ACTION_VERIFYING"
            if result.status.value == "VERIFYING"
            else "ACTION_RESULT",
            frame.frame_id,
            observation,
            proposal,
            result,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._session += 1
            self._store.invalidate()
        self.capture.close()
