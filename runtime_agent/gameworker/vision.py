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


class DSTScreen(StrEnum):
    UNKNOWN = "UNKNOWN"
    MAIN_MENU = "MAIN_MENU"
    OPTIONS = "OPTIONS"
    OPTIONS_DISCARD_CONFIRM = "OPTIONS_DISCARD_CONFIRM"
    LOGIN_REWARD_AVAILABLE = "LOGIN_REWARD_AVAILABLE"
    REWARD_RESULT = "REWARD_RESULT"
    HOST_GAME_WORLD_LIST = "HOST_GAME_WORLD_LIST"
    HOST_GAME_WORLD_SELECTED = "HOST_GAME_WORLD_SELECTED"
    MODS_DISABLED_CONFIRMATION = "MODS_DISABLED_CONFIRMATION"
    HOST_GAME_PLAYSTYLE = "HOST_GAME_PLAYSTYLE"
    HOST_GAME_CAVES_PROMPT = "HOST_GAME_CAVES_PROMPT"
    IN_WORLD_IDLE = "IN_WORLD_IDLE"
    GIFT_AVAILABLE = "GIFT_AVAILABLE"
    IN_WORLD_GIFT_OPENING = "IN_WORLD_GIFT_OPENING"
    IN_WORLD_GIFT_RECEIVED = "IN_WORLD_GIFT_RECEIVED"
    LOADING = "LOADING"
    DISCONNECTED = "DISCONNECTED"
    PAUSED = "PAUSED"
    DEAD = "DEAD"
    WORLD_RESET_PENDING = "WORLD_RESET_PENDING"
    CHARACTER_SELECTION = "CHARACTER_SELECTION"
    CHARACTER_SELECTION_HOVERED = "CHARACTER_SELECTION_HOVERED"
    CHARACTER_LOADOUT = "CHARACTER_LOADOUT"
    UNEXPECTED_MODAL = "UNEXPECTED_MODAL"


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
    screen: DSTScreen = DSTScreen.UNKNOWN
    screen_confidence: float = 0.0
    frame_width: int = 0
    frame_height: int = 0
    gameplay_change: float | None = None

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
            or not isinstance(self.screen, DSTScreen)
            or not math.isfinite(self.screen_confidence)
            or not 0 <= self.screen_confidence <= 1
            or self.frame_width < 0
            or self.frame_height < 0
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
            or (
                self.gameplay_change is not None
                and (
                    not math.isfinite(self.gameplay_change)
                    or not 0 <= self.gameplay_change <= 1
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
        self._last_gameplay_hash: bytes | None = None
        self._last_changed_at: float | None = None

    @staticmethod
    def frame_digest(frame) -> tuple[str, bytes]:
        tiny = frame.convert("L").resize((16, 16))
        values = tiny.tobytes()
        return hashlib.sha256(values).hexdigest()[:16], values

    def update(
        self, frame, now_monotonic: float
    ) -> tuple[str, float | None, bool, float | None]:
        digest, current = self.frame_digest(frame)
        width, height = frame.size
        gameplay = frame.crop((
            int(width * 0.18), int(height * 0.16),
            int(width * 0.82), int(height * 0.82),
        )).convert("L").resize((16, 16)).tobytes()
        change = None
        gameplay_change = None
        if self._last_hash is not None:
            change = sum(abs(a - b) for a, b in zip(current, self._last_hash)) / (
                255 * len(current)
            )
            gameplay_change = sum(
                abs(a - b) for a, b in zip(gameplay, self._last_gameplay_hash)
            ) / (255 * len(gameplay))
            if change >= self.change_threshold:
                self._last_changed_at = now_monotonic
        else:
            self._last_changed_at = now_monotonic
        self._last_hash = current
        self._last_gameplay_hash = gameplay
        frozen = bool(
            self._last_changed_at is not None
            and now_monotonic - self._last_changed_at >= self.frozen_after_seconds
        )
        return digest, change, frozen, gameplay_change


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
        self._gift_hover = None

    def arm_gift_hover(self, frame: Frame, observation: GameObservation) -> None:
        from runtime_agent.gameworker.gift_icon import HOVER_ROI

        if (
            observation.screen != DSTScreen.IN_WORLD_IDLE
            or not observation.production_ready
            or not observation.is_fresh(self._clock())
        ):
            return
        icon = next((d for d in observation.detections if d.kind == "gift_icon"), None)
        if icon is None or not icon.detected or not icon.verified:
            return
        image = frame.image()
        self._gift_hover = (
            self._clock() + 8,
            frame.runtime_generation,
            frame.worker_generation,
            frame.sequence,
            icon.bounds,
            image.crop(Viewport(*image.size).region(NormalizedRegion(*HOVER_ROI))),
        )

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
            _, score, _, location = cv2.minMaxLoc(result)
            confidence = max(0.0, min(1.0, float(score)))
            left = region[0] + location[0]
            top = region[1] + location[1]
            return Detection(
                template_id,
                confidence >= asset.threshold,
                confidence,
                bounds=NormalizedRegion(
                    left / image.width,
                    top / image.height,
                    (left + template.shape[1]) / image.width,
                    (top + template.shape[0]) / image.height,
                ),
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
        digest, change, frozen, gameplay_change = self._monitor.update(image, started)
        names = ("game_hud", "pause_menu", "player_marker", "interaction_prompt")
        detected = {
            name: self.detect(image, name, deadline=deadline)
            for name in (
                *names,
                *(key for key in self.registry.assets if key not in names),
            )
        }
        detections = tuple(detected.values())
        game, menu, player, interaction = (detected[name] for name in names)

        def found(name: str) -> bool:
            item = detected.get(name)
            return bool(item and item.detected and item.verified)

        screen = DSTScreen.UNKNOWN
        confidence = 0.0
        reward_buttons = ("login_reward_open_button", "login_reward_open_hover")
        matched_buttons = [detected[name] for name in reward_buttons if found(name)]
        reward_close = (
            "login_reward_close_button" if found("login_reward_close_button")
            else "login_reward_next_button"
        )
        discard_anchors = (
            "options_discard_title",
            "options_discard_body",
            "options_discard_yes",
        )
        if found("loading_label"):
            screen = DSTScreen.LOADING
            confidence = detected["loading_label"].confidence
        elif found("death_world_reset_text") and found("death_reset_now_button"):
            screen = DSTScreen.WORLD_RESET_PENDING
            confidence = min(
                detected["death_world_reset_text"].confidence,
                detected["death_reset_now_button"].confidence,
            )
        elif all(found(key) for key in discard_anchors):
            screen = DSTScreen.OPTIONS_DISCARD_CONFIRM
            confidence = min(detected[key].confidence for key in discard_anchors)
        elif found("mods_disabled_title") and found("mods_disabled_continue"):
            screen = DSTScreen.MODS_DISABLED_CONFIRMATION
            confidence = min(
                detected["mods_disabled_title"].confidence,
                detected["mods_disabled_continue"].confidence,
            )
        elif all(found(key) for key in (
            "inworld_gift_received_title", "inworld_gift_use_later", "inworld_gift_use_now",
        )):
            screen = DSTScreen.IN_WORLD_GIFT_RECEIVED
            confidence = min(detected[key].confidence for key in (
                "inworld_gift_received_title", "inworld_gift_use_later", "inworld_gift_use_now",
            ))
        elif found("inworld_gift_opening_title") and found("inworld_gift_popup_ribbon"):
            screen = DSTScreen.IN_WORLD_GIFT_OPENING
            confidence = min(detected[key].confidence for key in (
                "inworld_gift_opening_title", "inworld_gift_popup_ribbon",
            ))
        elif found("login_reward_result_title") and found(reward_close):
            screen = DSTScreen.REWARD_RESULT
            confidence = min(
                detected["login_reward_result_title"].confidence,
                detected[reward_close].confidence,
            )
        elif found("login_reward_title") and matched_buttons:
            screen = DSTScreen.LOGIN_REWARD_AVAILABLE
            confidence = min(
                detected["login_reward_title"].confidence,
                max(button.confidence for button in matched_buttons),
            )
        elif matched_buttons:
            # The Open Now button is a verified, specific anchor. The modal can
            # dim its title far below threshold while the button remains clear.
            screen = DSTScreen.LOGIN_REWARD_AVAILABLE
            confidence = max(button.confidence for button in matched_buttons)
        elif found("login_reward_title"):
            # A reward overlay obscures the menu even if its button animates.
            screen = DSTScreen.UNKNOWN
        elif found("options_title") and found("options_back"):
            screen = DSTScreen.OPTIONS
            confidence = min(
                detected["options_title"].confidence,
                detected["options_back"].confidence,
            )
        elif all(
            found(key)
            for key in (
                "character_select_title",
                "character_select_players",
                "character_select_wilson_name",
                "character_select_wilson_hover",
            )
        ):
            screen = DSTScreen.CHARACTER_SELECTION_HOVERED
            confidence = min(
                detected[key].confidence
                for key in (
                    "character_select_title",
                    "character_select_players",
                    "character_select_wilson_name",
                    "character_select_wilson_hover",
                )
            )
        elif all(
            found(key)
            for key in (
                "character_select_title",
                "character_select_players",
                "character_select_wilson_icon",
            )
        ):
            screen = DSTScreen.CHARACTER_SELECTION
            confidence = min(
                detected[key].confidence
                for key in (
                    "character_select_title",
                    "character_select_players",
                    "character_select_wilson_icon",
                )
            )
        elif (
            found("character_select_players")
            and found("character_loadout_wilson_name")
            and not found("character_select_title")
        ):
            screen = DSTScreen.CHARACTER_LOADOUT
            confidence = min(
                detected["character_select_players"].confidence,
                detected["character_loadout_wilson_name"].confidence,
            )
        elif all(
            found(key)
            for key in (
                "host_game_playstyle_title",
                "host_game_world_selected_name",
                "host_game_world_selected_start",
            )
        ):
            keys = (
                "host_game_playstyle_title",
                "host_game_world_selected_name",
                "host_game_world_selected_start",
            )
            screen = DSTScreen.HOST_GAME_WORLD_SELECTED
            confidence = min(detected[key].confidence for key in keys)
        elif all(
            found(key)
            for key in (
                "host_game_playstyle_title",
                "host_game_world_list_search",
                "host_game_world_list_create_new",
                "host_game_existing_world_row",
            )
        ):
            keys = (
                "host_game_playstyle_title",
                "host_game_world_list_search",
                "host_game_world_list_create_new",
                "host_game_existing_world_row",
            )
            screen = DSTScreen.HOST_GAME_WORLD_LIST
            confidence = min(detected[key].confidence for key in keys)
        elif all(
            found(key)
            for key in (
                "host_game_playstyle_title",
                "host_game_playstyle_prompt",
                "host_game_playstyle_survival",
            )
        ):
            screen = DSTScreen.HOST_GAME_PLAYSTYLE
            confidence = min(
                detected[key].confidence
                for key in (
                    "host_game_playstyle_title",
                    "host_game_playstyle_prompt",
                    "host_game_playstyle_survival",
                )
            )
        elif all(
            found(key)
            for key in (
                "host_game_caves_prompt_title",
                "host_game_caves_option_caves",
                "host_game_caves_option_no_caves",
                "host_game_caves_back",
            )
        ):
            screen = DSTScreen.HOST_GAME_CAVES_PROMPT
            confidence = min(
                detected[key].confidence
                for key in (
                    "host_game_caves_prompt_title",
                    "host_game_caves_option_caves",
                    "host_game_caves_option_no_caves",
                    "host_game_caves_back",
                )
            )
        elif (
            found("main_menu_browse")
            and found("main_menu_host_game")
            and found("main_menu_options")
        ):
            from PIL import ImageStat

            menu_region = Viewport(*image.size).region(
                NormalizedRegion(0.02, 0.43, 0.26, 0.60)
            )
            menu_luminance = ImageStat.Stat(image.crop(menu_region).convert("L")).mean[
                0
            ]
            if menu_luminance >= 12.5:
                screen = DSTScreen.MAIN_MENU
                confidence = min(
                    detected["main_menu_browse"].confidence,
                    detected["main_menu_host_game"].confidence,
                    detected["main_menu_options"].confidence,
                    min(1.0, menu_luminance / 20.0),
                )
        elif found("pause_menu"):
            screen, confidence = DSTScreen.PAUSED, menu.confidence
        elif found("game_hud") and found("world_inventory_frame"):
            screen = DSTScreen.IN_WORLD_IDLE
            confidence = min(game.confidence, detected["world_inventory_frame"].confidence)
        elif found("game_hud") and found("player_marker"):
            screen = DSTScreen.IN_WORLD_IDLE
            confidence = min(game.confidence, player.confidence)
        elif found("game_hud") and found("gift_icon_active"):
            # The live ACTIVE gift changes the gray toast color enough that its
            # gray identity template no longer matches. Its separately verified
            # crop is an independent in-world HUD anchor for this state.
            screen = DSTScreen.IN_WORLD_IDLE
            confidence = min(
                game.confidence,
                detected["gift_icon_active"].confidence,
            )
        elif found("game_hud") and found("world_present_banner"):
            # The health-heart template is sensitive to its changing fill and
            # overlay rendering. Two independent, verified HUD anchors can
            # identify the world when that player-specific template misses.
            # Keep this after the explicit menu, reward, pause, loading, and
            # reset classifiers above so those states retain precedence.
            screen = DSTScreen.IN_WORLD_IDLE
            confidence = min(
                detected["game_hud"].confidence,
                detected["world_present_banner"].confidence,
            )
        confidence_threshold = self.default_threshold
        if screen == DSTScreen.IN_WORLD_IDLE:
            marker_asset = self.registry.assets.get("player_marker")
            if marker_asset is not None:
                # The player health marker has its own verified threshold; it
                # can vary slightly as DST renders the animated health icon.
                confidence_threshold = min(
                    confidence_threshold, marker_asset.threshold
                )
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
        if screen == DSTScreen.UNKNOWN or confidence < confidence_threshold:
            flags.append("UNKNOWN")
        if any(
            key in {"detector_error", "detector_timeout"}
            for item in detections
            for key, _ in item.metadata
        ):
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
            and screen != DSTScreen.UNKNOWN
            and confidence >= confidence_threshold
        ):
            validity = ObservationValidity.VALID
        if (
            screen == DSTScreen.IN_WORLD_IDLE
            and validity == ObservationValidity.VALID
            and calibration.profile_id == "dst-1280x720-linux-v1"
        ):
            from runtime_agent.gameworker.gift_icon import (
                HOVER_ROI,
                classify_icon,
                hover_response,
            )

            icon = detected.get("world_present_banner")
            active_icon = detected.get("gift_icon_active")
            if (
                active_icon is not None
                and active_icon.verified
                and active_icon.detected
            ):
                icon = active_icon
            template = self._templates.get("world_present_banner")
            if icon is not None and template is not None:
                hovered = False
                if self._gift_hover is not None:
                    expires, rg, wg, seq, bounds, before = self._gift_hover
                    if self._clock() > expires:
                        self._gift_hover = None
                    elif (
                        frame.runtime_generation == rg
                        and frame.worker_generation == wg
                        and frame.sequence > seq
                        and frame.captured_monotonic >= expires - 8
                        and icon.bounds == bounds
                    ):
                        hovered = hover_response(
                            before,
                            image.crop(
                                Viewport(*image.size).region(
                                    NormalizedRegion(*HOVER_ROI)
                                )
                            ),
                        )
                evidence = classify_icon(
                    image,
                    icon,
                    template,
                    active=active_icon,
                    hover_verified=hovered,
                )
                if (
                    icon.confidence >= 0.94
                    and evidence.get("chroma_p95", 0) >= 35
                    and evidence.get("colored_fraction", 0) >= 0.20
                ):
                    # Preserve one natural colored candidate; do not infer a claim.
                    sample = Path("/tmp/dst-gift-active-reference.png")
                    try:
                        if not sample.exists():
                            image.save(sample)
                            image.crop(Viewport(*image.size).region(icon.bounds)).save(
                                sample.with_name("dst-gift-active-reference-roi.png")
                            )
                        evidence["active_sample_path"] = str(sample)
                    except OSError:
                        evidence["active_sample_error"] = "CAPTURE_FAILED"
                detections += (
                    Detection(
                        "gift_icon",
                        icon.confidence >= 0.85,
                        icon.confidence,
                        bounds=icon.bounds,
                        detector_id="gift-icon-template-chroma",
                        template_id="world_present_banner",
                        verified=icon.verified,
                        metadata=tuple(evidence.items()),
                    ),
                    Detection(
                        "gift_hover_response",
                        hovered,
                        1.0 if hovered else 0.0,
                        verified=True,
                    ),
                )
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
            screen=screen,
            screen_confidence=confidence,
            frame_width=frame.width,
            frame_height=frame.height,
            gameplay_change=gameplay_change,
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
