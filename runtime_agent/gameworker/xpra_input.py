"""One private, process-owned xpra input channel. No browser or TCP listener."""
from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from pathlib import Path

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS, DisplayEnvironment
from app.subprocess_env import sanitized_subprocess_environment


class InputError(RuntimeError):
    code = "WORKER_INPUT_FAILED"


class XpraInputDriver:
    # Measured against DST's bundled SDL: short pulses are not reliable. These
    # bounded transport timings are intentionally hidden from behavior policies.
    settle_seconds = 0.5
    press_seconds = 0.5

    def __init__(self, environment: DisplayEnvironment, *, timeout: float = 2.0):
        if timeout <= 0:
            raise ValueError("input timeout must be positive")
        self.environment = environment
        self.timeout = timeout
        self._closed = False
        self._buffer = bytearray()
        env = sanitized_subprocess_environment()
        for name in GRAPHICAL_ENVIRONMENT_KEYS:
            env.pop(name, None)
        env.update(environment.as_environ())
        self._process = subprocess.Popen(
            ["/usr/bin/python3", str(Path(__file__).with_name("xpra_bridge.py"))],
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, close_fds=True, shell=False,
        )
        self._selector = selectors.DefaultSelector()
        self._selector.register(self._process.stdout, selectors.EVENT_READ)
        try:
            ready = self._read(10.0)
            if ready.get("ready") != 1:
                raise InputError("private xpra input channel unavailable")
        except Exception:
            self.close()
            raise

    def _read(self, timeout):
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._selector.select(remaining):
                raise InputError("xpra input operation timed out")
            data = os.read(self._process.stdout.fileno(), 4096)
            if not data or len(self._buffer) + len(data) > 8192:
                raise InputError("xpra input channel closed or oversized")
            self._buffer.extend(data)
        line, _, remainder = self._buffer.partition(b"\n")
        self._buffer = bytearray(remainder)
        try:
            reply = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise InputError("xpra input channel closed or malformed") from exc
        if not reply.get("ok"):
            reason = reply.get("error")
            raise InputError(f"xpra input channel rejected operation: {reason}")
        return reply

    def _call(self, operation, *args):
        if self._closed:
            raise InputError("xpra input channel is closed")
        try:
            self._process.stdin.write(json.dumps([operation, *args]).encode() + b"\n")
            return self._read(self.timeout)
        except (OSError, InputError) as exc:
            # EOF makes the bridge release inputs before destroying its server.
            self.close()
            raise InputError(f"xpra input operation failed: {exc}") from exc

    def mouse_move(self, x, y):
        self._call("move", x, y)

    def focus_game_at_pointer(self):
        self._call("focus")

    def mouse_down(self, button):
        self._call("button", button, True)

    def mouse_up(self, button):
        self._call("button", button, False)

    def key_down(self, key):
        self._call("key", key, True)

    def key_up(self, key):
        self._call("key", key, False)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._process.stdin.close()
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        self._selector.close()
        self._process.stdout.close()
