from __future__ import annotations

import json
import math
import re
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from threading import Condition, Event, Lock
from typing import Any

from runtime_agent.gameworker.actions import (
    Action,
    ActionName,
    ActionResult,
    ActionStatus,
)
from runtime_agent.gameworker.capture import CaptureError, CaptureFailure, Frame
from runtime_agent.gameworker.geometry import CalibrationProfile, CoordinateSpace
from runtime_agent.gameworker.perception import ObservePipeline, PipelineOutcome
from runtime_agent.gameworker.recording import (
    MAX_EVENT_BYTES,
    MAX_MANIFEST_BYTES,
    RECORDING_FORMAT_VERSION,
    RecordingEventType,
    RecordingStatus,
)

MAX_REPLAY_EVENTS = 1_000_000
MAX_REPLAY_FRAMES = 100_000
MAX_REPLAY_IMAGE_BYTES = 64 * 1024 * 1024


class ReplayError(ValueError):
    code = "WORKER_REPLAY_INVALID"


class ReplayTimingMode(StrEnum):
    AS_FAST_AS_POSSIBLE = "AS_FAST_AS_POSSIBLE"
    RECORDED_TIMING = "RECORDED_TIMING"
    STEP = "STEP"


class ReplayClock:
    """Recorded monotonic timeline with cancellable real-time and step policies."""

    def __init__(
        self,
        mode: ReplayTimingMode = ReplayTimingMode.AS_FAST_AS_POSSIBLE,
        *,
        started_at: str | None = None,
        speed: float = 1.0,
        sleeper: Callable[[float], None] | None = None,
    ):
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("replay speed must be positive")
        self.mode = ReplayTimingMode(mode)
        self.speed = speed
        self._current = -1e-9
        self._cancelled = Event()
        self._condition = Condition()
        self._steps = 0
        self._sleeper = sleeper
        try:
            self._started_at = (
                datetime.fromisoformat(started_at)
                if started_at
                else datetime(1970, 1, 1, tzinfo=timezone.utc)
            )
        except (TypeError, ValueError) as exc:
            raise ReplayError("recording start timestamp is invalid") from exc
        if self._started_at.tzinfo is None:
            self._started_at = self._started_at.replace(tzinfo=timezone.utc)

    def __call__(self) -> float:
        with self._condition:
            return self._current

    def now_iso(self) -> str:
        with self._condition:
            value = max(0.0, self._current)
        return (self._started_at + timedelta(seconds=value)).isoformat()

    def advance(self, target: float) -> None:
        if not math.isfinite(target) or target < 0:
            raise ReplayError("recorded replay time is invalid")
        with self._condition:
            previous = self._current
            if target < previous:
                raise ReplayError("recorded replay time moved backwards")
        delay = max(0.0, target - max(0.0, previous)) / self.speed
        if self.mode == ReplayTimingMode.RECORDED_TIMING and delay:
            if self._sleeper is not None:
                self._sleeper(delay)
            elif self._cancelled.wait(delay):
                raise CaptureError("replay cancelled", failure=CaptureFailure.CLOSED)
        elif self.mode == ReplayTimingMode.STEP:
            with self._condition:
                while self._steps < 1 and not self._cancelled.is_set():
                    self._condition.wait(timeout=0.1)
                if self._cancelled.is_set():
                    raise CaptureError(
                        "replay cancelled", failure=CaptureFailure.CLOSED
                    )
                self._steps -= 1
        if self._cancelled.is_set():
            raise CaptureError("replay cancelled", failure=CaptureFailure.CLOSED)
        with self._condition:
            self._current = target

    def step(self, count: int = 1) -> None:
        if count < 1:
            raise ValueError("step count must be positive")
        with self._condition:
            self._steps += count
            self._condition.notify_all()

    def cancel(self) -> None:
        self._cancelled.set()
        with self._condition:
            self._condition.notify_all()


@dataclass(frozen=True, slots=True)
class _FrameRecord:
    event_sequence: int
    frame_id: str
    frame_sequence: int
    captured_at: str
    recorded_monotonic: float
    relative_monotonic: float
    runtime_id: int
    runtime_generation: int
    worker_generation: int
    width: int
    height: int
    coordinate_space: CoordinateSpace
    source: str
    metadata: tuple[tuple[str, bool | int | float | str | None], ...]
    image_path: Path
    image_bytes: int
    lifecycle_before: tuple[RecordingEventType, ...]


