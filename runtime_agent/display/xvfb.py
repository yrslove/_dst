from __future__ import annotations

import os
from pathlib import Path

from runtime_agent.display.base import DisplayEnvironment, DisplayState
from runtime_agent.process_supervisor import ProcessSupervisor


class XvfbDisplayBackend:
    """Linux-only readiness adapter; actual process ownership is delegated later."""

    def __init__(
        self,
        environment: DisplayEnvironment,
        command: tuple[str, ...],
        *,
        readiness_timeout_seconds: float = 15,
    ):
        self.environment = environment
        self.supervisor = ProcessSupervisor(
            "display", command, before_start=self._assert_display_unallocated
        )
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._terminal_error = False
        self._terminal_reason = "Xvfb readiness timeout"

    def adopt(self, pid: int, start_ticks: int) -> None:
        if not self._socket().exists():
            raise RuntimeError("cannot adopt Xvfb without its display socket")
        self.supervisor.adopt(pid, start_ticks)

    def _socket(self) -> Path:
        number = self.environment.display.rsplit(":", 1)[-1].split(".", 1)[0]
        return Path("/tmp/.X11-unix") / f"X{number}"

    def _assert_display_unallocated(self) -> None:
        if self._socket().exists():
            raise OSError("display socket is already allocated")

    def start(self) -> tuple[DisplayState, dict]:
        if os.name != "posix":
            return DisplayState.UNSUPPORTED, {
                "reason": "Xvfb is supported only on Linux",
                "validation": "UNVALIDATED_ON_REAL_NODE",
            }
        if self._terminal_error:
            return DisplayState.ERROR, {
                "reason": self._terminal_reason,
                "process": self.supervisor.status.as_dict(),
            }
        if not self.supervisor.status.desired and self._socket().exists():
            self._terminal_error = True
            self._terminal_reason = "display socket is already allocated"
            return DisplayState.ERROR, {
                "socket": str(self._socket()),
                "reason": self._terminal_reason,
                "process": self.supervisor.status.as_dict(),
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
        socket = self._socket()
        if self.supervisor.status.exhausted:
            return DisplayState.ERROR, {
                "socket": str(socket),
                "reason": "Xvfb restart budget exhausted",
                "process": self.supervisor.status.as_dict(),
            }
        if (
            self.supervisor.alive
            and not socket.exists()
            and self.supervisor.running_for >= self.readiness_timeout_seconds
        ):
            self.supervisor.shutdown(timeout=3)
            self._terminal_error = True
            self._terminal_reason = "Xvfb readiness timeout"
            return DisplayState.ERROR, {
                "socket": str(socket),
                "reason": "Xvfb readiness timeout",
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

    def stop(self, timeout: float = 10) -> None:
        self.supervisor.shutdown(timeout=timeout)
