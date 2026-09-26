from __future__ import annotations

import io
import json
import os
import queue
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from enum import Enum, StrEnum
from pathlib import Path, PurePosixPath
from threading import Event, Lock, Thread
from typing import Any

from runtime_agent.gameworker.actions import Action, ActionResult
from runtime_agent.gameworker.activity import ActionProposal
from runtime_agent.gameworker.capture import Frame
from runtime_agent.gameworker.vision import GameObservation

RECORDING_FORMAT_VERSION = 1
MAX_EVENT_BYTES = 64 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_RECORDING_BUFFER_BYTES = 256 * 1024 * 1024
_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_RUNTIME_INSTANCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SENSITIVE_KEYS = ("secret", "token", "password", "credential", "database_url")
_CAPTURE_METADATA_KEYS = {"kind", "backend", "version", "max_width", "max_height"}
_CALIBRATION_METADATA_KEYS = {
    "profile_id",
    "version",
    "verified",
    "expected_width",
    "expected_height",
}
_PERCEPTION_METADATA_KEYS = {
    "engine",
    "engine_version",
    "registry_version",
    "assets_configured",
    "assets_verified",
}


class RecordingStatus(StrEnum):
    INTERRUPTED = "INTERRUPTED"
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    FAILED = "FAILED"


class RecordingEventType(StrEnum):
    SESSION_STARTED = "SESSION_STARTED"
    WORKER_STARTED = "WORKER_STARTED"
    GAME_READY = "GAME_READY"
    GAME_LOST = "GAME_LOST"
    FRAME_CAPTURED = "FRAME_CAPTURED"
    OBSERVATION_PRODUCED = "OBSERVATION_PRODUCED"
    PLANNER_PROPOSAL = "PLANNER_PROPOSAL"
    ACTION_RESULT = "ACTION_RESULT"
    CAPTURE_ERROR = "CAPTURE_ERROR"
    PERCEPTION_ERROR = "PERCEPTION_ERROR"
    RECORDING_DROP = "RECORDING_DROP"
    USER_MARKER = "USER_MARKER"
    WORKER_PAUSED = "WORKER_PAUSED"
    WORKER_RESUMED = "WORKER_RESUMED"
    SHUTDOWN = "SHUTDOWN"


@dataclass(frozen=True, slots=True)
class RecordingLimits:
    max_duration_seconds: float = 15 * 60
    max_frames: int = 1800
    max_bytes: int = 512 * 1024 * 1024
    queue_size: int = 8
    frame_interval_seconds: float = 0.5
    shutdown_timeout_seconds: float = 5.0

    def validate(self) -> None:
        if not 1 <= self.max_duration_seconds <= 24 * 60 * 60:
            raise ValueError("recording duration must be between 1 second and 24 hours")
        if not 1 <= self.max_frames <= 100_000:
            raise ValueError("recording max_frames must be between 1 and 100000")
        if not 1024 * 1024 <= self.max_bytes <= 100 * 1024 * 1024 * 1024:
            raise ValueError("recording max_bytes is outside the safe range")
        if not 1 <= self.queue_size <= 64:
            raise ValueError("recording queue_size must be between 1 and 64")
        if not 0 <= self.frame_interval_seconds <= 60:
            raise ValueError(
                "recording frame interval must be between 0 and 60 seconds"
            )
        if not 0.1 <= self.shutdown_timeout_seconds <= 30:
            raise ValueError(
                "recording shutdown timeout must be between 0.1 and 30 seconds"
            )


