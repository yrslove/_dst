from __future__ import annotations

import re
import subprocess

from app.subprocess_env import sanitized_subprocess_environment


class LocalIncusExecutor:
    """Narrow node-local command boundary reserved for a future pull protocol."""

    def inspect(self, external_id: str) -> subprocess.CompletedProcess[str]:
        return self._run("info", self._target(external_id), timeout=30)

    def start(self, external_id: str) -> subprocess.CompletedProcess[str]:
        return self._run("start", self._target(external_id), timeout=90)

    def stop(self, external_id: str) -> subprocess.CompletedProcess[str]:
        return self._run("stop", self._target(external_id), "--timeout", "30", timeout=60)

    @staticmethod
    def _target(external_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,94}", external_id):
            raise ValueError("invalid Incus runtime identifier")
        return external_id

    @staticmethod
    def _run(*args: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["incus", *args],
            env=sanitized_subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
