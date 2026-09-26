from __future__ import annotations

import json
import subprocess

from app.subprocess_env import sanitized_subprocess_environment


def incus_available() -> bool:
    try:
        result = subprocess.run(
            ["incus", "info"],
            env=sanitized_subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def active_runtime_count() -> int:
    try:
        result = subprocess.run(
            ["incus", "list", "--format", "json"],
            env=sanitized_subprocess_environment(),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            return 0
        instances = json.loads(result.stdout or "[]")
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return 0
    return sum(
        1
        for instance in instances
        if str(instance.get("status", "")).upper() == "RUNNING"
        and str(instance.get("name", "")).startswith("dst-")
    )
