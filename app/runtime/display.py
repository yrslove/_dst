from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DisplayEnvironment:
    """Canonical graphical-session environment shared by runtime components."""

    display: str
    xdg_runtime_dir: str | None = None
    dbus_session_bus_address: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r":[0-9]{1,4}(?:\.[0-9]+)?", self.display):
            raise ValueError("DISPLAY must identify a local runtime X server")

    def as_environ(self) -> dict[str, str]:
        result = {"DISPLAY": self.display}
        if self.xdg_runtime_dir:
            result["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        if self.dbus_session_bus_address:
            result["DBUS_SESSION_BUS_ADDRESS"] = self.dbus_session_bus_address
        return result
