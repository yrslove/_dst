from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Protocol

from runtime_agent.gameworker.capture import Frame
from runtime_agent.gameworker.geometry import (
    CalibrationProfile,
    NormalizedRegion,
    Viewport,
)

logger = logging.getLogger("runtime_agent.gameworker.vision")
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_TEMPLATE_BYTES = 16 * 1024 * 1024


class ObservationValidity(StrEnum):
    VALID = "VALID"
    UNKNOWN = "UNKNOWN"
    STALE = "STALE"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Detection:
    kind: str
    detected: bool
    confidence: float
    bounds: NormalizedRegion | None = None
    detector_id: str = "unknown"
    detector_version: int = 1
    template_id: str | None = None
    verified: bool = False
    metadata: tuple[tuple[str, bool | int | float | str | None], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.kind, str)
            or not self.kind
            or len(self.kind) > 128
            or not isinstance(self.detected, bool)
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("detection kind/confidence is invalid")
        if (
            not isinstance(self.detector_id, str)
            or not self.detector_id
            or len(self.detector_id) > 128
            or self.detector_version < 1
            or (
                self.template_id is not None
                and (
                    not isinstance(self.template_id, str) or len(self.template_id) > 128
                )
            )
            or not isinstance(self.verified, bool)
            or not isinstance(self.metadata, tuple)
            or len(self.metadata) > 32
            or any(
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not item[0]
                or len(item[0]) > 64
                or not isinstance(item[1], (bool, int, float, str, type(None)))
                or (isinstance(item[1], str) and len(item[1]) > 256)
                or (isinstance(item[1], float) and not math.isfinite(item[1]))
                for item in self.metadata
            )
        ):
            raise ValueError("detection provenance/metadata is invalid")


@dataclass(frozen=True, slots=True)
class GameObservation:
    timestamp: str
    observed_monotonic: float
    observation_generation: int
    source_frame_id: str
    source_sequence: int
    source_captured_monotonic: float
    runtime_id: int
    runtime_generation: int
    worker_generation: int
    validity: ObservationValidity
    fresh_until: float
    game_visible: Detection
    menu_visible: Detection
    player_visible: Detection
    interaction_prompt_visible: Detection
    worker_confidence: float
    detections: tuple[Detection, ...]
    calibration_profile_id: str
    calibration_version: int
    calibration_verified: bool
    assets_verified: bool
    screen_hash: str
    screen_change: float | None
    perception_latency: float
    diagnostic_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timestamp, str)
            or not self.timestamp
            or len(self.timestamp) > 64
            or not isinstance(self.source_frame_id, str)
            or not self.source_frame_id
            or len(self.source_frame_id) > 128
            or self.observation_generation < 1
            or self.source_sequence < 1
            or self.runtime_id < 1
            or self.runtime_generation < 1
            or self.worker_generation < 1
            or not math.isfinite(self.observed_monotonic)
            or not math.isfinite(self.source_captured_monotonic)
            or self.source_captured_monotonic < 0
            or self.observed_monotonic < self.source_captured_monotonic
            or not math.isfinite(self.fresh_until)
        ):
            raise ValueError("observation identity is invalid")
        if (
            not math.isfinite(self.worker_confidence)
            or not 0 <= self.worker_confidence <= 1
        ):
            raise ValueError("observation confidence is invalid")
        if (
            not isinstance(self.validity, ObservationValidity)
            or not isinstance(self.detections, tuple)
            or len(self.detections) > 128
            or any(not isinstance(item, Detection) for item in self.detections)
            or not isinstance(self.diagnostic_flags, tuple)
            or len(self.diagnostic_flags) > 32
            or any(
                not isinstance(item, str) or not item or len(item) > 128
                for item in self.diagnostic_flags
            )
            or not isinstance(self.calibration_profile_id, str)
            or not self.calibration_profile_id
            or len(self.calibration_profile_id) > 128
            or self.calibration_version < 1
            or not isinstance(self.calibration_verified, bool)
            or not isinstance(self.assets_verified, bool)
            or not math.isfinite(self.perception_latency)
            or self.perception_latency < 0
            or not isinstance(self.screen_hash, str)
            or not self.screen_hash
            or len(self.screen_hash) > 128
            or (
                self.screen_change is not None
                and (
                    not math.isfinite(self.screen_change)
                    or not 0 <= self.screen_change <= 1
                )
            )
        ):
            raise ValueError("observation collections exceed bounds")

    @property
    def game_available(self) -> bool:
        return self.game_visible.detected

    @property
    def valid(self) -> bool:
        return self.validity == ObservationValidity.VALID

    @property
    def confidence(self) -> float:
        return self.worker_confidence

    def is_fresh(self, now_monotonic: float | None = None) -> bool:
        now = time.monotonic() if now_monotonic is None else now_monotonic
        return self.valid and now <= self.fresh_until

    @property
    def production_ready(self) -> bool:
        return self.valid and self.calibration_verified and self.assets_verified

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TemplateAsset:
    id: str
    filename: str
    expected_region: NormalizedRegion
    threshold: float
    version: int
    expected_use: str
    verified: bool = False

    def __post_init__(self) -> None:
        if (
            not isinstance(self.id, str)
            or not self.id
            or len(self.id) > 128
            or not isinstance(self.filename, str)
            or not self.filename
            or len(self.filename) > 512
            or self.version < 1
            or not isinstance(self.expected_use, str)
            or not self.expected_use
            or len(self.expected_use) > 256
            or not isinstance(self.verified, bool)
        ):
            raise ValueError("template identity is invalid")
        if not math.isfinite(self.threshold) or not 0 < self.threshold <= 1:
            raise ValueError("template threshold is invalid")


