from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

WORKER_CONFIG_SCHEMA_VERSION = 1
DEFAULT_ASSETS_MANIFEST = Path(__file__).parent / "dst" / "assets" / "manifest.json"


def _is_absolute_path(value: Path) -> bool:
    return value.is_absolute() or PurePosixPath(value.as_posix()).is_absolute()


def _strict_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


class WorkerMode(StrEnum):
    DISABLED = "DISABLED"
    # NOOP is the canonical behavior name; DISABLED remains the wire value for
    # backward compatibility with the control plane and existing runtimes.
    NOOP = "DISABLED"
    OBSERVE = "OBSERVE"
    ACTIVE = "ACTIVE"
    REPLAY = "REPLAY"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str) and value.upper() == "NOOP":
            return cls.DISABLED
        return None


@dataclass(frozen=True, slots=True)
class InputBindings:
    move_up: str = "w"
    move_down: str = "s"
    move_left: str = "a"
    move_right: str = "d"
    interact: str = "space"
    inventory: str = "tab"
    cancel: str = "Escape"


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    schema_version: int = WORKER_CONFIG_SCHEMA_VERSION
    plugin: str = "noop"
    plugin_version: str = "0.1.0"
    profile: str = "dst-default-v1"
    profile_version: int = 1
    mode: WorkerMode = WorkerMode.DISABLED
    autostart: bool = False
    tick_interval: float = 1.0
    observation_interval: float = 2.0
    action_timeout: float = 1.0
    recovery_attempts: int = 3
    vision_threshold: float = 0.80
    max_actions_per_second: float = 2.0
    max_key_presses_per_second: float = 6.0
    action_queue_size: int = 16
    input_subprocess_timeout: float = 2.0
    deadman_timeout: float = 5.0
    capture_backend: str = "x11"
    capture_timeout: float = 2.0
    max_frame_age: float = 3.0
    perception_timeout: float = 1.0
    max_observation_age: float = 3.0
    planner_timeout: float = 0.5
    calibration_verified: bool = False
    capture_max_width: int = 1280
    capture_max_height: int = 720
    diagnostic_capture: bool = True
    diagnostic_directory: Path = Path(
        "/home/dst/.local/state/dst-runtime/worker-diagnostics"
    )
    diagnostic_ring_size: int = 10
    diagnostic_max_bytes: int = 25 * 1024 * 1024
    recording_enabled: bool = False
    recording_root: Path = Path("/home/dst/.local/state/dst-runtime/worker-recordings")
    recording_max_duration: float = 15 * 60
    recording_max_frames: int = 1800
    recording_max_bytes: int = 512 * 1024 * 1024
    recording_queue_size: int = 8
    recording_frame_interval: float = 0.5
    recording_shutdown_timeout: float = 5.0
    replay_session_path: Path | None = None
    replay_timing_mode: str = "AS_FAST_AS_POSSIBLE"
    assets_manifest: Path = DEFAULT_ASSETS_MANIFEST
    bindings: InputBindings = field(default_factory=InputBindings)

    def validate(self) -> None:
        if self.schema_version != WORKER_CONFIG_SCHEMA_VERSION:
            raise ValueError("WORKER_CONFIG_VERSION_MISMATCH")
        if self.plugin not in {"noop", "dst"}:
            raise ValueError("unsupported worker plugin")
        if self.plugin == "noop" and self.mode != WorkerMode.DISABLED:
            raise ValueError("noop worker supports only DISABLED/NOOP mode")
        if self.tick_interval <= 0 or self.observation_interval <= 0:
            raise ValueError("worker intervals must be positive")
        if self.tick_interval > 60 or self.observation_interval > 60:
            raise ValueError("worker intervals must not exceed 60 seconds")
        if not 0.1 <= self.action_timeout <= 5.0:
            raise ValueError("action_timeout must be between 0.1 and 5 seconds")
        if self.recovery_attempts < 0 or self.recovery_attempts > 10:
            raise ValueError("recovery_attempts must be between 0 and 10")
        if not 0.0 <= self.vision_threshold <= 1.0:
            raise ValueError("vision_threshold must be between 0 and 1")
        if not 0.1 <= self.max_actions_per_second <= 20:
            raise ValueError("max_actions_per_second must be between 0.1 and 20")
        if not 0.1 <= self.max_key_presses_per_second <= 50:
            raise ValueError("max_key_presses_per_second must be between 0.1 and 50")
        if not 1 <= self.action_queue_size <= 1024:
            raise ValueError("action_queue_size must be between 1 and 1024")
        if not 0.1 <= self.input_subprocess_timeout <= 10:
            raise ValueError(
                "input_subprocess_timeout must be between 0.1 and 10 seconds"
            )
        if self.deadman_timeout <= self.action_timeout or self.deadman_timeout > 30:
            raise ValueError(
                "deadman_timeout must exceed action_timeout and be at most 30 seconds"
            )
        if self.capture_backend != "x11":
            raise ValueError("unsupported capture backend")
        if not 0.1 <= self.capture_timeout <= 10:
            raise ValueError("capture_timeout must be between 0.1 and 10 seconds")
        if not 0.1 <= self.max_frame_age <= 60:
            raise ValueError("max_frame_age must be between 0.1 and 60 seconds")
        if not 0.05 <= self.perception_timeout <= self.max_frame_age:
            raise ValueError("perception_timeout must not exceed max_frame_age")
        if not 0.1 <= self.max_observation_age <= 60:
            raise ValueError("max_observation_age must be between 0.1 and 60 seconds")
        if not 0.05 <= self.planner_timeout <= 5:
            raise ValueError("planner_timeout must be between 0.05 and 5 seconds")
        if not isinstance(self.calibration_verified, bool):
            raise TypeError("calibration_verified must be boolean")
        if self.capture_max_width < 320 or self.capture_max_height < 240:
            raise ValueError("capture bounds are too small")
        if self.capture_max_width > 3840 or self.capture_max_height > 2160:
            raise ValueError("capture bounds exceed the worker resource limit")
        if not 1 <= self.diagnostic_ring_size <= 50:
            raise ValueError("diagnostic_ring_size must be between 1 and 50")
        if not 1024 * 1024 <= self.diagnostic_max_bytes <= 250 * 1024 * 1024:
            raise ValueError("diagnostic_max_bytes is outside the safe range")
        if not PurePosixPath(self.diagnostic_directory.as_posix()).is_absolute():
            raise ValueError("diagnostic_directory must be absolute")
        if not isinstance(self.recording_enabled, bool):
            raise TypeError("recording_enabled must be boolean")
        if not _is_absolute_path(self.recording_root):
            raise ValueError("recording_root must be absolute")
        if not 1 <= self.recording_max_duration <= 24 * 60 * 60:
            raise ValueError("recording_max_duration must be between 1 and 86400")
        if not 1 <= self.recording_max_frames <= 100_000:
            raise ValueError("recording_max_frames must be between 1 and 100000")
        if not 1024 * 1024 <= self.recording_max_bytes <= 100 * 1024 * 1024 * 1024:
            raise ValueError("recording_max_bytes is outside the safe range")
        if not 1 <= self.recording_queue_size <= 64:
            raise ValueError("recording_queue_size must be between 1 and 64")
        if (
            self.recording_queue_size
            * self.capture_max_width
            * self.capture_max_height
            * 3
            > 256 * 1024 * 1024
        ):
            raise ValueError("recording queue can exceed the 256 MiB memory bound")
        if not 0 <= self.recording_frame_interval <= 60:
            raise ValueError("recording_frame_interval must be between 0 and 60")
        if not 0.1 <= self.recording_shutdown_timeout <= 30:
            raise ValueError("recording_shutdown_timeout must be between 0.1 and 30")
        if self.replay_timing_mode not in {
            "AS_FAST_AS_POSSIBLE",
            "RECORDED_TIMING",
            "STEP",
        }:
            raise ValueError("unsupported replay_timing_mode")
        if self.mode == WorkerMode.REPLAY:
            if self.recording_enabled:
                raise ValueError("recording cannot be enabled in REPLAY mode")
            if self.replay_session_path is None:
                raise ValueError("REPLAY mode requires replay_session_path")
            if not self.replay_session_path.is_absolute():
                raise ValueError("replay_session_path must be absolute")
            if not self.replay_session_path.is_dir():
                raise ValueError("replay_session_path must be an existing directory")
            manifest_path = self.replay_session_path / "manifest.json"
            try:
                if manifest_path.stat().st_size > 1024 * 1024:
                    raise ValueError("replay manifest exceeds size bound")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("replay manifest is missing or malformed") from exc
            if not isinstance(manifest, dict) or manifest.get("format_version") != 1:
                raise ValueError("unknown replay format version")
        elif self.recording_enabled and self.mode not in {
            WorkerMode.OBSERVE,
            WorkerMode.ACTIVE,
        }:
            raise ValueError("recording requires OBSERVE or ACTIVE mode")
        if any(
            not re.fullmatch(r"[A-Za-z0-9_+.-]{1,32}", value)
            for value in (
                self.bindings.move_up,
                self.bindings.move_down,
                self.bindings.move_left,
                self.bindings.move_right,
                self.bindings.interact,
                self.bindings.inventory,
                self.bindings.cancel,
            )
        ):
            raise ValueError("input bindings contain unsupported key names")

    @classmethod
    def from_env(cls, *, plugin: str | None = None) -> WorkerConfig:
        bindings = InputBindings()
        raw_bindings = os.getenv("WORKER_INPUT_BINDINGS")
        if raw_bindings:
            values = json.loads(raw_bindings)
            allowed = set(InputBindings.__dataclass_fields__)
            if set(values) - allowed or any(
                not isinstance(v, str) or not v for v in values.values()
            ):
                raise ValueError("invalid WORKER_INPUT_BINDINGS")
            bindings = InputBindings(**values)
        config = cls(
            schema_version=int(os.getenv("WORKER_CONFIG_SCHEMA_VERSION", "1")),
            plugin=(plugin or os.getenv("WORKER_PLUGIN", "noop")).lower(),
            mode=WorkerMode(os.getenv("WORKER_MODE", "DISABLED").upper()),
            autostart=_strict_bool("WORKER_AUTOSTART", False),
            tick_interval=float(os.getenv("WORKER_TICK_INTERVAL", "1")),
            observation_interval=float(os.getenv("WORKER_OBSERVATION_INTERVAL", "2")),
            action_timeout=float(os.getenv("WORKER_ACTION_TIMEOUT", "1")),
            recovery_attempts=int(os.getenv("WORKER_RECOVERY_ATTEMPTS", "3")),
            vision_threshold=float(os.getenv("WORKER_VISION_THRESHOLD", "0.80")),
            max_actions_per_second=float(
                os.getenv("WORKER_MAX_ACTIONS_PER_SECOND", "2")
            ),
            max_key_presses_per_second=float(
                os.getenv("WORKER_MAX_KEY_PRESSES_PER_SECOND", "6")
            ),
            action_queue_size=int(os.getenv("WORKER_ACTION_QUEUE_SIZE", "16")),
            input_subprocess_timeout=float(
                os.getenv("WORKER_INPUT_SUBPROCESS_TIMEOUT", "2")
            ),
            deadman_timeout=float(os.getenv("WORKER_DEADMAN_TIMEOUT", "5")),
            capture_backend=os.getenv("WORKER_CAPTURE_BACKEND", "x11").lower(),
            capture_timeout=float(os.getenv("WORKER_CAPTURE_TIMEOUT", "2")),
            max_frame_age=float(os.getenv("WORKER_MAX_FRAME_AGE", "3")),
            perception_timeout=float(os.getenv("WORKER_PERCEPTION_TIMEOUT", "1")),
            max_observation_age=float(os.getenv("WORKER_MAX_OBSERVATION_AGE", "3")),
            planner_timeout=float(os.getenv("WORKER_PLANNER_TIMEOUT", "0.5")),
            calibration_verified=_strict_bool("WORKER_CALIBRATION_VERIFIED", False),
            capture_max_width=int(os.getenv("WORKER_CAPTURE_MAX_WIDTH", "1280")),
            capture_max_height=int(os.getenv("WORKER_CAPTURE_MAX_HEIGHT", "720")),
            diagnostic_capture=_strict_bool("WORKER_DIAGNOSTIC_CAPTURE", True),
            diagnostic_directory=Path(
                os.getenv(
                    "WORKER_DIAGNOSTIC_DIRECTORY",
                    "/home/dst/.local/state/dst-runtime/worker-diagnostics",
                )
            ),
            diagnostic_ring_size=int(os.getenv("WORKER_DIAGNOSTIC_RING_SIZE", "10")),
            diagnostic_max_bytes=int(
                os.getenv("WORKER_DIAGNOSTIC_MAX_BYTES", str(25 * 1024 * 1024))
            ),
            recording_enabled=_strict_bool("WORKER_RECORDING_ENABLED", False),
            recording_root=Path(
                os.getenv(
                    "WORKER_RECORDING_ROOT",
                    "/home/dst/.local/state/dst-runtime/worker-recordings",
                )
            ),
            recording_max_duration=float(
                os.getenv("WORKER_RECORDING_MAX_DURATION", str(15 * 60))
            ),
            recording_max_frames=int(os.getenv("WORKER_RECORDING_MAX_FRAMES", "1800")),
            recording_max_bytes=int(
                os.getenv("WORKER_RECORDING_MAX_BYTES", str(512 * 1024 * 1024))
            ),
            recording_queue_size=int(os.getenv("WORKER_RECORDING_QUEUE_SIZE", "8")),
            recording_frame_interval=float(
                os.getenv("WORKER_RECORDING_FRAME_INTERVAL", "0.5")
            ),
            recording_shutdown_timeout=float(
                os.getenv("WORKER_RECORDING_SHUTDOWN_TIMEOUT", "5")
            ),
            replay_session_path=(
                Path(value)
                if (value := os.getenv("WORKER_REPLAY_SESSION_PATH"))
                else None
            ),
            replay_timing_mode=os.getenv(
                "WORKER_REPLAY_TIMING_MODE", "AS_FAST_AS_POSSIBLE"
            ).upper(),
            assets_manifest=Path(
                os.getenv("WORKER_ASSETS_MANIFEST", str(DEFAULT_ASSETS_MANIFEST))
            ),
            bindings=bindings,
        )
        config.validate()
        return config


@dataclass(frozen=True, slots=True)
class WorkerProfile:
    name: str
    version: int
    config: WorkerConfig
