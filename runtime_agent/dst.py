import time
from datetime import datetime, timezone
from pathlib import Path


def is_ready(
    *, process_alive: bool, readiness_file: Path, started_at: str | None = None
) -> bool:
    """DST is ready only after an explicit launcher-created readiness marker."""
    if not process_alive or not started_at:
        return False
    try:
        started = datetime.fromisoformat(started_at)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        marker_mtime_ns = readiness_file.stat().st_mtime_ns
        return (
            readiness_file.is_file()
            and marker_mtime_ns > int(started.timestamp() * 1_000_000_000)
            and marker_mtime_ns <= time.time_ns()
        )
    except (OSError, ValueError):
        return False