class AssetRegistry:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path.resolve()
        self.assets: dict[str, TemplateAsset] = {}
        self.error_code: str | None = None
        self.profile = "unconfigured"
        self._load()

    @property
    def configured(self) -> bool:
        return bool(self.assets) and self.error_code is None

    @property
    def production_ready(self) -> bool:
        return self.configured and all(asset.verified for asset in self.assets.values())

    def _load(self) -> None:
        if not self.manifest_path.is_file():
            self.error_code = "WORKER_ASSET_MISSING"
            return
        root = self.manifest_path.parent
        try:
            if self.manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
                raise ValueError("asset manifest exceeds size bound")
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("asset manifest must be an object")
            if payload.get("schema_version") != 1:
                raise ValueError("asset schema version mismatch")
            self.profile = str(payload.get("profile") or "unconfigured")
            templates = payload.get("templates", [])
            if not isinstance(templates, list) or len(templates) > 128:
                raise ValueError("asset template collection is invalid")
            seen: set[str] = set()
            for item in templates:
                if not isinstance(item, dict):
                    raise TypeError("asset template must be an object")
                template_id = str(item["id"])
                if template_id in seen:
                    raise ValueError("duplicate template ID")
                seen.add(template_id)
                filename = str(item["filename"])
                path = (root / filename).resolve()
                if Path(filename).is_absolute() or not path.is_relative_to(root):
                    raise ValueError("template path escapes registry root")
                if path.suffix.lower() != ".png":
                    raise ValueError("template must be PNG")
                region = item["expected_region"]
                verified = item.get("verified", False)
                if not isinstance(verified, bool):
                    raise TypeError("template verified must be boolean")
                asset = TemplateAsset(
                    id=template_id,
                    filename=filename,
                    expected_region=NormalizedRegion(*region),
                    threshold=float(item["threshold"]),
                    version=int(item["version"]),
                    expected_use=str(item.get("expected_use") or template_id),
                    verified=verified,
                )
                if not path.is_file():
                    self.error_code = "WORKER_ASSET_MISSING"
                    continue
                if path.stat().st_size > MAX_TEMPLATE_BYTES:
                    raise ValueError("template file exceeds size bound")
                self.assets[asset.id] = asset
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.assets.clear()
            self.error_code = "WORKER_ASSET_INVALID"


