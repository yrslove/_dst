"""One private, process-owned xpra input channel. No browser or TCP listener."""
from __future__ import annotations

import json
import logging
import os
import selectors
import subprocess
import time
from pathlib import Path

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS, DisplayEnvironment
from app.subprocess_env import sanitized_subprocess_environment

logger = logging.getLogger("runtime_agent.gameworker.input")


class InputError(RuntimeError):
    code = "WORKER_INPUT_FAILED"


class InputTransportError(InputError):
    """The private bridge or its server connection was lost."""


class XpraInputDriver:
    # Measured against DST's bundled SDL: short pulses are not reliable. These
    # bounded transport timings are intentionally hidden from behavior policies.
    settle_seconds = 0.5
    press_seconds = 0.5

    @property
    def closed(self) -> bool:
        return self._closed

    def __init__(self, environment: DisplayEnvironment, *, timeout: float = 2.0):
        if timeout <= 0:
            raise ValueError("input timeout must be positive")
        self.environment = environment
        self.timeout = timeout
        self._point = [0, 0]
        self._closed = False
        self._buffer = bytearray()
        self._process = None
        self._selector = None
        self._start_bridge()

    def _start_bridge(self):
        env = sanitized_subprocess_environment()
        for name in GRAPHICAL_ENVIRONMENT_KEYS:
            env.pop(name, None)
        env.update(self.environment.as_environ())
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
            if not data:
                raise InputTransportError("xpra input bridge reached EOF")
            if len(self._buffer) + len(data) > 8192:
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
            if reason in {
                "BlockingIOError", "BrokenPipeError", "ChannelUnavailable",
                "ConnectionAbortedError",
                "ConnectionResetError", "EOFError", "TimeoutError",
            }:
                detail = reply.get("detail")
                suffix = f": {detail[:160]}" if isinstance(detail, str) else ""
                raise InputTransportError(
                    f"xpra input server connection lost: {reason}{suffix}"
                )
            raise InputError(f"xpra input channel rejected operation: {reason}")
        return reply

    @staticmethod
    def _safe_to_retry(operation, args):
        return operation in {"move", "focus"} or (
            operation == "button" and len(args) > 1 and args[1] is False
        ) or (operation == "key" and len(args) > 1 and args[1] is False)

    def _call_once(self, operation, *args):
        if self._closed:
            raise InputTransportError("xpra input channel is closed")
        try:
            self._process.stdin.write(json.dumps([operation, *args]).encode() + b"\n")
            result = self._read(self.timeout)
            if operation == "button" and len(args) > 1 and args[1] is False:
                logger.warning(
                    "xpra_button_ack %s",
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                )
            elif operation == "focus":
                logger.warning(
                    "xpra_focus_ack %s",
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                )
            elif operation in {"move", "focus", "button"}:
                logger.info(
                    "xpra_input_ack %s",
                    json.dumps(result, sort_keys=True, separators=(",", ":")),
                )
            return result
        except InputTransportError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise InputTransportError(
                f"xpra input operation failed: {type(exc).__name__}"
            ) from exc
        except InputError:
            self.close()
            raise

    def _reconnect(self, operation, args):
        logger.warning(
            "xpra_input_reconnect operation=%s retry=1",
            operation,
        )
        self._closed = False
        self._buffer.clear()
        self._start_bridge()
        # A new bridge starts with no local pointer state. Reestablish only
        # the last known position before an idempotent focus/release retry.
        if operation != "move":
            self._call_once("move", *self._point)
        return self._call_once(operation, *args)

    def _call(self, operation, *args):
        if operation == "move":
            self._point = [args[0], args[1]]
        try:
            return self._call_once(operation, *args)
        except InputTransportError as exc:
            if self._safe_to_retry(operation, args):
                try:
                    return self._reconnect(operation, args)
                except (OSError, InputError) as retry_exc:
                    self.close()
                    raise InputError(
                        f"xpra input operation failed after reconnect: {retry_exc}"
                    ) from retry_exc
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
        try:
            self._process.stdin.close()
        except OSError:
            pass
        try:
            self._process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)
        if self._selector is not None:
            self._selector.close()
        if self._process is not None and self._process.stdout is not None:
            self._process.stdout.close()