def _contained(root: Path, reference: str) -> Path:
    if not isinstance(reference, str) or not reference or "\\" in reference:
        raise ReplayError("recording contains an invalid file reference")
    candidate = (root / reference).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ReplayError("recording file reference escapes session root") from exc
    return candidate


def _read_json(path: Path, limit: int) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            raise ReplayError(f"missing or oversized {path.name}")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"malformed {path.name}") from exc
    if not isinstance(value, dict):
        raise ReplayError(f"{path.name} must contain an object")
    return value


class ReplayCaptureSource:
    """Strict recording loader implementing the ordinary CaptureSource contract."""

    def __init__(
        self,
        session_path: Path,
        *,
        runtime_generation: int | None = None,
        worker_generation: int | None = None,
        runtime_id: int | None = None,
        timing_mode: ReplayTimingMode = ReplayTimingMode.AS_FAST_AS_POSSIBLE,
        allow_incomplete: bool = True,
        clock: ReplayClock | None = None,
    ):
        self.session_path = Path(session_path).resolve()
        if not self.session_path.is_dir():
            raise ReplayError("replay session directory does not exist")
        self.manifest = _read_json(
            self.session_path / "manifest.json", MAX_MANIFEST_BYTES
        )
        self._validate_manifest(allow_incomplete)
        recorded_runtime = self._positive_int("runtime_generation")
        recorded_worker = self._positive_int("worker_generation")
        recorded_runtime_id = self._positive_int("runtime_id")
        self.runtime_generation = runtime_generation or recorded_runtime
        self.worker_generation = worker_generation or recorded_worker
        self.runtime_id = runtime_id or recorded_runtime_id
        if min(self.runtime_id, self.runtime_generation, self.worker_generation) < 1:
            raise ReplayError("replay generation identity is invalid")
        self.clock = clock or ReplayClock(
            timing_mode, started_at=self.manifest.get("started_at")
        )
        self.incomplete = self.manifest["completion_status"] != RecordingStatus.COMPLETE
        self._events_path = _contained(self.session_path, "events.jsonl")
        self._recorded_observation_offsets: dict[str, int] = {}
        self._records = self._validate_events()
        self._index = 0
        self._closed = False
        self._lock = Lock()

    def _positive_int(self, name: str) -> int:
        value = self.manifest.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ReplayError(f"manifest {name} is invalid")
        return value

    def _validate_manifest(self, allow_incomplete: bool) -> None:
        if self.manifest.get("format_version") != RECORDING_FORMAT_VERSION:
            raise ReplayError("unsupported recording format version")
        session_id = self.manifest.get("session_id")
        if (
            not isinstance(session_id, str)
            or not re.fullmatch(r"[a-f0-9]{32}", session_id)
            or session_id != self.session_path.name
        ):
            raise ReplayError("manifest session identity does not match directory")
        try:
            status = RecordingStatus(self.manifest.get("completion_status"))
        except (TypeError, ValueError) as exc:
            raise ReplayError("manifest completion status is invalid") from exc
        if status != RecordingStatus.COMPLETE and not allow_incomplete:
            raise ReplayError("recording is incomplete")
        for name in ("created_at", "started_at"):
            timestamp = self.manifest.get(name)
            try:
                parsed = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError) as exc:
                raise ReplayError(f"manifest {name} is invalid") from exc
            if parsed.tzinfo is None:
                raise ReplayError(f"manifest {name} must include a timezone")
        ended_at = self.manifest.get("ended_at")
        if status == RecordingStatus.COMPLETE and not isinstance(ended_at, str):
            raise ReplayError("complete recording has no end timestamp")
        if ended_at is not None:
            try:
                ended = datetime.fromisoformat(ended_at)
            except (TypeError, ValueError) as exc:
                raise ReplayError("manifest end timestamp is invalid") from exc
            if ended.tzinfo is None:
                raise ReplayError("manifest end timestamp must include a timezone")
        runtime_instance_id = self.manifest.get("runtime_instance_id")
        if not isinstance(runtime_instance_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,128}", runtime_instance_id
        ):
            raise ReplayError("manifest runtime instance ID is invalid")
        if self.manifest.get("worker_mode") not in {"OBSERVE", "ACTIVE"}:
            raise ReplayError("manifest worker mode is invalid")
        storage = self.manifest.get("storage")
        if not isinstance(storage, dict):
            raise ReplayError("manifest storage contract is missing")
        if storage.get("frame_format") != "PNG":
            raise ReplayError("unsupported recording frame format")
        if storage.get("events_file") != "events.jsonl":
            raise ReplayError("unsupported recording event layout")
        if storage.get("frames_directory") != "frames":
            raise ReplayError("unsupported recording frame layout")
        duration = storage.get("max_duration_seconds")
        max_frames = storage.get("max_frames")
        max_bytes = storage.get("max_bytes")
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(duration)
            or not 1 <= duration <= 24 * 60 * 60
            or not isinstance(max_frames, int)
            or isinstance(max_frames, bool)
            or not 1 <= max_frames <= MAX_REPLAY_FRAMES
            or not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or not 1024 * 1024 <= max_bytes <= 100 * 1024 * 1024 * 1024
        ):
            raise ReplayError("manifest storage limits are invalid")
        for name in ("runtime_id", "runtime_generation", "worker_generation"):
            self._positive_int(name)
        for name in ("capture_source", "calibration", "perception"):
            if not isinstance(self.manifest.get(name), dict):
                raise ReplayError(f"manifest {name} metadata is missing")
        if (
            "kind" not in self.manifest["capture_source"]
            or not {"profile_id", "version"} <= self.manifest["calibration"].keys()
            or "engine" not in self.manifest["perception"]
        ):
            raise ReplayError("manifest provenance metadata is incomplete")

    @staticmethod
    def _event_int(event: dict[str, Any], name: str, minimum: int = 0) -> int:
        value = event.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
            raise ReplayError(f"event {name} is invalid")
        return value

    @staticmethod
    def _event_float(value: Any, name: str) -> float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ReplayError(f"event {name} is invalid")
        result = float(value)
        if not math.isfinite(result) or result < 0:
            raise ReplayError(f"event {name} is invalid")
        return result

    def _frame_record(
        self,
        event: dict[str, Any],
        lifecycle: tuple[RecordingEventType, ...],
    ) -> _FrameRecord:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ReplayError("frame event payload is invalid")
        frame_id = payload.get("frame_id")
        if not isinstance(frame_id, str) or not frame_id or len(frame_id) > 128:
            raise ReplayError("frame ID is invalid")
        width = self._event_int(payload, "width", 1)
        height = self._event_int(payload, "height", 1)
        if width > 3840 or height > 2160 or width * height * 3 > 3840 * 2160 * 3:
            raise ReplayError("frame dimensions exceed replay bounds")
        image_bytes = self._event_int(payload, "image_bytes", 1)
        if image_bytes > MAX_REPLAY_IMAGE_BYTES:
            raise ReplayError("frame image exceeds replay size bound")
        image_path = _contained(self.session_path, payload.get("image_file"))
        if (
            image_path.parent != (self.session_path / "frames").resolve()
            or image_path.suffix.lower() != ".png"
            or not image_path.is_file()
            or image_path.stat().st_size != image_bytes
        ):
            raise ReplayError("frame image reference is missing or inconsistent")
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                if image.format != "PNG" or image.size != (width, height):
                    raise ReplayError(
                        "recorded frame image metadata does not match payload"
                    )
                image.verify()
        except (OSError, ValueError) as exc:
            raise ReplayError("recorded frame image is invalid") from exc
        metadata_value = payload.get("metadata", [])
        if not isinstance(metadata_value, list) or len(metadata_value) > 32:
            raise ReplayError("frame metadata is invalid")
        metadata: list[tuple[str, bool | int | float | str | None]] = []
        for item in metadata_value:
            if (
                not isinstance(item, list)
                or len(item) != 2
                or not isinstance(item[0], str)
            ):
                raise ReplayError("frame metadata is invalid")
            if not isinstance(item[1], (bool, int, float, str, type(None))):
                raise ReplayError("frame metadata is invalid")
            metadata.append((item[0], item[1]))
        try:
            coordinate_space = CoordinateSpace(payload.get("coordinate_space"))
        except (TypeError, ValueError) as exc:
            raise ReplayError("frame coordinate space is invalid") from exc
        captured_at = payload.get("captured_at")
        source = payload.get("source")
        if not isinstance(captured_at, str) or not captured_at or len(captured_at) > 64:
            raise ReplayError("frame wall timestamp is invalid")
        try:
            captured_timestamp = datetime.fromisoformat(captured_at)
        except ValueError as exc:
            raise ReplayError("frame wall timestamp is invalid") from exc
        if captured_timestamp.tzinfo is None:
            raise ReplayError("frame wall timestamp must include a timezone")
        if not isinstance(source, str) or not source or len(source) > 128:
            raise ReplayError("frame source is invalid")
        return _FrameRecord(
            event_sequence=self._event_int(event, "sequence", 1),
            frame_id=frame_id,
            frame_sequence=self._event_int(payload, "frame_sequence", 1),
            captured_at=captured_at,
            recorded_monotonic=self._event_float(
                payload.get("captured_monotonic"), "captured_monotonic"
            ),
            relative_monotonic=self._event_float(
                payload.get("relative_monotonic"), "relative_monotonic"
            ),
            runtime_id=self._event_int(payload, "runtime_id", 1),
            runtime_generation=self._event_int(payload, "runtime_generation", 1),
            worker_generation=self._event_int(payload, "worker_generation", 1),
            width=width,
            height=height,
            coordinate_space=coordinate_space,
            source=source,
            metadata=tuple(metadata),
            image_path=image_path,
            image_bytes=image_bytes,
            lifecycle_before=lifecycle,
        )

    def _validate_events(self) -> tuple[_FrameRecord, ...]:
        path = self._events_path
        if not path.is_file():
            raise ReplayError("recording event stream is missing")
        storage_limit = self.manifest["storage"]["max_bytes"]
        if path.stat().st_size > storage_limit:
            raise ReplayError("recording event stream exceeds storage limit")
        records: list[_FrameRecord] = []
        frame_ids: set[str] = set()
        frame_sequences: set[int] = set()
        pending_lifecycle: list[RecordingEventType] = []
        expected_event = 1
        last_relative = -1.0
        last_frame_relative = -1.0
        recorded_runtime = self._positive_int("runtime_generation")
        recorded_worker = self._positive_int("worker_generation")
        offset = 0
        try:
            with path.open("rb") as handle:
                for raw in handle:
                    event_offset = offset
                    offset += len(raw)
                    if expected_event > MAX_REPLAY_EVENTS:
                        raise ReplayError("recording event count exceeds replay bound")
                    if len(raw) > MAX_EVENT_BYTES + 1:
                        raise ReplayError("recording event line exceeds size bound")
                    try:
                        event = json.loads(raw)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        if (
                            self.incomplete
                            and not raw.endswith(b"\n")
                            and not handle.peek(1)
                        ):
                            break
                        raise ReplayError(
                            "recording contains a malformed event"
                        ) from exc
                    if not isinstance(event, dict):
                        raise ReplayError("recording event must be an object")
                    sequence = self._event_int(event, "sequence", 1)
                    if sequence != expected_event:
                        raise ReplayError(
                            "recording event sequence is duplicate or discontinuous"
                        )
                    expected_event += 1
                    relative = self._event_float(
                        event.get("relative_monotonic"), "relative_monotonic"
                    )
                    if relative < last_relative:
                        raise ReplayError("recording event time moved backwards")
                    last_relative = relative
                    if (
                        self._event_int(event, "runtime_generation", 1)
                        != recorded_runtime
                        or self._event_int(event, "worker_generation", 1)
                        != recorded_worker
                    ):
                        raise ReplayError(
                            "recording mixes runtime or worker generations"
                        )
                    try:
                        kind = RecordingEventType(event.get("event_type"))
                    except (TypeError, ValueError) as exc:
                        raise ReplayError("recording event type is unknown") from exc
                    related_frame = event.get("frame_id")
                    if related_frame is not None and (
                        not isinstance(related_frame, str)
                        or not related_frame
                        or len(related_frame) > 128
                    ):
                        raise ReplayError("event frame reference is invalid")
                    for reference_name in ("observation_id", "action_id"):
                        reference = event.get(reference_name)
                        if reference is not None and (
                            not isinstance(reference, str)
                            or not reference
                            or len(reference) > 128
                        ):
                            raise ReplayError(
                                f"event {reference_name} reference is invalid"
                            )
                    if (
                        related_frame is not None
                        and kind != RecordingEventType.FRAME_CAPTURED
                        and related_frame not in frame_ids
                    ):
                        raise ReplayError("event references an unknown frame")
                    if kind in {
                        RecordingEventType.GAME_READY,
                        RecordingEventType.GAME_LOST,
                    }:
                        pending_lifecycle.append(kind)
                    if kind == RecordingEventType.OBSERVATION_PRODUCED:
                        payload = event.get("payload")
                        if (
                            not isinstance(payload, dict)
                            or payload.get("origin") != "recorded"
                            or not isinstance(payload.get("observation"), dict)
                            or not isinstance(related_frame, str)
                            or payload["observation"].get("source_frame_id")
                            != related_frame
                            or related_frame in self._recorded_observation_offsets
                        ):
                            raise ReplayError("recorded observation event is invalid")
                        self._recorded_observation_offsets[related_frame] = event_offset
                    if kind != RecordingEventType.FRAME_CAPTURED:
                        continue
                    record = self._frame_record(event, tuple(pending_lifecycle))
                    pending_lifecycle.clear()
                    if record.frame_id != related_frame:
                        raise ReplayError("frame event identity is inconsistent")
                    if (
                        record.runtime_id != self._positive_int("runtime_id")
                        or record.runtime_generation != recorded_runtime
                        or record.worker_generation != recorded_worker
                    ):
                        raise ReplayError("frame generation does not match manifest")
                    if (
                        record.frame_id in frame_ids
                        or record.frame_sequence in frame_sequences
                    ):
                        raise ReplayError("recording contains duplicate frame identity")
                    if records and record.frame_sequence <= records[-1].frame_sequence:
                        raise ReplayError("recording frame sequence is not increasing")
                    if record.relative_monotonic < last_frame_relative:
                        raise ReplayError("recording frame time moved backwards")
                    if (
                        record.relative_monotonic
                        > self.manifest["storage"]["max_duration_seconds"]
                    ):
                        raise ReplayError("recording frame exceeds session duration")
                    last_frame_relative = record.relative_monotonic
                    frame_ids.add(record.frame_id)
                    frame_sequences.add(record.frame_sequence)
                    records.append(record)
                    if len(records) > self.manifest["storage"]["max_frames"]:
                        raise ReplayError("recording exceeds its declared frame limit")
                    if len(records) > MAX_REPLAY_FRAMES:
                        raise ReplayError("recording frame count exceeds replay bound")
        except OSError as exc:
            raise ReplayError("recording event stream cannot be read") from exc
        counters = self.manifest.get("counters")
        if not isinstance(counters, dict):
            raise ReplayError("manifest counters are missing")
        if not self.incomplete and counters.get("recorded_frames") != len(records):
            raise ReplayError("manifest frame count is inconsistent")
        total_size = path.stat().st_size + sum(item.image_bytes for item in records)
        if total_size > storage_limit:
            raise ReplayError("recording exceeds its declared storage limit")
        if not self.incomplete and (
            counters.get("events_written") != expected_event - 1
            or counters.get("storage_bytes") != total_size
        ):
            raise ReplayError("manifest counters are inconsistent")
        return tuple(records)

    def recorded_observation(self, frame_id: str) -> dict[str, Any] | None:
        """Read one expected observation without retaining the event history."""
        offset = self._recorded_observation_offsets.get(frame_id)
        if offset is None:
            return None
        try:
            with self._events_path.open("rb") as handle:
                handle.seek(offset)
                event = json.loads(handle.readline(MAX_EVENT_BYTES + 2))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReplayError("recorded observation can no longer be read") from exc
        payload = event.get("payload")
        if not isinstance(payload, dict) or not isinstance(
            payload.get("observation"), dict
        ):
            raise ReplayError("recorded observation is invalid")
        return payload["observation"]

    def iter_recorded_events(
        self, event_type: RecordingEventType | None = None
    ) -> Iterator[dict[str, Any]]:
        """Stream inspectable metadata; raw frame payloads are never returned."""
        expected = RecordingEventType(event_type) if event_type is not None else None
        try:
            with self._events_path.open("rb") as handle:
                for raw in handle:
                    try:
                        event = json.loads(raw)
                    except (UnicodeError, json.JSONDecodeError) as exc:
                        if self.incomplete and not raw.endswith(b"\n"):
                            return
                        raise ReplayError(
                            "recording contains a malformed event"
                        ) from exc
                    if expected is None or event.get("event_type") == expected:
                        yield event
        except OSError as exc:
            raise ReplayError("recording event stream cannot be read") from exc

    @property
    def frame_count(self) -> int:
        return len(self._records)

    @property
    def has_next(self) -> bool:
        with self._lock:
            return not self._closed and self._index < len(self._records)

    def peek_lifecycle_events(self) -> tuple[RecordingEventType, ...]:
        with self._lock:
            if self._closed or self._index >= len(self._records):
                return ()
            return self._records[self._index].lifecycle_before

    def capture(self) -> Frame:
        with self._lock:
            if self._closed:
                raise CaptureError("replay is closed", failure=CaptureFailure.CLOSED)
            if self._index >= len(self._records):
                raise CaptureError("replay is exhausted", failure=CaptureFailure.CLOSED)
            record = self._records[self._index]
            self._index += 1
        self.clock.advance(record.relative_monotonic)
        try:
            from PIL import Image

            with Image.open(record.image_path) as image:
                if image.format != "PNG" or image.size != (record.width, record.height):
                    raise ReplayError(
                        "recorded frame image metadata does not match payload"
                    )
                pixels = image.convert("RGB").tobytes()
        except (OSError, ValueError) as exc:
            raise ReplayError("recorded frame image is invalid") from exc
        provenance = (
            ("recording_session_id", self.manifest["session_id"]),
            ("recorded_frame_source", record.source),
            ("recorded_captured_monotonic", record.recorded_monotonic),
            ("recorded_runtime_id", record.runtime_id),
            ("recorded_runtime_generation", record.runtime_generation),
            ("recorded_worker_generation", record.worker_generation),
        )
        return Frame(
            frame_id=record.frame_id,
            sequence=record.frame_sequence,
            captured_at=record.captured_at,
            captured_monotonic=record.relative_monotonic,
            runtime_id=self.runtime_id,
            runtime_generation=self.runtime_generation,
            worker_generation=self.worker_generation,
            width=record.width,
            height=record.height,
            pixels=pixels,
            coordinate_space=record.coordinate_space,
            source="replay",
            metadata=record.metadata[:26] + provenance,
        )

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.clock.cancel()