class FrameChangeMonitor:
    def __init__(
        self, *, frozen_after_seconds: float = 30.0, change_threshold: float = 0.01
    ):
        self.frozen_after_seconds = frozen_after_seconds
        self.change_threshold = change_threshold
        self._last_hash: bytes | None = None
        self._last_changed_at: float | None = None

    @staticmethod
    def frame_digest(frame) -> tuple[str, bytes]:
        tiny = frame.convert("L").resize((16, 16))
        values = tiny.tobytes()
        return hashlib.sha256(values).hexdigest()[:16], values

    def update(self, frame, now_monotonic: float) -> tuple[str, float | None, bool]:
        digest, current = self.frame_digest(frame)
        change = None
        if self._last_hash is not None:
            change = sum(abs(a - b) for a, b in zip(current, self._last_hash)) / (
                255 * len(current)
            )
            if change >= self.change_threshold:
                self._last_changed_at = now_monotonic
        else:
            self._last_changed_at = now_monotonic
        self._last_hash = current
        frozen = bool(
            self._last_changed_at is not None
            and now_monotonic - self._last_changed_at >= self.frozen_after_seconds
        )
        return digest, change, frozen


class PerceptionEngine(Protocol):
    def analyze(
        self,
        frame: Frame,
        *,
        calibration: CalibrationProfile,
        observation_generation: int,
        max_frame_age: float,
        deadline: float,
    ) -> GameObservation: ...


