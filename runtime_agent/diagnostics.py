from __future__ import annotations

import os
from pathlib import Path


def graphical_session_health() -> dict:
    display = os.getenv("DISPLAY")
    if not display:
        return {"healthy": False, "display": None, "reason": "DISPLAY is not set"}
    socket_name = display.rsplit(":", 1)[-1].split(".", 1)[0]
    socket_path = Path("/tmp/.X11-unix") / f"X{socket_name}"
    return {
        "healthy": socket_path.exists(),
        "display": display,
        "socket": str(socket_path),
        "reason": None if socket_path.exists() else "X11 socket is absent",
    }
