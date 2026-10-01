from __future__ import annotations

import logging
import math
import queue
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event, Lock, Thread

from runtime_agent.gameworker.config import InputBindings, WorkerMode
from runtime_agent.gameworker.fixed_ui import (
    DST_FIXED_1280X720,
    FIXED_UI_ACTION_TARGETS,
)
from runtime_agent.gameworker.geometry import NormalizedPoint, Viewport
from runtime_agent.gameworker.input import DeadmanSafety, InputController, InputError

logger = logging.getLogger("runtime_agent.gameworker.actions")


class ActionName(StrEnum):
    NONE = "NONE"
    MOVE_FORWARD = "MOVE_FORWARD"
    MOVE_BACKWARD = "MOVE_BACKWARD"
    TURN_LEFT = "TURN_LEFT"
    TURN_RIGHT = "TURN_RIGHT"
    PAUSE_WORLD = "PAUSE_WORLD"
    INTERACT = "INTERACT"
    HOVER_GIFT_ICON = "HOVER_GIFT_ICON"
    CLICK_GIFT_ICON = "CLICK_GIFT_ICON"
    CLICK_INWORLD_USE_LATER = "CLICK_INWORLD_USE_LATER"
    CANCEL = "CANCEL"
    RESUME_WORLD = "RESUME_WORLD"
    OPEN_INVENTORY = "OPEN_INVENTORY"
    OPEN_CRAFTING_MENU = "OPEN_CRAFTING_MENU"
    STOP_MOVEMENT = "STOP_MOVEMENT"
    RELEASE_ALL = "RELEASE_ALL"
    RECOVERY = "RECOVERY"
    PAUSE = "PAUSE"
    CLICK_REWARD_OPEN = "CLICK_REWARD_OPEN"
    CLICK_REWARD_CLOSE = "CLICK_REWARD_CLOSE"
    CLICK_REWARD_NEXT = "CLICK_REWARD_NEXT"
    CLICK_OPTIONS = "CLICK_OPTIONS"
    CLICK_BACK = "CLICK_BACK"
    DISCARD_OPTIONS = "DISCARD_OPTIONS"
    CLICK_HOST_GAME = "CLICK_HOST_GAME"
    SELECT_EXISTING_WORLD = "SELECT_EXISTING_WORLD"
    START_EXISTING_WORLD = "START_EXISTING_WORLD"
    CONFIRM_MODS_DISABLED = "CONFIRM_MODS_DISABLED"
    SELECT_SURVIVOR = "SELECT_SURVIVOR"
    START_SURVIVOR = "START_SURVIVOR"
    SELECT_SURVIVAL = "SELECT_SURVIVAL"
    SELECT_NO_CAVES = "SELECT_NO_CAVES"


class ActionStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    PREEMPTED = "PREEMPTED"
    FAILED = "FAILED"
    STALE_GENERATION = "STALE_GENERATION"
    GAME_NOT_READY = "GAME_NOT_READY"
    SAFETY_BLOCKED = "SAFETY_BLOCKED"
    SUPPRESSED = "SUPPRESSED"