class VisionDetector:
    """Generic template engine; absent/unverified assets produce UNKNOWN."""

    def __init__(
        self,
        registry: AssetRegistry,
        *,
        default_threshold: float,
        clock=None,
        wall_clock=None,
    ):
        if not 0 <= default_threshold <= 1:
            raise ValueError("default confidence threshold is invalid")
        self.registry = registry
        self.default_threshold = default_threshold
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or (
            lambda: datetime.now(timezone.utc).isoformat()
        )
        self._templates: dict[str, object] = {}
        self._monitor = FrameChangeMonitor()

    def detect(
        self, image, template_id: str, *, deadline: float | None = None
    ) -> Detection:
        asset = self.registry.assets.get(template_id)
        if asset is None:
            return Detection(template_id, False, 0.0, template_id=template_id)
        if deadline is not None and self._clock() >= deadline:
            return Detection(
                template_id,
                False,
                0.0,
                template_id=template_id,
                metadata=(("detector_timeout", True),),
            )
        try:
            import cv2
            import numpy as np

            template = self._templates.get(template_id)
            if template is None:
                template_path = self.registry.manifest_path.parent / asset.filename
                template = cv2.imdecode(
                    np.frombuffer(template_path.read_bytes(), dtype=np.uint8),
                    cv2.IMREAD_GRAYSCALE,
                )
                if template is None:
                    return Detection(
                        template_id,
                        False,
                        0.0,
                        template_id=template_id,
                        metadata=(("detector_error", "INVALID_TEMPLATE_IMAGE"),),
                    )
                self._templates[template_id] = template
            region = Viewport(*image.size).region(asset.expected_region)
            sample = np.asarray(image.crop(region).convert("L"))
            if (
                sample.shape[0] < template.shape[0]
                or sample.shape[1] < template.shape[1]
            ):
                return Detection(template_id, False, 0.0, template_id=template_id)
            result = cv2.matchTemplate(sample, template, cv2.TM_CCOEFF_NORMED)
            confidence = max(0.0, min(1.0, float(result.max())))
            return Detection(
                template_id,
                confidence >= asset.threshold,
                confidence,
                bounds=asset.expected_region,
                detector_id="opencv-template",
                template_id=template_id,
                verified=asset.verified,
            )
        except Exception as exc:  # noqa: BLE001 - isolate each detector backend
            logger.warning(
                "detector failed detector=%s error=%s",
                template_id,
                type(exc).__name__,
            )
            return Detection(
                template_id,
                False,
                0.0,
                template_id=template_id,
                metadata=(("detector_error", type(exc).__name__),),
            )

    def analyze(
        self,
        frame: Frame,
        *,
        calibration: CalibrationProfile,
        observation_generation: int,
        max_frame_age: float,
        deadline: float,
    ) -> GameObservation:
        started = self._clock()
        stale = not frame.is_fresh(max_frame_age, started)
        generation_valid = frame.runtime_generation > 0 and frame.worker_generation > 0
        image = frame.image()
        digest, change, frozen = self._monitor.update(image, started)
        detections = tuple(
            self.detect(image, name, deadline=deadline)
            for name in (
                "game_hud",
                "pause_menu",
                "player_marker",
                "interaction_prompt",
            )
        )
        game, menu, player, interaction = detections
        confidence = max(item.confidence for item in detections)
        assets_verified = self.registry.production_ready
        calibration_verified = calibration.verified and calibration.matches(
            frame.width, frame.height
        )
        flags: list[str] = []
        if not self.registry.configured:
            flags.append("UNCONFIGURED")
        if not assets_verified:
            flags.append("ASSETS_UNVERIFIED")
        if not calibration_verified:
            flags.append("CALIBRATION_UNVERIFIED")
        if frozen:
            flags.append("SCREEN_FROZEN")
        if stale:
            flags.append("STALE")
        if confidence < self.default_threshold:
            flags.append("UNKNOWN")
        if any(item.metadata for item in detections):
            flags.append("DETECTOR_ERROR")
        finished = self._clock()
        validity = ObservationValidity.UNKNOWN
        if stale:
            validity = ObservationValidity.STALE
        elif not generation_valid or finished > deadline:
            validity = ObservationValidity.INVALID
            flags.append(
                "PERCEPTION_TIMEOUT" if finished > deadline else "GENERATION_INVALID"
            )
        elif (
            assets_verified
            and calibration_verified
            and confidence >= self.default_threshold
        ):
            validity = ObservationValidity.VALID
        return GameObservation(
            timestamp=self._wall_clock(),
            observed_monotonic=finished,
            observation_generation=observation_generation,
            source_frame_id=frame.frame_id,
            source_sequence=frame.sequence,
            source_captured_monotonic=frame.captured_monotonic,
            runtime_id=frame.runtime_id,
            runtime_generation=frame.runtime_generation,
            worker_generation=frame.worker_generation,
            validity=validity,
            fresh_until=frame.captured_monotonic + max_frame_age,
            game_visible=game,
            menu_visible=menu,
            player_visible=player,
            interaction_prompt_visible=interaction,
            worker_confidence=confidence,
            detections=detections,
            calibration_profile_id=calibration.profile_id,
            calibration_version=calibration.version,
            calibration_verified=calibration_verified,
            assets_verified=assets_verified,
            screen_hash=digest,
            screen_change=change,
            perception_latency=max(0.0, finished - started),
            diagnostic_flags=tuple(dict.fromkeys(flags)),
        )


class ObservationStore:
    """Rejects late or cross-generation results before they become current."""

    def __init__(self, runtime_generation: int, worker_generation: int):
        self.runtime_generation = runtime_generation
        self.worker_generation = worker_generation
        self._current: GameObservation | None = None
        self._last_source_sequence = 0
        self._last_observation_generation = 0
        self._lock = Lock()

    def publish(self, observation: GameObservation) -> bool:
        with self._lock:
            if (
                observation.runtime_generation != self.runtime_generation
                or observation.worker_generation != self.worker_generation
            ):
                return False
            if (
                observation.source_sequence <= self._last_source_sequence
                or observation.observation_generation
                <= self._last_observation_generation
            ):
                return False
            self._current = observation
            self._last_source_sequence = observation.source_sequence
            self._last_observation_generation = observation.observation_generation
            return True

    def current(self) -> GameObservation | None:
        with self._lock:
            return self._current

    def invalidate(self) -> None:
        with self._lock:
            self._current = None