class ReplayActionSink:
    """Input-free terminal action boundary; no InputDriver is reachable."""

    def __init__(
        self, *, runtime_id: int, runtime_generation: int, worker_generation: int
    ):
        self.runtime_id = runtime_id
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self._sequence = 0
        self.suppressed_total = 0
        self.last_result: ActionResult | None = None

    def execute(
        self, action: ActionName, *, duration: float | None = None
    ) -> ActionResult:
        self._sequence += 1
        return self._suppress(
            action_id=f"replay-action-{self._sequence}",
            action=action,
            duration=0.0 if duration is None else duration,
        )

    def execute_action(self, action: Action) -> ActionResult:
        if (
            action.runtime_id not in {0, self.runtime_id}
            or action.runtime_generation != self.runtime_generation
            or action.worker_generation != self.worker_generation
        ):
            return ActionResult(
                action.action_id,
                action.name,
                ActionStatus.STALE_GENERATION,
                0.0,
                self.runtime_generation,
                self.worker_generation,
                runtime_id=self.runtime_id,
                reason="action does not belong to this replay generation",
            )
        return self._suppress(
            action_id=action.action_id,
            action=action.name,
            duration=0.0 if action.duration is None else action.duration,
        )

    def _suppress(
        self, *, action_id: str, action: ActionName, duration: float
    ) -> ActionResult:
        result = ActionResult(
            action_id=action_id,
            action=action,
            status=ActionStatus.SUPPRESSED,
            duration=duration,
            runtime_generation=self.runtime_generation,
            worker_generation=self.worker_generation,
            runtime_id=self.runtime_id,
            reason="REPLAY mode permanently suppresses gameplay input",
        )
        self.suppressed_total += 1
        self.last_result = result
        return result


