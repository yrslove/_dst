from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock


@dataclass(frozen=True, slots=True)
class _EvidenceFrame:
    frame: object
    observation: dict


class WorkerDiagnostics:
    def __init__(
        self, directory: Path, *, enabled: bool, ring_size: int, max_bytes: int
    ):
        self.directory = directory
        self.enabled = enabled
        self.ring_size = max(1, min(ring_size, 50))
        self.max_bytes = max(1024 * 1024, max_bytes)
        self._artifacts: deque[Path] = deque()
        self._lock = Lock()
        self._frames: deque[_EvidenceFrame] = deque(maxlen=8)
        self._pending: list[_EvidenceFrame] | None = None
        self._pending_future_frames = 0
        self._pending_failure_index = 0
        self._pending_previous_stable_state: str | None = None
        self._pending_context: dict = {}
        self._last_stable_observation: dict | None = None
        self._uncertainty_active = False
        self._context: dict = {}

    def update_context(self, **values) -> None:
        """Keep small worker-owned state for the next uncertainty bundle."""
        with self._lock:
            self._context = {
                key: value
                for key, value in values.items()
                if isinstance(value, (str, int, float, bool, type(None)))
            }

    def save_frame(self, frame_id: str, destination: Path) -> bool:
        """Export one identified frame from the existing bounded evidence ring."""
        with self._lock:
            record = next((item for item in self._frames
                           if item.frame.frame_id == frame_id), None)
            if record is None:
                return False
            destination.parent.mkdir(parents=True, exist_ok=True)
            record.frame.image().save(destination, compress_level=1)
            return True

    def observe(self, frame, observation, *, assets, calibration) -> None:
        """Retain a small frame ring and finalize one bundle per unknown episode."""
        if not self.enabled:
            return
        record = _EvidenceFrame(frame, observation.as_dict())
        with self._lock:
            if self._frames and (
                self._frames[-1].frame.runtime_generation != frame.runtime_generation
                or self._frames[-1].frame.worker_generation != frame.worker_generation
            ):
                self._frames.clear()
                self._pending = None
                self._pending_future_frames = 0
                self._last_stable_observation = None
                self._uncertainty_active = False
            self._frames.append(record)
            if self._pending is not None:
                if record.frame.sequence > self._pending[-1].frame.sequence:
                    self._pending.append(record)
                    self._pending_future_frames += 1
                if self._pending_future_frames >= 3:
                    pending, self._pending = self._pending, None
                    self._pending_future_frames = 0
                    self._write_evidence_locked(
                        pending,
                        failure_index=self._pending_failure_index,
                        assets=assets,
                        calibration=calibration,
                    )
            if observation.production_ready:
                self._last_stable_observation = observation.as_dict()
            marker = next(
                (
                    item
                    for item in observation.detections
                    if item.kind == "player_marker"
                ),
                None,
            )
            marker_asset = assets.get("player_marker")
            marker_uncertain = bool(
                observation.screen.value == "IN_WORLD_IDLE"
                and marker_asset is not None
                and marker is not None
                and marker.confidence < marker_asset.threshold
            )
            uncertain = not observation.production_ready or marker_uncertain
            if not uncertain:
                self._uncertainty_active = False
                return
            if self._pending is not None:
                return
            if self._uncertainty_active:
                return
            self._uncertainty_active = True
            history = list(self._frames)
            # Keep seven predecessors and append three following observations.
            self._pending = history[-8:]
            self._pending_failure_index = len(self._pending) - 1
            self._pending_future_frames = 0
            self._pending_previous_stable_state = (
                self._last_stable_observation or {}
            ).get("screen")
            self._pending_context = dict(self._context)

    def _write_evidence_locked(
        self, records, *, failure_index: int, assets, calibration
    ) -> None:
        if not records:
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            failure = records[min(failure_index, len(records) - 1)]
            frame = failure.frame
            stem = (
                f"evidence-r{frame.runtime_generation}-w{frame.worker_generation}"
                f"-f{frame.sequence:08d}"
            )
            bundle = self.directory / stem
            bundle.mkdir(exist_ok=False)
            frame_dir = bundle / "frames"
            crop_dir = bundle / "crops"
            frame_dir.mkdir()
            crop_dir.mkdir()
            entries = []
            for index, item in enumerate(records):
                image = item.frame.image()
                filename = f"{index:02d}-{item.frame.sequence:08d}.png"
                # Evidence is lossless, but encoding must not stall fresh
                # observations or command ACKs while the worker lock is held.
                image.save(frame_dir / filename, format="PNG", compress_level=1)
                entries.append(
                    {
                        "file": f"frames/{filename}",
                        "frame_id": item.frame.frame_id,
                        "sequence": item.frame.sequence,
                        "captured_at": item.frame.captured_at,
                        "captured_monotonic": item.frame.captured_monotonic,
                        "source": item.frame.source,
                        "width": item.frame.width,
                        "height": item.frame.height,
                        "observation": item.observation,
                    }
                )
            image = frame.image()
            viewport = (frame.width, frame.height)
            from runtime_agent.gameworker.geometry import Viewport

            for detector_id, asset in assets.items():
                bounds = Viewport(*viewport).region(asset.expected_region)
                crop = image.crop(bounds)
                crop.save(crop_dir / f"{detector_id}.png", format="PNG", compress_level=1)
            detections = failure.observation.get("detections", [])
            thresholds = {
                detector_id: asset.threshold for detector_id, asset in assets.items()
            }
            scores = [
                {
                    "id": item.get("kind"),
                    "score": item.get("confidence"),
                    "detected": item.get("detected"),
                    "verified": item.get("verified"),
                    "threshold": thresholds.get(item.get("kind")),
                    "bounds": item.get("bounds"),
                }
                for item in detections
            ]
            ranked = sorted(
                (item for item in scores if isinstance(item["score"], (int, float))),
                key=lambda item: item["score"],
                reverse=True,
            )
            metadata = {
                "schema_version": 1,
                "event": "PERCEPTION_UNCERTAIN",
                "previous_stable_state": self._pending_previous_stable_state,
                "selected_state": failure.observation.get("screen"),
                "runner_up_detectors": ranked[:5],
                "detectors": scores,
                "thresholds": thresholds,
                "geometry": {"width": frame.width, "height": frame.height},
                "calibration": (
                    {
                        "profile_id": calibration.profile_id,
                        "version": calibration.version,
                        "verified": calibration.verified,
                    }
                    if calibration is not None
                    else {"profile_id": "unknown", "version": 0, "verified": False}
                ),
                "state": {
                    **self._pending_context,
                    "control_plane_decision": "not_available_in_runtime_agent",
                    "held_inputs": self._pending_context.get("held_inputs"),
                },
                "frames": entries,
            }
            (bundle / "manifest.json").write_text(
                json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8"
            )
            self._rotate_evidence()
        except (OSError, ValueError, TypeError):
            return

    def flush(self, *, assets=None, calibration=None) -> None:
        """Persist an incomplete evidence window if the worker is stopping."""
        with self._lock:
            if self._pending is None:
                return
            self._write_evidence_locked(
                self._pending,
                failure_index=self._pending_failure_index,
                assets=assets or {},
                calibration=calibration,
            )
            self._pending = None
            self._pending_future_frames = 0

    def _rotate_evidence(self) -> None:
        bundles = sorted(
            self.directory.glob("evidence-*"),
            key=lambda path: path.stat().st_mtime,
        )
        files = [
            path for bundle in bundles for path in bundle.rglob("*") if path.is_file()
        ]
        total = sum(path.stat().st_size for path in files)
        while bundles and (len(bundles) > self.ring_size or total > self.max_bytes):
            bundle = bundles.pop(0)
            for path in bundle.rglob("*"):
                if path.is_file():
                    try:
                        total -= path.stat().st_size
                        path.unlink()
                    except OSError:
                        pass
            for name in ("frames", "crops"):
                try:
                    (bundle / name).rmdir()
                except OSError:
                    pass
            try:
                bundle.rmdir()
            except OSError:
                pass

    def capture_error(
        self, frame, observation: dict, transitions: list[dict], sequence: int
    ) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                self.directory.mkdir(parents=True, exist_ok=True)
                stem = f"error-{sequence:08d}"
                image_path = self.directory / f"{stem}.png"
                json_path = self.directory / f"{stem}.json"
                frame.save(image_path, format="PNG", optimize=True)
                json_path.write_text(
                    json.dumps(
                        {"observation": observation, "transitions": transitions[-20:]},
                        ensure_ascii=True,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                self._artifacts.extend((image_path, json_path))
                self._rotate()
        except OSError:
            return

    def _rotate(self) -> None:
        existing = sorted(
            self.directory.glob("error-*"), key=lambda path: path.stat().st_mtime
        )
        total = sum(path.stat().st_size for path in existing)
        while existing and (
            len(existing) > self.ring_size * 2 or total > self.max_bytes
        ):
            path = existing.pop(0)
            try:
                total -= path.stat().st_size
                path.unlink()
            except OSError:
                pass
