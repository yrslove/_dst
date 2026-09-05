from __future__ import annotations

import os
from pathlib import Path

from runtime_agent.display.base import DisplayEnvironment, DisplayState
from runtime_agent.process_supervisor import ProcessSupervisor


class XvfbDisplayBackend:
    """Linux-only readiness adapter; actual process ownership is delegated later."""

    def __init__(self, environment: DisplayEnvironment, command: tuple[str, ...]):
        self.environment = environment
        self.supervisor = ProcessSupervisor("display", command)

    def start(self) -> tuple[DisplayState, dict]:
        if os.name != "posix":
            return DisplayState.UNSUPPORTED, {
                "reason": "Xvfb is supported only on Linux",
                "validation": "UNVALIDATED_ON_REAL_NODE",
            }
        self.supervisor.request_start()
        self.supervisor.tick()
        return self.probe()

    def probe(self) -> tuple[DisplayState, dict]:
        if os.name != "posix":
            return DisplayState.UNSUPPORTED, {
                "reason": "Xvfb is supported only on Linux",
                "validation": "UNVALIDATED_ON_REAL_NODE",
            }
        number = self.environment.display.rsplit(":", 1)[-1].split(".", 1)[0]
        socket = Path("/tmp/.X11-unix") / f"X{number}"
        if self.supervisor.status.exhausted:
            return DisplayState.ERROR, {
                "socket": str(socket),
                "reason": "Xvfb restart budget exhausted",
                "process": self.supervisor.status.as_dict(),
            }
        if self.supervisor.alive and socket.exists():
            return DisplayState.READY, {
                "socket": str(socket),
                "probe": "process_and_socket",
                "process": self.supervisor.status.as_dict(),
            }
        return DisplayState.STARTING, {
            "socket": str(socket),
            "reason": "waiting for Xvfb socket",
            "process": self.supervisor.status.as_dict(),
        }

    def stop(self) -> None:
        self.supervisor.shutdown()
