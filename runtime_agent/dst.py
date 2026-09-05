from datetime import datetime
from pathlib import Path


def is_ready(
    *, process_alive: bool, readiness_file: Path, started_at: str | None = None
) -> bool:
    """DST is ready only after an explicit launcher-created readiness marker."""
    if not process_alive or not started_at:
        return False
    try:
        return (
            readiness_file.is_file()
            and readiness_file.stat().st_mtime
            >= datetime.fromisoformat(started_at).timestamp()
        )
    except OSError:
        return False
