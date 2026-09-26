from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

GRAPHICAL_ENVIRONMENT_KEYS = (
    "DISPLAY",
    "XAUTHORITY",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
)


@dataclass(frozen=True, slots=True)
class DisplayEnvironment:
    """Canonical graphical-session environment shared by runtime components."""

    display: str
    xdg_runtime_dir: str | None = None
    dbus_session_bus_address: str | None = None
    xauthority: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r":[0-9]{1,4}(?:\.[0-9]+)?", self.display):
            raise ValueError("DISPLAY must identify a local runtime X server")
        if self.xauthority and not PurePosixPath(self.xauthority).is_absolute():
            raise ValueError("XAUTHORITY must be an absolute runtime path")

    def as_environ(self) -> dict[str, str]:
        result = {"DISPLAY": self.display}
        if self.xauthority:
            result["XAUTHORITY"] = self.xauthority
        if self.xdg_runtime_dir:
            result["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        if self.dbus_session_bus_address:
            result["DBUS_SESSION_BUS_ADDRESS"] = self.dbus_session_bus_address
        return result
