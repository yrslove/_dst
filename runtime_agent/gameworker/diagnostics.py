from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from threading import Lock


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