@dataclass(frozen=True, slots=True)
class RecordingSession:
    session_id: str
    path: Path
    format_version: int
    created_at: str
    runtime_instance_id: str
    runtime_id: int
    runtime_generation: int
    worker_generation: int
    worker_mode: str

    def __post_init__(self) -> None:
        if not _SESSION_ID.fullmatch(self.session_id):
            raise ValueError("invalid recording session ID")
        if self.format_version != RECORDING_FORMAT_VERSION:
            raise ValueError("unsupported recording format version")
        if min(self.runtime_id, self.runtime_generation, self.worker_generation) < 1:
            raise ValueError("recording generation identity is invalid")
        if not self.runtime_instance_id or len(self.runtime_instance_id) > 128:
            raise ValueError("runtime instance ID is invalid")
        if self.worker_mode not in {"OBSERVE", "ACTIVE"}:
            raise ValueError("recording worker mode must be OBSERVE or ACTIVE")
        try:
            created = datetime.fromisoformat(self.created_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("recording creation timestamp is invalid") from exc
        if created.tzinfo is None:
            raise ValueError("recording creation timestamp must include a timezone")


@dataclass(frozen=True, slots=True)
class RecordingEvent:
    event_type: RecordingEventType
    sequence: int
    timestamp: str
    relative_monotonic: float
    runtime_generation: int
    worker_generation: int
    frame_id: str | None = None
    observation_id: str | None = None
    action_id: str | None = None
    payload: Mapping[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "sequence": self.sequence,
            "timestamp": self.timestamp,
            "relative_monotonic": self.relative_monotonic,
            "runtime_generation": self.runtime_generation,
            "worker_generation": self.worker_generation,
            "frame_id": self.frame_id,
            "observation_id": self.observation_id,
            "action_id": self.action_id,
            "payload": self.payload or {},
        }


@dataclass(frozen=True, slots=True)
class _WriteItem:
    kind: str
    value: Frame | RecordingEvent


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        raise ValueError("recording payload nesting exceeds bound")
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str) and len(value) > 4096:
            raise ValueError("recording string exceeds bound")
        return value
    if isinstance(value, float):
        if not (-float("inf") < value < float("inf")):
            raise ValueError("recording payload contains non-finite number")
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise ValueError("recording mapping exceeds bound")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise ValueError("recording payload key is invalid")
            if any(marker in key.lower() for marker in _SENSITIVE_KEYS):
                raise ValueError("sensitive recording payload key is forbidden")
            result[key] = _json_value(item, depth=depth + 1)
        return result
    if isinstance(value, (tuple, list)):
        if len(value) > 256:
            raise ValueError("recording collection exceeds bound")
        return [_json_value(item, depth=depth + 1) for item in value]
    raise TypeError(f"unsupported recording payload type: {type(value).__name__}")


