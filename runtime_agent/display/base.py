from __future__ import annotations

from enum import StrEnum

from app.runtime.display import DisplayEnvironment

__all__ = ["DisplayEnvironment", "DisplayState"]


class DisplayState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    ERROR = "ERROR"
    UNSUPPORTED = "UNSUPPORTED"
