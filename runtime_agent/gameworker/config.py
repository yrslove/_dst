from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

WORKER_CONFIG_SCHEMA_VERSION = 1
DEFAULT_ASSETS_MANIFEST = Path(__file__).parent / "dst" / "assets" / "manifest.json"


class WorkerMode(StrEnum):
    DISABLED = "DISABLED"
    OBSERVE = "OBSERVE"
    ACTIVE = "ACTIVE"


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
    deadman_timeout: float = 5.0
    capture_max_width: int = 1280
    capture_max_height: int = 720
    diagnostic_capture: bool = True
    diagnostic_directory: Path = Path("/var/lib/dst-runtime/worker-diagnostics")
    diagnostic_ring_size: int = 10
    diagnostic_max_bytes: int = 25 * 1024 * 1024
    assets_manifest: Path = DEFAULT_ASSETS_MANIFEST
    bindings: InputBindings = field(default_factory=InputBindings)

    def validate(self) -> None:
        if self.schema_version != WORKER_CONFIG_SCHEMA_VERSION:
            raise ValueError("WORKER_CONFIG_VERSION_MISMATCH")
        if self.plugin not in {"noop", "dst"}:
            raise ValueError("unsupported worker plugin")
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
        if self.deadman_timeout <= self.action_timeout or self.deadman_timeout > 30:
            raise ValueError(
                "deadman_timeout must exceed action_timeout and be at most 30 seconds"
            )
        if self.capture_max_width < 320 or self.capture_max_height < 240:
            raise ValueError("capture bounds are too small")
        if self.capture_max_width > 3840 or self.capture_max_height > 2160:
            raise ValueError("capture bounds exceed the worker resource limit")
        if not 1 <= self.diagnostic_ring_size <= 50:
            raise ValueError("diagnostic_ring_size must be between 1 and 50")
        if not 1024 * 1024 <= self.diagnostic_max_bytes <= 250 * 1024 * 1024:
            raise ValueError("diagnostic_max_bytes is outside the safe range")
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
            autostart=os.getenv("WORKER_AUTOSTART", "0").lower()
            in {"1", "true", "yes"},
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
            deadman_timeout=float(os.getenv("WORKER_DEADMAN_TIMEOUT", "5")),
            capture_max_width=int(os.getenv("WORKER_CAPTURE_MAX_WIDTH", "1280")),
            capture_max_height=int(os.getenv("WORKER_CAPTURE_MAX_HEIGHT", "720")),
            diagnostic_capture=os.getenv("WORKER_DIAGNOSTIC_CAPTURE", "1").lower()
            in {"1", "true", "yes"},
            diagnostic_directory=Path(
                os.getenv(
                    "WORKER_DIAGNOSTIC_DIRECTORY",
                    "/var/lib/dst-runtime/worker-diagnostics",
                )
            ),
            diagnostic_ring_size=int(os.getenv("WORKER_DIAGNOSTIC_RING_SIZE", "10")),
            diagnostic_max_bytes=int(
                os.getenv("WORKER_DIAGNOSTIC_MAX_BYTES", str(25 * 1024 * 1024))
            ),
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