def _encoded_json(value: Mapping[str, Any], *, limit: int = MAX_EVENT_BYTES) -> bytes:
    encoded = json.dumps(
        _json_value(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > limit:
        raise ValueError("recording JSON object exceeds size bound")
    return encoded


def _allowlisted_metadata(
    value: Mapping[str, Any], allowed: set[str], name: str
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError(f"recording {name} metadata contains unsupported fields")
    return _json_value(dict(value))


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = _encoded_json(value, limit=MAX_MANIFEST_BYTES)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class SessionRecorder:
    """Non-blocking, bounded writer owned by exactly one worker generation."""

    def __init__(
        self,
        root: Path,
        *,
        runtime_instance_id: str,
        runtime_id: int,
        runtime_generation: int,
        worker_generation: int,
        worker_mode: str,
        capture_source: Mapping[str, Any],
        calibration: Mapping[str, Any],
        perception: Mapping[str, Any],
        limits: RecordingLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], str] = _utcnow,
    ):
        self.limits = limits or RecordingLimits()
        self.limits.validate()
        root = Path(root)
        if not (root.is_absolute() or PurePosixPath(root.as_posix()).is_absolute()):
            raise ValueError("recording root must be absolute")
        if min(runtime_id, runtime_generation, worker_generation) < 1:
            raise ValueError("recording generation identity is invalid")
        if not _RUNTIME_INSTANCE_ID.fullmatch(runtime_instance_id):
            raise ValueError("runtime instance ID is invalid")
        capture_metadata = _allowlisted_metadata(
            capture_source, _CAPTURE_METADATA_KEYS, "capture source"
        )
        calibration_metadata = _allowlisted_metadata(
            calibration, _CALIBRATION_METADATA_KEYS, "calibration"
        )
        perception_metadata = _allowlisted_metadata(
            perception, _PERCEPTION_METADATA_KEYS, "perception"
        )
        if (
            "kind" not in capture_metadata
            or not {"profile_id", "version"} <= calibration_metadata.keys()
            or "engine" not in perception_metadata
        ):
            raise ValueError("recording provenance metadata is incomplete")
        self._clock = clock
        self._wall_clock = wall_clock
        self._started = clock()
        session_id = uuid.uuid4().hex
        session_path = root / session_id
        root.mkdir(parents=True, exist_ok=True)
        session_path.mkdir(exist_ok=False)
        (session_path / "frames").mkdir()
        (session_path / "events.jsonl").touch(exist_ok=False)
        self.session = RecordingSession(
            session_id=session_id,
            path=session_path,
            format_version=RECORDING_FORMAT_VERSION,
            created_at=wall_clock(),
            runtime_instance_id=runtime_instance_id,
            runtime_id=runtime_id,
            runtime_generation=runtime_generation,
            worker_generation=worker_generation,
            worker_mode=str(worker_mode),
        )
        self._capture_source = capture_metadata
        self._calibration = calibration_metadata
        self._perception = perception_metadata
        self._queue: queue.Queue[_WriteItem | None] = queue.Queue(
            maxsize=self.limits.queue_size
        )
        self._lock = Lock()
        self._stop = Event()
        self._accepting = True
        self._graceful = False
        self._status = RecordingStatus.INTERRUPTED
        self._failure: str | None = None
        self._ended_at: str | None = None
        self._event_sequence = 0
        self._last_event_relative = 0.0
        self._accepted_frames = 0
        self._accepted_frame_ids: set[str] = set()
        self._accepted_frame_sequences: set[int] = set()
        self._buffered_frame_bytes = 0
        self._recorded_frames = 0
        self._dropped_frames = 0
        self._events_written = 0
        self._write_failures = 0
        self._bytes_written = 0
        self._last_sample_at: float | None = None
        self._write_manifest()
        self._thread = Thread(
            target=self._run,
            name=f"recording-{session_id[:8]}-g{worker_generation}",
            daemon=True,
        )
        self._thread.start()
        self.record_event(RecordingEventType.SESSION_STARTED)

    @property
    def path(self) -> Path:
        return self.session.path

    def _manifest(self) -> dict[str, Any]:
        with self._lock:
            return {
                "format_version": self.session.format_version,
                "session_id": self.session.session_id,
                "created_at": self.session.created_at,
                "started_at": self.session.created_at,
                "ended_at": self._ended_at,
                "completion_status": self._status,
                "failure": self._failure,
                "runtime_instance_id": self.session.runtime_instance_id,
                "runtime_id": self.session.runtime_id,
                "runtime_generation": self.session.runtime_generation,
                "worker_generation": self.session.worker_generation,
                "worker_mode": self.session.worker_mode,
                "capture_source": self._capture_source,
                "calibration": self._calibration,
                "perception": self._perception,
                "storage": {
                    "frame_format": "PNG",
                    "events_file": "events.jsonl",
                    "frames_directory": "frames",
                    "max_duration_seconds": self.limits.max_duration_seconds,
                    "max_frames": self.limits.max_frames,
                    "max_bytes": self.limits.max_bytes,
                },
                "counters": {
                    "recorded_frames": self._recorded_frames,
                    "dropped_recording_frames": self._dropped_frames,
                    "events_written": self._events_written,
                    "write_failures": self._write_failures,
                    "storage_bytes": self._bytes_written,
                },
            }

    def _write_manifest(self) -> None:
        _atomic_json(self.path / "manifest.json", self._manifest())

    def _relative(self) -> float:
        return max(0.0, self._clock() - self._started)

    def _new_event(
        self,
        event_type: RecordingEventType,
        *,
        frame_id: str | None = None,
        observation_id: str | None = None,
        action_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> RecordingEvent:
        normalized = _json_value(dict(payload or {}))
        event = RecordingEvent(
            event_type,
            0,
            self._wall_clock(),
            self._relative(),
            self.session.runtime_generation,
            self.session.worker_generation,
            frame_id,
            observation_id,
            action_id,
            normalized,
        )
        _encoded_json(event.as_dict())
        return event

    def _sequence_event(self, event: RecordingEvent) -> RecordingEvent:
        with self._lock:
            self._event_sequence += 1
            sequence = self._event_sequence
            relative = max(self._last_event_relative, event.relative_monotonic)
            self._last_event_relative = relative
        return replace(event, sequence=sequence, relative_monotonic=relative)

    def _put(self, item: _WriteItem, *, frame: bool = False) -> bool:
        with self._lock:
            if not self._accepting:
                if frame:
                    self._dropped_frames += 1
                return False
            if self._clock() - self._started > self.limits.max_duration_seconds:
                self._truncate_locked("max session duration reached")
                if frame:
                    self._dropped_frames += 1
                return False
            try:
                self._queue.put_nowait(item)
                return True
            except queue.Full:
                if frame:
                    self._dropped_frames += 1
                return False

    def record_frame(self, frame: Frame) -> bool:
        if (
            frame.runtime_id != self.session.runtime_id
            or frame.runtime_generation != self.session.runtime_generation
            or frame.worker_generation != self.session.worker_generation
        ):
            return False
        now = self._clock()
        with self._lock:
            if not self._accepting:
                self._dropped_frames += 1
                return False
            if now - self._started > self.limits.max_duration_seconds:
                self._truncate_locked("max session duration reached")
                self._dropped_frames += 1
                return False
            if self._accepted_frames >= self.limits.max_frames:
                self._truncate_locked("max frame count reached")
                self._dropped_frames += 1
                return False
            if (
                frame.frame_id in self._accepted_frame_ids
                or frame.sequence in self._accepted_frame_sequences
            ):
                self._dropped_frames += 1
                return False
            if (
                self._last_sample_at is not None
                and now - self._last_sample_at < self.limits.frame_interval_seconds
            ):
                self._dropped_frames += 1
                return False
            if (
                self._buffered_frame_bytes + len(frame.pixels)
                > MAX_RECORDING_BUFFER_BYTES
            ):
                self._dropped_frames += 1
                return False
            self._accepted_frames += 1
            self._accepted_frame_ids.add(frame.frame_id)
            self._accepted_frame_sequences.add(frame.sequence)
            self._buffered_frame_bytes += len(frame.pixels)
            self._last_sample_at = now
        accepted = self._put(_WriteItem("frame", frame), frame=True)
        if not accepted:
            with self._lock:
                self._accepted_frames -= 1
                self._accepted_frame_ids.discard(frame.frame_id)
                self._accepted_frame_sequences.discard(frame.sequence)
                self._buffered_frame_bytes -= len(frame.pixels)
        return accepted

    def record_event(
        self,
        event_type: RecordingEventType | str,
        *,
        frame_id: str | None = None,
        observation_id: str | None = None,
        action_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> bool:
        try:
            event_kind = RecordingEventType(event_type)
            if frame_id is not None and event_kind != RecordingEventType.FRAME_CAPTURED:
                with self._lock:
                    if frame_id not in self._accepted_frame_ids:
                        return False
            event = self._new_event(
                event_kind,
                frame_id=frame_id,
                observation_id=observation_id,
                action_id=action_id,
                payload=payload,
            )
        except (TypeError, ValueError):
            return False
        return self._put(_WriteItem("event", event))

    def record_observation(self, observation: GameObservation) -> bool:
        if (
            observation.runtime_generation != self.session.runtime_generation
            or observation.worker_generation != self.session.worker_generation
        ):
            return False
        observation_id = f"observation-{observation.observation_generation}"
        return self.record_event(
            RecordingEventType.OBSERVATION_PRODUCED,
            frame_id=observation.source_frame_id,
            observation_id=observation_id,
            payload={"observation": asdict(observation), "origin": "recorded"},
        )

    def record_proposal(
        self, proposal: ActionProposal, *, frame_id: str, observation_id: str
    ) -> bool:
        return self.record_event(
            RecordingEventType.PLANNER_PROPOSAL,
            frame_id=frame_id,
            observation_id=observation_id,
            payload={"proposal": asdict(proposal)},
        )

    def record_action(self, action: Action, result: ActionResult) -> bool:
        if (
            action.runtime_generation != self.session.runtime_generation
            or action.worker_generation != self.session.worker_generation
            or result.runtime_generation != self.session.runtime_generation
            or result.worker_generation != self.session.worker_generation
        ):
            return False
        return self.record_event(
            RecordingEventType.ACTION_RESULT,
            action_id=action.action_id,
            payload={"action": asdict(action), "result": asdict(result)},
        )

    def record_action_result(
        self,
        result: ActionResult,
        *,
        frame_id: str | None = None,
        observation_id: str | None = None,
        proposal: ActionProposal | None = None,
    ) -> bool:
        if (
            result.runtime_generation != self.session.runtime_generation
            or result.worker_generation != self.session.worker_generation
        ):
            return False
        payload: dict[str, Any] = {"result": asdict(result)}
        if proposal is not None:
            payload["proposal"] = asdict(proposal)
        return self.record_event(
            RecordingEventType.ACTION_RESULT,
            frame_id=frame_id,
            observation_id=observation_id,
            action_id=result.action_id,
            payload=payload,
        )

    def marker(self, label: str, note: str | None = None) -> bool:
        if not label or len(label) > 128 or (note is not None and len(note) > 1024):
            return False
        return self.record_event(
            RecordingEventType.USER_MARKER, payload={"label": label, "note": note}
        )

    def _truncate_locked(self, reason: str) -> None:
        self._accepting = False
        self._status = RecordingStatus.TRUNCATED
        self._failure = reason

    def _fail(self, exc: BaseException) -> None:
        with self._lock:
            self._accepting = False
            self._status = RecordingStatus.FAILED
            self._failure = type(exc).__name__
            self._write_failures += 1
        self._stop.set()

    def _write_event(self, handle, event: RecordingEvent) -> None:
        event = self._sequence_event(event)
        encoded = _encoded_json(event.as_dict()) + b"\n"
        with self._lock:
            if self._bytes_written + len(encoded) > self.limits.max_bytes:
                self._truncate_locked("max storage bytes reached")
                self._stop.set()
                return
        handle.write(encoded)
        with self._lock:
            self._events_written += 1
            self._bytes_written += len(encoded)

    def _write_frame(self, handle, frame: Frame) -> None:
        image_buffer = io.BytesIO()
        frame.image().save(image_buffer, format="PNG", optimize=True)
        encoded = image_buffer.getvalue()
        relative_path = f"frames/{frame.sequence:08d}.png"
        final_path = self.path / Path(relative_path)
        event = self._new_event(
            RecordingEventType.FRAME_CAPTURED,
            frame_id=frame.frame_id,
            payload={
                "frame_sequence": frame.sequence,
                "frame_id": frame.frame_id,
                "captured_at": frame.captured_at,
                "captured_monotonic": frame.captured_monotonic,
                "relative_monotonic": max(
                    0.0, frame.captured_monotonic - self._started
                ),
                "runtime_id": frame.runtime_id,
                "runtime_generation": frame.runtime_generation,
                "worker_generation": frame.worker_generation,
                "width": frame.width,
                "height": frame.height,
                "coordinate_space": frame.coordinate_space,
                "source": frame.source,
                "metadata": frame.metadata,
                "image_file": relative_path,
                "image_bytes": len(encoded),
            },
        )
        event = self._sequence_event(event)
        event_bytes = _encoded_json(event.as_dict()) + b"\n"
        with self._lock:
            if (
                self._bytes_written + len(encoded) + len(event_bytes)
                > self.limits.max_bytes
            ):
                self._truncate_locked("max storage bytes reached")
                self._dropped_frames += 1
                self._stop.set()
                return
        temporary = final_path.with_suffix(".png.tmp")
        try:
            with temporary.open("xb") as image_handle:
                image_handle.write(encoded)
                image_handle.flush()
            os.replace(temporary, final_path)
        finally:
            temporary.unlink(missing_ok=True)
        handle.write(event_bytes)
        with self._lock:
            self._recorded_frames += 1
            self._events_written += 1
            self._bytes_written += len(encoded) + len(event_bytes)

    def _discard_queue(self) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            if item is not None and item.kind == "frame":
                with self._lock:
                    self._dropped_frames += 1
                    self._buffered_frame_bytes -= len(item.value.pixels)
            self._queue.task_done()

    def _run(self) -> None:
        events_path = self.path / "events.jsonl"
        try:
            with events_path.open("ab") as handle:
                while True:
                    if self._stop.is_set():
                        self._discard_queue()
                        break
                    try:
                        item = self._queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    try:
                        if item is None:
                            break
                        if item.kind == "frame":
                            try:
                                self._write_frame(handle, item.value)
                            finally:
                                with self._lock:
                                    self._buffered_frame_bytes -= len(item.value.pixels)
                        else:
                            self._write_event(handle, item.value)
                    finally:
                        self._queue.task_done()
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as exc:  # noqa: BLE001 - disk boundary must isolate worker
            self._fail(exc)
            self._discard_queue()
        finally:
            with self._lock:
                if self._graceful and self._status == RecordingStatus.INTERRUPTED:
                    self._status = RecordingStatus.COMPLETE
                self._ended_at = self._wall_clock()
            try:
                self._write_manifest()
            except BaseException as exc:  # noqa: BLE001 - preserve incomplete manifest
                self._fail(exc)

    def close(self, *, timeout: float | None = None) -> bool:
        deadline = time.monotonic() + (
            self.limits.shutdown_timeout_seconds
            if timeout is None
            else max(0.0, timeout)
        )
        with self._lock:
            if not self._accepting and not self._thread.is_alive():
                return self._status in {
                    RecordingStatus.COMPLETE,
                    RecordingStatus.TRUNCATED,
                }
            self._accepting = False
            self._graceful = True
        while self._thread.is_alive():
            try:
                self._queue.put(
                    None, timeout=max(0.0, min(0.05, deadline - time.monotonic()))
                )
                break
            except queue.Full:
                if time.monotonic() >= deadline:
                    break
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        if self._thread.is_alive():
            with self._lock:
                self._graceful = False
                self._status = RecordingStatus.INTERRUPTED
                self._failure = "recorder shutdown timeout"
            self._stop.set()
            return False
        # The writer may fail between the liveness check and enqueueing the
        # shutdown sentinel. Drain that otherwise orphaned queue item.
        self._discard_queue()
        with self._lock:
            return self._status in {
                RecordingStatus.COMPLETE,
                RecordingStatus.TRUNCATED,
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "session_id": self.session.session_id,
                "state": "RECORDING" if self._accepting else self._status,
                "completion_status": self._status,
                "accepting": self._accepting,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "buffered_frame_bytes": self._buffered_frame_bytes,
                "writer_alive": self._thread.is_alive(),
                "recorded_frames": self._recorded_frames,
                "dropped_recording_frames": self._dropped_frames,
                "events_written": self._events_written,
                "write_failures": self._write_failures,
                "storage_bytes": self._bytes_written,
                "failure": self._failure,
            }
