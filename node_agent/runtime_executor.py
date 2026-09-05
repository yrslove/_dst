from __future__ import annotations

import subprocess


class LocalIncusExecutor:
    """Narrow node-local command boundary reserved for a future pull protocol."""

    def inspect(self, external_id: str) -> subprocess.CompletedProcess[str]:
        return self._run("info", external_id, timeout=30)

    def start(self, external_id: str) -> subprocess.CompletedProcess[str]:
        return self._run("start", external_id, timeout=90)

    def stop(self, external_id: str) -> subprocess.CompletedProcess[str]:
        return self._run("stop", external_id, "--timeout", "30", timeout=60)

    @staticmethod
    def _run(*args: str, timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["incus", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
