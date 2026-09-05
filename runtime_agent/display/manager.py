from __future__ import annotations

import os

from runtime_agent.display.base import DisplayEnvironment, DisplayState
from runtime_agent.display.xvfb import XvfbDisplayBackend


class DisplayManager:
    """Owns graphical-environment state only; never Steam/DST lifecycle."""

    def __init__(
        self,
        *,
        backend: str,
        environment: DisplayEnvironment | None = None,
        xvfb_command: tuple[str, ...] = (
            "Xvfb",
            ":99",
            "-screen",
            "0",
            "1280x720x24",
            "-nolisten",
            "tcp",
        ),
    ):
        self.backend = backend
        self.environment = environment or DisplayEnvironment(
            os.getenv("DISPLAY", ":99"),
            os.getenv("XDG_RUNTIME_DIR"),
            os.getenv("DBUS_SESSION_BUS_ADDRESS"),
        )
        self._state = DisplayState.NOT_CONFIGURED
        self._diagnostics: dict = {}
        self._xvfb = (
            XvfbDisplayBackend(self.environment, xvfb_command)
            if backend == "xvfb"
            else None
        )

    def prepare(self) -> DisplayState:
        if self.backend != "xvfb":
            self._state, self._diagnostics = (
                DisplayState.UNSUPPORTED,
                {"reason": f"unsupported display backend: {self.backend}"},
            )
        else:
            self._state = DisplayState.STOPPED
        return self._state

    def start(self) -> DisplayState:
        if self._state == DisplayState.NOT_CONFIGURED:
            self.prepare()
        if self._state == DisplayState.UNSUPPORTED:
            return self._state
        assert self._xvfb is not None
        self._state, self._diagnostics = self._xvfb.start()
        return self._state

    def status(self) -> DisplayState:
        return self._state

    def ensure_ready(self) -> bool:
        return self.start() == DisplayState.READY

    def stop(self) -> DisplayState:
        if self._state != DisplayState.UNSUPPORTED:
            if self._xvfb:
                self._xvfb.stop()
            self._state = DisplayState.STOPPED
        return self._state

    def diagnostics(self) -> dict:
        return {
            "backend": self.backend,
            "state": self._state,
            "environment": self.environment.as_environ(),
            **self._diagnostics,
        }
