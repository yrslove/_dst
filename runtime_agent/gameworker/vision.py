from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from runtime_agent.gameworker.geometry import NormalizedRegion, Viewport


@dataclass(frozen=True, slots=True)
class Detection:
    detected: bool
    confidence: float
    template_id: str | None = None


@dataclass(slots=True)
class GameObservation:
    timestamp: str
    game_visible: Detection
    menu_visible: Detection
    player_visible: Detection
    interaction_prompt_visible: Detection
    worker_confidence: float
    screen_hash: str
    screen_change: float | None
    diagnostic_flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class TemplateAsset:
    id: str
    filename: str
    expected_region: NormalizedRegion
    threshold: float
    version: int


class AssetRegistry:
    def __init__(self, manifest_path: Path):
        self.manifest_path = manifest_path
        self.assets: dict[str, TemplateAsset] = {}
        self.error_code: str | None = None
        self._load()

    @property
    def configured(self) -> bool:
        return bool(self.assets) and self.error_code is None

    def _load(self) -> None:
        if not self.manifest_path.is_file():
            self.error_code = "WORKER_ASSET_MISSING"
            return
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1:
                raise ValueError("asset schema version mismatch")
            for item in payload.get("templates", []):
                region = item["expected_region"]
                asset = TemplateAsset(
                    id=item["id"],
                    filename=item["filename"],
                    expected_region=NormalizedRegion(*region),
                    threshold=float(item["threshold"]),
                    version=int(item["version"]),
                )
                if not (self.manifest_path.parent / asset.filename).is_file():
                    self.error_code = "WORKER_ASSET_MISSING"
                    continue
                self.assets[asset.id] = asset
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            self.assets.clear()
            self.error_code = "WORKER_ASSET_MISSING"


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
        values = bytes(tiny.getdata())
        digest = hashlib.sha256(values).hexdigest()[:16]
        return digest, values

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


class VisionDetector:
    """Small template-matching layer; absent assets always produce UNKNOWN."""

    def __init__(self, registry: AssetRegistry, *, default_threshold: float):
        self.registry = registry
        self.default_threshold = default_threshold
        self._templates: dict[str, object] = {}

    def detect(self, frame, template_id: str) -> Detection:
        asset = self.registry.assets.get(template_id)
        if asset is None:
            return Detection(False, 0.0, template_id)
        try:
            import cv2
            import numpy as np

            template = self._templates.get(template_id)
            if template is None:
                template = cv2.imread(
                    str(self.registry.manifest_path.parent / asset.filename),
                    cv2.IMREAD_GRAYSCALE,
                )
                if template is None:
                    return Detection(False, 0.0, template_id)
                self._templates[template_id] = template
            region = Viewport(*frame.size).region(asset.expected_region)
            sample = np.asarray(frame.crop(region).convert("L"))
            if (
                sample.shape[0] < template.shape[0]
                or sample.shape[1] < template.shape[1]
            ):
                return Detection(False, 0.0, template_id)
            result = cv2.matchTemplate(sample, template, cv2.TM_CCOEFF_NORMED)
            confidence = max(0.0, min(1.0, float(result.max())))
            threshold = asset.threshold or self.default_threshold
            return Detection(confidence >= threshold, confidence, template_id)
        except (ImportError, ValueError, TypeError):
            return Detection(False, 0.0, template_id)

    def observe(
        self, frame, *, screen_hash: str, screen_change: float | None, frozen: bool
    ) -> GameObservation:
        game = self.detect(frame, "game_hud")
        menu = self.detect(frame, "pause_menu")
        player = self.detect(frame, "player_marker")
        interaction = self.detect(frame, "interaction_prompt")
        flags: list[str] = []
        if not self.registry.configured:
            flags.append("UNCONFIGURED")
        if frozen:
            flags.append("SCREEN_FROZEN")
        confidence = max(
            game.confidence, menu.confidence, player.confidence, interaction.confidence
        )
        if confidence < self.default_threshold:
            flags.append("UNKNOWN")
        return GameObservation(
            timestamp=datetime.now(timezone.utc).isoformat(),
            game_visible=game,
            menu_visible=menu,
            player_visible=player,
            interaction_prompt_visible=interaction,
            worker_confidence=confidence,
            screen_hash=screen_hash,
            screen_change=screen_change,
            diagnostic_flags=flags,
        )