@dataclass(frozen=True, slots=True)
class Action:
    """Canonical, credential-free and pickle-safe gameplay intent."""

    action_id: str
    name: ActionName
    runtime_generation: int
    worker_generation: int
    runtime_id: int = 0
    duration: float | None = None
    deadline: float | None = None
    created_at: float = field(default_factory=time.monotonic)
    sequence: int = 0
    parameters: tuple[tuple[str, bool | int | float | str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    action: ActionName
    status: ActionStatus
    duration: float
    runtime_generation: int
    worker_generation: int
    runtime_id: int = 0
    reason: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status not in {
            ActionStatus.PENDING, ActionStatus.SENT, ActionStatus.VERIFYING,
        }

    @property
    def dry_run(self) -> bool:
        return self.status == ActionStatus.SUPPRESSED

    @property
    def result(self) -> str:
        if self.status in {ActionStatus.COMPLETED, ActionStatus.SUCCEEDED}:
            return "OK"
        if self.status == ActionStatus.SUPPRESSED:
            return "WOULD_EXECUTE"
        return self.status


@dataclass(frozen=True, slots=True)
class SafetyState:
    configured_mode: WorkerMode
    effective_mode: WorkerMode
    runtime_verified: bool
    game_ready: bool
    healthy: bool = True
    paused: bool = False
    stopping: bool = False


class ActionTicket:
    def __init__(self, action: Action):
        self.action = action
        self._done = Event()
        self._lock = Lock()
        self._result: ActionResult | None = None
        self.cancel_status: ActionStatus | None = None
        self.cancel_reason: str | None = None

    @property
    def done(self) -> bool:
        return self._done.is_set()

    @property
    def result(self) -> ActionResult | None:
        return self._result

    def wait(self, timeout: float | None = None) -> ActionResult | None:
        self._done.wait(timeout)
        return self._result

    def _finish(self, result: ActionResult) -> bool:
        with self._lock:
            if self._done.is_set():
                return False
            self._result = result
            self._done.set()
            return True


_MOVEMENT_KEYS = {
    ActionName.MOVE_FORWARD: "move_up",
    ActionName.MOVE_BACKWARD: "move_down",
    ActionName.TURN_LEFT: "move_left",
    ActionName.TURN_RIGHT: "move_right",
}
_OPPOSITES = {
    ActionName.MOVE_FORWARD: ActionName.MOVE_BACKWARD,
    ActionName.MOVE_BACKWARD: ActionName.MOVE_FORWARD,
    ActionName.TURN_LEFT: ActionName.TURN_RIGHT,
    ActionName.TURN_RIGHT: ActionName.TURN_LEFT,
}
_PRESS_KEYS = {
    ActionName.INTERACT: "interact",
    ActionName.CANCEL: "cancel",
    ActionName.RESUME_WORLD: "cancel",
    ActionName.OPEN_INVENTORY: "inventory",
    ActionName.OPEN_CRAFTING_MENU: "open_crafting",
    ActionName.PAUSE_WORLD: "cancel",
}
CLICK_REGIONS = {
    ActionName.HOVER_GIFT_ICON: (0.115, 0.0, 0.36, 0.14),
    # Guard only. The actual point is resolved from the current gift detection.
    ActionName.CLICK_GIFT_ICON: (0.115, 0.0, 0.36, 0.14),
    ActionName.CLICK_INWORLD_USE_LATER: (0.34, 0.79, 0.51, 0.9),
    ActionName.CLICK_REWARD_OPEN: (0.35, 0.75, 0.65, 0.97),
    ActionName.CLICK_REWARD_CLOSE: (0.35, 0.75, 0.65, 0.97),
    ActionName.CLICK_REWARD_NEXT: (0.675, 0.49, 0.75, 0.66),
    ActionName.CLICK_OPTIONS: (0.02, 0.66, 0.18, 0.75),
    ActionName.CLICK_BACK: (0.02, 0.87, 0.15, 0.99),
    ActionName.DISCARD_OPTIONS: (0.32, 0.54, 0.50, 0.61),
    ActionName.CLICK_HOST_GAME: (0.02, 0.42, 0.28, 0.65),
    ActionName.SELECT_EXISTING_WORLD: (0.15, 0.20, 0.86, 0.40),
    # Match the selected-world Resume World detector region. The click point is
    # still resolved from that live template detection; this is only its guard.
    ActionName.START_EXISTING_WORLD: (0.74, 0.89, 0.94, 0.99),
    ActionName.CONFIRM_MODS_DISABLED: (0.32, 0.75, 0.50, 0.84),
    ActionName.SELECT_SURVIVOR: (0.25, 0.14, 0.42, 0.37),
    ActionName.START_SURVIVOR: (0.81, 0.9, 0.97, 0.98),
    ActionName.SELECT_SURVIVAL: (0.40, 0.35, 0.60, 0.67),
    ActionName.SELECT_NO_CAVES: (0.48, 0.30, 0.64, 0.60),
}
_SAFETY_ACTIONS = {ActionName.STOP_MOVEMENT, ActionName.RELEASE_ALL}


class ActionExecutor:
    """Bounded, single-owner executor for one runtime/worker generation."""

    def __init__(
        self,
        controller: InputController,
        deadman: DeadmanSafety,
        bindings: InputBindings,
        *,
        runtime_generation: int,
        worker_generation: int,
        runtime_id: int = 0,
        mode: WorkerMode,
        action_timeout: float,
        queue_size: int = 16,
        clock: Callable[[], float] = time.monotonic,
        allowed_actions: frozenset[ActionName] | None = None,
    ):
        if queue_size < 1:
            raise ValueError("action queue size must be positive")
        if action_timeout <= 0:
            raise ValueError("action timeout must be positive")
        self.controller = controller
        self.deadman = deadman
        self.bindings = bindings
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self.runtime_id = runtime_id
        self.action_timeout = action_timeout
        self.allowed_actions = allowed_actions
        self._clock = clock
        self._queue: queue.Queue[ActionTicket] = queue.Queue(maxsize=queue_size)
        self._lock = Lock()
        self._stop = Event()
        self._closed = False
        self._faulted = False
        self._safety_latched = False
        self._current: ActionTicket | None = None
        self._pending: dict[str, ActionTicket] = {}
        self._completed: OrderedDict[str, ActionResult] = OrderedDict()
        self._result_limit = max(
            128,
            queue_size * 4,
            math.ceil(controller.action_limiter.rate * action_timeout) + queue_size + 8,
        )
        self._state = SafetyState(
            configured_mode=mode,
            effective_mode=mode,
            runtime_verified=mode == WorkerMode.ACTIVE,
            game_ready=mode == WorkerMode.ACTIVE,
        )
        self._thread = Thread(
            target=self._run, name=f"action-executor-{worker_generation}", daemon=True
        )
        self._thread.start()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    @property
    def current_action_id(self) -> str | None:
        with self._lock:
            return self._current.action.action_id if self._current else None

    def update_safety(
        self,
        *,
        configured_mode: WorkerMode,
        effective_mode: WorkerMode,
        runtime_verified: bool,
        game_ready: bool,
        healthy: bool,
        paused: bool,
        stopping: bool,
    ) -> None:
        state = SafetyState(
            configured_mode,
            effective_mode,
            runtime_verified,
            game_ready,
            healthy,
            paused,
            stopping,
        )
        with self._lock:
            previous = self._state
            self._state = state
        was_active = self._gate_is_open(previous)
        is_active = self._gate_is_open(state)
        if was_active and not is_active:
            self.cancel_all(
                status=ActionStatus.PREEMPTED,
                reason="safety gate closed",
                revoke=True,
            )
        elif is_active and not self.deadman.tripped and not self._faulted:
            with self._lock:
                self._safety_latched = False
            self.controller.activate()

    def reset(self) -> None:
        self.deadman.reset()
        with self._lock:
            self._faulted = False
            self._safety_latched = False
            active = self._gate_is_open(self._state) and not self._closed
        if active:
            self.controller.activate()

    @staticmethod
    def _gate_is_open(state: SafetyState) -> bool:
        return (
            state.configured_mode == WorkerMode.ACTIVE
            and state.effective_mode == WorkerMode.ACTIVE
            and state.runtime_verified
            and state.game_ready
            and state.healthy
            and not state.paused
            and not state.stopping
        )

    def _result(
        self,
        action: Action,
        status: ActionStatus,
        *,
        started: float | None = None,
        reason: str | None = None,
    ) -> ActionResult:
        return ActionResult(
            action.action_id,
            action.name if isinstance(action.name, ActionName) else ActionName.NONE,
            status,
            max(0.0, self._clock() - started) if started is not None else 0.0,
            action.runtime_generation,
            action.worker_generation,
            action.runtime_id,
            reason,
        )

    def _validate(self, action: Action) -> ActionResult | None:
        if not isinstance(action.action_id, str) or not action.action_id.strip():
            return self._result(
                action, ActionStatus.REJECTED, reason="invalid action id"
            )
        if not isinstance(action.name, ActionName) or action.name in {
            ActionName.NONE,
            ActionName.RECOVERY,
            ActionName.PAUSE,
        }:
            return self._result(action, ActionStatus.REJECTED, reason="invalid action")
        if (
            self.allowed_actions is not None
            and action.name not in self.allowed_actions
            and action.name not in _SAFETY_ACTIONS
        ):
            return self._result(
                action, ActionStatus.REJECTED, reason="action not whitelisted"
            )
        if action.runtime_generation != self.runtime_generation:
            return self._result(
                action, ActionStatus.STALE_GENERATION, reason="stale runtime generation"
            )
        if self.runtime_id and action.runtime_id != self.runtime_id:
            return self._result(
                action, ActionStatus.STALE_GENERATION, reason="stale runtime identity"
            )
        if action.worker_generation != self.worker_generation:
            return self._result(
                action, ActionStatus.STALE_GENERATION, reason="stale worker generation"
            )
        if action.name not in _SAFETY_ACTIONS and action.deadline is None:
            return self._result(
                action,
                ActionStatus.REJECTED,
                reason="gameplay action deadline required",
            )
        if (
            not math.isfinite(action.created_at)
            or action.created_at > self._clock() + 0.1
        ):
            return self._result(
                action, ActionStatus.REJECTED, reason="invalid action creation time"
            )
        if action.deadline is not None and (
            not math.isfinite(action.deadline) or action.deadline <= self._clock()
        ):
            return self._result(action, ActionStatus.TIMED_OUT, reason="action expired")
        if (
            action.deadline is not None
            and action.deadline > action.created_at + self.action_timeout
        ):
            return self._result(
                action, ActionStatus.REJECTED, reason="action deadline exceeds bound"
            )
        if action.name in _MOVEMENT_KEYS and (
            action.duration is None
            or not math.isfinite(action.duration)
            or action.duration <= 0
            or action.duration > self.action_timeout
        ):
            return self._result(
                action,
                ActionStatus.REJECTED,
                reason="movement duration missing or outside configured bound",
            )
        if action.duration is not None and (
            not math.isfinite(action.duration)
            or action.duration < 0
            or action.duration > self.action_timeout
        ):
            return self._result(
                action, ActionStatus.REJECTED, reason="invalid action duration"
            )
        if not isinstance(action.parameters, tuple) or any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], (bool, int, float, str, type(None)))
            for item in action.parameters
        ):
            return self._result(
                action, ActionStatus.REJECTED, reason="invalid action parameters"
            )
        if action.name in CLICK_REGIONS:
            values = dict(action.parameters)
            try:
                point = NormalizedPoint(float(values["x"]), float(values["y"]))
                viewport = Viewport(int(values["width"]), int(values["height"]))
                left, top, right, bottom = CLICK_REGIONS[action.name]
                expected_keys = {"x", "y", "width", "height"}
                if action.name == ActionName.CLICK_GIFT_ICON:
                    expected_keys.add("evidence_sequence")
                if set(values) != expected_keys or not (
                    left < point.x < right and top < point.y < bottom
                ):
                    raise ValueError("anchor outside guarded UI region")
                if action.name == ActionName.CLICK_GIFT_ICON and (
                    not isinstance(values["evidence_sequence"], int)
                    or values["evidence_sequence"] < 1
                ):
                    raise ValueError("gift detection sequence is invalid")
                if (
                    not 640 <= viewport.width <= 4096
                    or not 480 <= viewport.height <= 2160
                ):
                    raise ValueError("invalid viewport")
                fixed_target = FIXED_UI_ACTION_TARGETS.get(action.name.value)
                if fixed_target is not None:
                    DST_FIXED_1280X720.point(
                        fixed_target, viewport.width, viewport.height
                    )
            except (KeyError, TypeError, ValueError, OverflowError):
                return self._result(
                    action, ActionStatus.REJECTED, reason="invalid action anchor"
                )
        return None

    def submit(self, action: Action) -> ActionTicket:
        ticket = ActionTicket(action)
        with self._lock:
            completed = self._completed.get(action.action_id)
            if completed is not None:
                ticket._finish(completed)
                return ticket
            pending = self._pending.get(action.action_id)
            if pending is not None:
                return pending
            invalid = self._validate(action)
            if invalid is None and not self._closed:
                self._pending[action.action_id] = ticket
        if invalid is not None:
            self._finish(ticket, invalid)
            return ticket
        if action.name in _SAFETY_ACTIONS:
            self.cancel_all(
                status=ActionStatus.PREEMPTED,
                reason=f"{action.name.lower()} requested",
                revoke=True,
            )
            self._finish(ticket, self._result(action, ActionStatus.COMPLETED))
            return ticket
        with self._lock:
            if self._closed:
                self._pending.pop(action.action_id, None)
                result = self._result(
                    action, ActionStatus.SAFETY_BLOCKED, reason="executor stopped"
                )
            else:
                result = None
                opposite = _OPPOSITES.get(action.name)
                if opposite is not None:
                    for other in self._pending.values():
                        if other.action.name == opposite and not other.done:
                            other.cancel_status = ActionStatus.PREEMPTED
                            other.cancel_reason = f"preempted by {action.name}"
                try:
                    self._queue.put_nowait(ticket)
                except queue.Full:
                    self._pending.pop(action.action_id, None)
                    result = self._result(
                        action, ActionStatus.REJECTED, reason="action queue full"
                    )
        if result is not None:
            self._finish(ticket, result)
        return ticket

    def execute(self, action: Action) -> ActionResult:
        ticket = self.submit(action)
        wait_for = self.action_timeout + 1.0
        if action.deadline is not None:
            wait_for = max(0.0, min(wait_for, action.deadline - self._clock() + 0.1))
        result = ticket.wait(wait_for)
        if result is not None:
            return result
        with self._lock:
            self._faulted = True
        self.cancel_all(
            status=ActionStatus.TIMED_OUT,
            reason="executor wait timed out",
            revoke=True,
        )
        result = ticket.wait(1.0)
        if result is not None:
            return result
        result = self._result(
            action, ActionStatus.TIMED_OUT, reason="executor did not stop in time"
        )
        self._finish(ticket, result)
        return result

    def cancel_action(
        self,
        action_id: str,
        *,
        status: ActionStatus = ActionStatus.CANCELLED,
        reason: str = "action cancelled",
    ) -> bool:
        with self._lock:
            ticket = self._pending.get(action_id)
            if ticket is None:
                return action_id in self._completed
            ticket.cancel_status = status
            ticket.cancel_reason = reason
        return True

    def cancel_all(
        self,
        *,
        status: ActionStatus = ActionStatus.CANCELLED,
        reason: str = "all actions cancelled",
        revoke: bool = True,
    ) -> None:
        with self._lock:
            for ticket in self._pending.values():
                if not ticket.done:
                    ticket.cancel_status = status
                    ticket.cancel_reason = reason
            if revoke:
                self._safety_latched = True
        if revoke:
            self.controller.revoke()
        self.controller.release_all(reason=reason)

    def release_inputs(self, *, reason: str) -> None:
        """Serialize a transient cleanup without permanently closing an open gate."""
        with self._lock:
            already_latched = self._safety_latched
        self.cancel_all(
            status=ActionStatus.PREEMPTED,
            reason=reason,
            revoke=True,
        )
        with self._lock:
            can_reactivate = (
                not already_latched
                and not self._closed
                and not self._faulted
                and not self.deadman.tripped
                and self._gate_is_open(self._state)
            )
            if can_reactivate:
                self._safety_latched = False
        if can_reactivate:
            self.controller.activate()

    def _gate(self, action: Action) -> ActionResult | None:
        with self._lock:
            state = self._state
            closed = self._closed
            faulted = self._faulted
            safety_latched = self._safety_latched
        if action.deadline is not None and action.deadline <= self._clock():
            return self._result(action, ActionStatus.TIMED_OUT, reason="action expired")
        if state.configured_mode == WorkerMode.OBSERVE:
            return self._result(action, ActionStatus.SUPPRESSED, reason="OBSERVE mode")
        if not state.runtime_verified or not state.game_ready:
            return self._result(
                action, ActionStatus.GAME_NOT_READY, reason="game is not ready"
            )
        if (
            closed
            or faulted
            or safety_latched
            or self.deadman.tripped
            or not self._gate_is_open(state)
        ):
            return self._result(
                action, ActionStatus.SAFETY_BLOCKED, reason="safety gate closed"
            )
        return None

    def _execute_ticket(self, ticket: ActionTicket) -> ActionResult:
        action = ticket.action
        if ticket.cancel_status is not None:
            return self._result(
                action, ticket.cancel_status, reason=ticket.cancel_reason
            )
        gated = self._gate(action)
        if gated is not None:
            return gated
        started = self._clock()
        try:
            with self.controller.lease.acquire(timeout=min(0.2, self.action_timeout)):
                if ticket.cancel_status is not None:
                    return self._result(
                        action,
                        ticket.cancel_status,
                        started=started,
                        reason=ticket.cancel_reason,
                    )
                if (
                    action.name not in CLICK_REGIONS
                    and not self.controller.action_limiter.allow()
                ):
                    return self._result(
                        action,
                        ActionStatus.REJECTED,
                        started=started,
                        reason="action rate limit exceeded",
                    )
                self.deadman.touch()
                if action.name in _MOVEMENT_KEYS:
                    key = getattr(self.bindings, _MOVEMENT_KEYS[action.name])
                    self.controller.key_down(key)
                    try:
                        assert action.duration is not None
                        end = started + action.duration
                        while self._clock() < end:
                            if ticket.cancel_status is not None:
                                return self._result(
                                    action,
                                    ticket.cancel_status,
                                    started=started,
                                    reason=ticket.cancel_reason,
                                )
                            if (
                                action.deadline is not None
                                and self._clock() >= action.deadline
                            ):
                                return self._result(
                                    action,
                                    ActionStatus.TIMED_OUT,
                                    started=started,
                                    reason="deadline reached while executing",
                                )
                            if self.deadman.tripped:
                                return self._result(
                                    action,
                                    ActionStatus.SAFETY_BLOCKED,
                                    started=started,
                                    reason="deadman tripped",
                                )
                            self.deadman.touch()
                            self._stop.wait(min(0.02, max(0.0, end - self._clock())))
                    finally:
                        self.controller.key_up(key)
                elif action.name in CLICK_REGIONS:
                    values = dict(action.parameters)
                    mouse_action = (
                        self.controller.hover
                        if action.name == ActionName.HOVER_GIFT_ICON
                        else self.controller.click
                    )
                    mouse_action(
                        NormalizedPoint(float(values["x"]), float(values["y"])),
                        Viewport(int(values["width"]), int(values["height"])),
                    )
                else:
                    key = getattr(self.bindings, _PRESS_KEYS[action.name])
                    self.controller.key_press(key)
            if ticket.cancel_status is not None:
                return self._result(action, ticket.cancel_status, started=started,
                                    reason=ticket.cancel_reason)
            if action.deadline is not None and self._clock() >= action.deadline:
                return self._result(action, ActionStatus.TIMED_OUT, started=started,
                                    reason="transport exceeded deadline")
            return self._result(action, ActionStatus.SENT, started=started)
        except InputError as exc:
            with self._lock:
                self._faulted = True
            self.controller.revoke()
            self.controller.release_all(reason="action_failure")
            logger.exception(
                "action input failed action_id=%s action_type=%s "
                "runtime_id=%s runtime_generation=%s worker_generation=%s",
                action.action_id,
                action.name,
                action.runtime_id,
                action.runtime_generation,
                action.worker_generation,
            )
            return self._result(
                action, ActionStatus.FAILED, started=started, reason=str(exc)
            )
        except Exception as exc:
            with self._lock:
                self._faulted = True
            self.controller.revoke()
            self.controller.release_all(reason="action_exception")
            logger.exception("action executor failure action_id=%s", action.action_id)
            return self._result(
                action, ActionStatus.FAILED, started=started, reason=str(exc)
            )

    def _finish(self, ticket: ActionTicket, result: ActionResult) -> None:
        if not ticket._finish(result):
            return
        with self._lock:
            self._pending.pop(ticket.action.action_id, None)
            self._completed[ticket.action.action_id] = result
            self._completed.move_to_end(ticket.action.action_id)
            while len(self._completed) > self._result_limit:
                self._completed.popitem(last=False)
        logger.info(
            "action terminal action_id=%s action_type=%s result=%s "
            "runtime_id=%s runtime_generation=%s worker_generation=%s reason=%s",
            result.action_id,
            result.action,
            result.status,
            result.runtime_id,
            result.runtime_generation,
            result.worker_generation,
            result.reason,
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                ticket = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    self._current = ticket
                result = self._execute_ticket(ticket)
                self._finish(ticket, result)
            finally:
                with self._lock:
                    if self._current is ticket:
                        self._current = None
                self._queue.task_done()
        while True:
            try:
                ticket = self._queue.get_nowait()
            except queue.Empty:
                break
            self._finish(
                ticket,
                self._result(
                    ticket.action,
                    ticket.cancel_status or ActionStatus.CANCELLED,
                    reason=ticket.cancel_reason or "executor stopped",
                ),
            )
            self._queue.task_done()

    def shutdown(self) -> bool:
        with self._lock:
            self._closed = True
            self._state = SafetyState(
                self._state.configured_mode,
                self._state.effective_mode,
                self._state.runtime_verified,
                self._state.game_ready,
                self._state.healthy,
                self._state.paused,
                True,
            )
        self.cancel_all(
            status=ActionStatus.PREEMPTED,
            reason="executor shutdown",
            revoke=True,
        )
        self._stop.set()
        if self._thread.is_alive():
            driver_timeout = float(getattr(self.controller.driver, "timeout", 0.0))
            self._thread.join(
                timeout=self.action_timeout + max(1.0, driver_timeout) + 0.5
            )
        self.controller.release_all(reason="executor_shutdown")
        return not self._thread.is_alive()


class GameActions:
    """Compatibility facade and semantic DST binding layer."""

    def __init__(
        self,
        controller: InputController,
        deadman: DeadmanSafety,
        bindings: InputBindings,
        *,
        mode: WorkerMode,
        action_timeout: float,
        runtime_generation: int = 0,
        worker_generation: int = 0,
        runtime_id: int = 0,
        queue_size: int = 16,
        allowed_actions: frozenset[ActionName] | None = None,
    ):
        self.controller = controller
        self.deadman = deadman
        self.bindings = bindings
        self.mode = mode
        self.action_timeout = action_timeout
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self.runtime_id = runtime_id
        self._sequence = 0
        self._sequence_lock = Lock()
        self.executor = ActionExecutor(
            controller,
            deadman,
            bindings,
            runtime_generation=runtime_generation,
            worker_generation=worker_generation,
            runtime_id=runtime_id,
            mode=mode,
            action_timeout=action_timeout,
            queue_size=queue_size,
            allowed_actions=allowed_actions,
        )

    def _new_action(
        self,
        name: ActionName,
        *,
        duration: float | None = None,
        parameters: tuple = (),
        valid_until: float | None = None,
    ) -> Action:
        with self._sequence_lock:
            self._sequence += 1
            sequence = self._sequence
        return Action(
            action_id=(
                f"runtime-{self.runtime_generation}:worker-{self.worker_generation}:"
                f"action-{sequence}"
            ),
            name=name,
            runtime_generation=self.runtime_generation,
            worker_generation=self.worker_generation,
            runtime_id=self.runtime_id,
            duration=duration,
            deadline=min(
                time.monotonic() + self.action_timeout,
                valid_until if valid_until is not None else float("inf"),
            ),
            sequence=sequence,
            parameters=parameters,
        )

    def set_mode(self, mode: WorkerMode) -> None:
        self.mode = mode
        self.executor.update_safety(
            configured_mode=mode,
            effective_mode=mode,
            runtime_verified=mode == WorkerMode.ACTIVE,
            game_ready=mode == WorkerMode.ACTIVE,
            healthy=True,
            paused=False,
            stopping=False,
        )

    def set_safety(self, **values) -> None:
        self.mode = values["configured_mode"]
        self.executor.update_safety(**values)

    def cancel(self) -> None:
        self.executor.cancel_all(reason="game action cancellation", revoke=True)

    def release_all(self) -> None:
        self.executor.release_inputs(reason="release_all")

    def reset_cancel(self) -> None:
        self.executor.reset()

    def shutdown(self) -> bool:
        return self.executor.shutdown()

    def execute_action(self, action: Action) -> ActionResult:
        return self.executor.execute(action)

    def move_forward(self, duration: float | None = None) -> ActionResult:
        return self.execute_action(
            self._new_action(ActionName.MOVE_FORWARD, duration=duration)
        )

    def move_backward(self, duration: float | None = None) -> ActionResult:
        return self.execute_action(
            self._new_action(ActionName.MOVE_BACKWARD, duration=duration)
        )

    def turn_left(self, duration: float | None = None) -> ActionResult:
        return self.execute_action(
            self._new_action(ActionName.TURN_LEFT, duration=duration)
        )

    def turn_right(self, duration: float | None = None) -> ActionResult:
        return self.execute_action(
            self._new_action(ActionName.TURN_RIGHT, duration=duration)
        )

    def interact(self) -> ActionResult:
        return self.execute_action(self._new_action(ActionName.INTERACT))

    def cancel_ui(self) -> ActionResult:
        return self.execute_action(self._new_action(ActionName.CANCEL))

    def open_inventory(self) -> ActionResult:
        return self.execute_action(self._new_action(ActionName.OPEN_INVENTORY))

    def execute(
        self,
        action: ActionName,
        *,
        duration: float | None = None,
        target: NormalizedPoint | None = None,
        viewport: Viewport | None = None,
        valid_until: float | None = None,
        evidence_sequence: int | None = None,
    ) -> ActionResult:
        parameters = ()
        if action in CLICK_REGIONS:
            if target is None or viewport is None:
                raise ValueError("click requires detected target and viewport")
            parameters = (
                ("x", target.x),
                ("y", target.y),
                ("width", viewport.width),
                ("height", viewport.height),
            )
            if action == ActionName.CLICK_GIFT_ICON:
                if valid_until is None or evidence_sequence is None:
                    raise ValueError("gift click requires fresh detection evidence")
                parameters += (("evidence_sequence", evidence_sequence),)
        return self.execute_action(
            self._new_action(
                action,
                duration=duration,
                parameters=parameters,
                valid_until=valid_until,
            )
        )


class ObserveActions:
    """OBSERVE-only action boundary with no input driver or executor thread."""

    input_free = True

    def __init__(
        self, *, runtime_id: int, runtime_generation: int, worker_generation: int
    ):
        self.runtime_id = runtime_id
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self._sequence = 0

    def execute(
        self,
        action: ActionName,
        *,
        duration: float | None = None,
        target: NormalizedPoint | None = None,
        viewport: Viewport | None = None,
        valid_until: float | None = None,
        evidence_sequence: int | None = None,
    ) -> ActionResult:
        self._sequence += 1
        return ActionResult(
            f"observe-action-{self._sequence}",
            action,
            ActionStatus.SUPPRESSED,
            0.0,
            self.runtime_generation,
            self.worker_generation,
            runtime_id=self.runtime_id,
            reason="OBSERVE has no input controller",
        )

    def execute_action(self, action: Action) -> ActionResult:
        return ActionResult(
            action.action_id,
            action.name,
            ActionStatus.SUPPRESSED,
            0.0,
            self.runtime_generation,
            self.worker_generation,
            runtime_id=self.runtime_id,
            reason="OBSERVE has no input controller",
        )

    def set_safety(self, **values) -> None:
        return None

    def cancel(self) -> None:
        return None

    def release_all(self) -> None:
        return None

    def reset_cancel(self) -> None:
        return None

    def shutdown(self) -> bool:
        return True