class ReplayRunner:
    """Step/run facade over the ordinary Stage 3 ObservePipeline."""

    def __init__(
        self,
        source: ReplayCaptureSource,
        engine,
        planner,
        calibration: CalibrationProfile,
        *,
        max_frame_age: float,
        max_observation_age: float,
        perception_timeout: float,
        planner_timeout: float,
        assume_game_ready: bool = True,
    ):
        self.source = source
        self.actions = ReplayActionSink(
            runtime_id=source.runtime_id,
            runtime_generation=source.runtime_generation,
            worker_generation=source.worker_generation,
        )
        self.pipeline = ObservePipeline(
            source,
            engine,
            planner,
            self.actions,
            calibration,
            runtime_generation=source.runtime_generation,
            worker_generation=source.worker_generation,
            max_frame_age=max_frame_age,
            max_observation_age=max_observation_age,
            perception_timeout=perception_timeout,
            planner_timeout=planner_timeout,
            clock=source.clock,
        )
        self._closed = False
        self.frames_processed = 0
        self._event_sequence = 0
        self._finished = False
        self._events: deque[dict[str, Any]] = deque(maxlen=32)
        self._emit("REPLAY_STARTED")
        if assume_game_ready:
            self.pipeline.on_game_ready()

    def _emit(
        self,
        event_type: str,
        *,
        frame_id: str | None = None,
        status: str | None = None,
    ) -> None:
        self._event_sequence += 1
        self._events.append(
            {
                "event_type": event_type,
                "sequence": self._event_sequence,
                "timestamp": self.source.clock.now_iso(),
                "relative_monotonic": max(0.0, self.source.clock()),
                "frame_id": frame_id,
                "status": status,
            }
        )

    @property
    def lifecycle_events(self) -> list[dict[str, Any]]:
        return list(self._events)

    def step(self) -> PipelineOutcome | None:
        if self._closed or not self.source.has_next:
            if not self._finished:
                self._finished = True
                self._emit("REPLAY_FINISHED", status="EXHAUSTED")
            return None
        try:
            for event in self.source.peek_lifecycle_events():
                if event == RecordingEventType.GAME_READY:
                    self.pipeline.on_game_ready()
                elif event == RecordingEventType.GAME_LOST:
                    self.pipeline.on_game_lost()
            outcome = self.pipeline.tick()
        except Exception:
            self._emit("REPLAY_FAILED", status="FAILED")
            raise
        self.frames_processed += int(outcome.frame_id is not None)
        self._emit(
            "REPLAY_FRAME_PROCESSED",
            frame_id=outcome.frame_id,
            status=outcome.status,
        )
        return outcome

    def outcomes(self) -> Iterator[PipelineOutcome]:
        while not self._closed and self.source.has_next:
            outcome = self.step()
            if outcome is not None:
                yield outcome
        if not self._closed and not self._finished:
            self.step()

    def run(self, *, max_results: int = MAX_REPLAY_FRAMES) -> list[PipelineOutcome]:
        if not 1 <= max_results <= MAX_REPLAY_FRAMES:
            raise ValueError("replay result bound is invalid")
        results: list[PipelineOutcome] = []
        for outcome in self.outcomes():
            if len(results) >= max_results:
                raise ReplayError("replay result count exceeds requested bound")
            results.append(outcome)
        return results

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._finished:
            self._finished = True
            self._emit("REPLAY_FINISHED", status="CANCELLED")
        self.pipeline.close()
