from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import time
import uuid

from app.providers.base import ProviderError, RuntimeDescriptor, RuntimeProvider
from app.providers.view.base import (
    RuntimeViewProvider,
    ViewBackendUnavailable,
    ViewStatus,
    ViewUnavailable,
)
from app.runtime.display import DisplayEnvironment
from app.subprocess_env import sanitized_subprocess_environment

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SAFE_DISPLAY = re.compile(r":[0-9]{1,4}(?:\.[0-9]+)?")

# Ubuntu 24.04's xpra 3.1.5 can serve HTML while rejecting its own `xpra stop`
# connection. Only terminate the exact shadow process started by this provider.
_STOP_SHADOW = """
import os, pathlib, signal, socket, sys, time
display, port = sys.argv[1:]
expected = '--bind-tcp=127.0.0.1:' + port
matches = []
for path in pathlib.Path('/proc').glob('[0-9]*/cmdline'):
    try:
        args = path.read_bytes().split(b'\\0')
        args = [item.decode() for item in args if item]
    except (OSError, UnicodeError):
        continue
    if (len(args) >= 4 and pathlib.Path(args[1]).name == 'xpra'
            and args[2:4] == ['shadow', display]
            and expected in args
            and '--socket-dir=/run/dst-runtime/xpra' in args):
        matches.append(int(path.parent.name))
if len(matches) > 1:
    sys.exit(1)
if matches:
    os.kill(matches[0], signal.SIGTERM)
for _ in range(30):
    with socket.socket() as connection:
        connection.settimeout(0.1)
        if connection.connect_ex(('127.0.0.1', int(port))) != 0:
            sys.exit(0)
    time.sleep(0.1)
sys.exit(1)
"""


class XpraRuntimeViewProvider(RuntimeViewProvider):
    """Ephemeral xpra shadow + loopback-only Incus proxy.

    The xpra process shadows the same DISPLAY used by Steam/DST. The browser-facing
    address never leaves loopback; FastAPI's authenticated adapter proxies it.
    """

    name = "xpra"
    container_port = 14500

    def __init__(
        self, runtime_provider: RuntimeProvider, *, incus_remote: str = "local"
    ):
        self.runtime_provider = runtime_provider
        self.incus_remote = incus_remote

    def _require_host(self) -> None:
        if os.name != "posix" or shutil.which("incus") is None:
            raise ViewBackendUnavailable(
                "xpra/Incus remote view is unavailable on this host"
            )

    def _target(self, external_id: str, remote: str | None) -> str:
        selected = remote or self.incus_remote
        if not _SAFE_NAME.fullmatch(external_id) or (
            selected and not _SAFE_NAME.fullmatch(selected)
        ):
            raise ViewUnavailable("invalid runtime view target")
        target = (
            external_id if selected in {"", "local"} else f"{selected}:{external_id}"
        )
        if len(target) > 96:
            raise ViewUnavailable("runtime view target name is too long")
        return target

    @staticmethod
    def _free_loopback_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    @staticmethod
    def _encode(payload: dict) -> str:
        return (
            base64.urlsafe_b64encode(
                json.dumps(payload, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )

    @staticmethod
    def _runtime_argv(
        display: DisplayEnvironment, *command: str
    ) -> tuple[str, ...]:
        environment = [f"DISPLAY={display.display}"]
        if display.xauthority:
            environment.append(f"XAUTHORITY={display.xauthority}")
        if display.xdg_runtime_dir:
            environment.append(f"XDG_RUNTIME_DIR={display.xdg_runtime_dir}")
        if display.dbus_session_bus_address:
            environment.append(
                f"DBUS_SESSION_BUS_ADDRESS={display.dbus_session_bus_address}"
            )
        return ("/usr/bin/env", *environment, *command)

    @staticmethod
    def _decode(value: str) -> dict:
        try:
            padding = "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(value + padding))
        except Exception as exc:
            raise ViewUnavailable("invalid xpra backend session identifier") from exc
        if not isinstance(payload, dict):
            raise ViewUnavailable("invalid xpra backend session identifier")
        return payload

    @classmethod
    def _validated_payload(cls, value: str) -> dict:
        payload = cls._decode(value)
        target = payload.get("target")
        device = payload.get("device")
        display = payload.get("display")
        port = payload.get("port")
        if (
            not isinstance(target, str)
            or not _SAFE_NAME.fullmatch(target)
            or not isinstance(device, str)
            or not device.startswith("view-")
            or not _SAFE_NAME.fullmatch(device)
            or not isinstance(display, str)
            or not _SAFE_DISPLAY.fullmatch(display)
            or not isinstance(port, int)
            or isinstance(port, bool)
            or not 1 <= port <= 65535
        ):
            raise ViewUnavailable("invalid xpra backend session identifier")
        return payload

    def _incus(
        self, *args: str, allow_failure: bool = False
    ) -> subprocess.CompletedProcess:
        self._require_host()
        try:
            result = subprocess.run(
                ["incus", *args],
                env=sanitized_subprocess_environment(),
                timeout=20,
                capture_output=True,
                text=True,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ViewBackendUnavailable("Incus view adapter command failed") from exc
        if result.returncode != 0 and not allow_failure:
            raise ViewUnavailable("Incus view adapter rejected session setup")
        return result

    def prepare_runtime(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> None:
        self._require_host()
        selected_remote = runtime.incus_remote or self.incus_remote
        if selected_remote not in {"", "local"}:
            raise ViewBackendUnavailable(
                "xpra loopback transport currently requires a control plane colocated with the Incus Node"
            )
        if runtime.provider != "incus" or not _SAFE_DISPLAY.fullmatch(display.display):
            raise ViewBackendUnavailable(
                "xpra backend requires an Incus runtime and a canonical X display"
            )
        try:
            xpra_probe = self.runtime_provider.execute(
                runtime, ("xpra", "--version"), timeout=10
            )
            display_probe = self.runtime_provider.execute(
                runtime,
                (
                    "test",
                    "-S",
                    f"/tmp/.X11-unix/X{display.display[1:].split('.')[0]}",
                ),
                timeout=10,
            )
        except ProviderError as exc:
            raise ViewBackendUnavailable("xpra runtime probe failed") from exc
        if any(
            int(probe.raw.get("exit_code", 1)) != 0
            for probe in (xpra_probe, display_probe)
        ):
            raise ViewBackendUnavailable(
                "xpra or the runtime display socket is unavailable"
            )

    def _cleanup_backend(self, target: str, device: str, display: str) -> None:
        failure: ViewUnavailable | None = None
        try:
            removed = self._incus(
                "config", "device", "remove", target, device, allow_failure=True
            )
            if not self._idempotent_cleanup_result(removed):
                failure = ViewUnavailable(
                    "Incus view proxy cleanup could not be confirmed"
                )
        except ViewUnavailable as exc:
            failure = exc
        try:
            stopped = self._incus(
                "exec",
                target,
                "--",
                "/usr/bin/python3",
                "-c",
                _STOP_SHADOW,
                display,
                str(self.container_port),
                allow_failure=True,
            )
            if stopped.returncode != 0:
                failure = failure or ViewUnavailable(
                    "xpra shadow cleanup could not be confirmed"
                )
        except ViewUnavailable as exc:
            failure = failure or exc
        if failure:
            raise failure

    @staticmethod
    def _idempotent_cleanup_result(result: subprocess.CompletedProcess) -> bool:
        if result.returncode == 0:
            return True
        message = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return any(
            marker in message
            for marker in (
                "not found",
                "does not exist",
                "doesn't exist",
                "not running",
                "no matching session",
            )
        )

    def reserve_session(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> str:
        self._require_host()
        selected_remote = runtime.incus_remote or self.incus_remote
        if selected_remote not in {"", "local"}:
            raise ViewBackendUnavailable(
                "xpra loopback transport currently requires a control plane colocated with the Incus Node"
            )
        if runtime.provider != "incus" or not _SAFE_DISPLAY.fullmatch(display.display):
            raise ViewBackendUnavailable(
                "xpra backend requires an Incus runtime and a canonical X display"
            )
        host_port = self._free_loopback_port()
        nonce = uuid.uuid4().hex[:12]
        device = f"view-{nonce}"
        target = self._target(runtime.external_id, runtime.incus_remote)
        backend_id = self._encode(
            {
                "target": target,
                "device": device,
                "port": host_port,
                "display": display.display,
                "nonce": nonce,
            }
        )
        if len(backend_id) > 255:
            raise ViewUnavailable(
                "xpra backend session identifier exceeds storage limit"
            )
        return backend_id

    def create_session(
        self,
        runtime: RuntimeDescriptor,
        display: DisplayEnvironment,
        *,
        backend_session_id: str | None = None,
    ) -> ViewStatus:
        backend_id = backend_session_id or self.reserve_session(runtime, display)
        payload = self._validated_payload(backend_id)
        target = self._target(runtime.external_id, runtime.incus_remote)
        if payload["target"] != target or payload["display"] != display.display:
            raise ViewUnavailable("xpra reservation does not match runtime session")
        host_port = int(payload["port"])
        device = str(payload["device"])
        try:
            installed = self.runtime_provider.execute(
                runtime,
                ("install", "-d", "-m", "0700", "/run/dst-runtime/xpra"),
                timeout=10,
            )
            started = self.runtime_provider.execute(
                runtime,
                self._runtime_argv(
                    display,
                    "xpra",
                    "shadow",
                    display.display,
                    "--daemon=yes",
                    "--exit-with-client=no",
                    f"--bind-tcp=127.0.0.1:{self.container_port}",
                    "--html=on",
                    "--auth=none",
                    "--tcp-auth=none",
                    "--socket-dir=/run/dst-runtime/xpra",
                ),
                timeout=20,
            )
        except ProviderError as exc:
            self._cleanup_or_defer(target, device, display.display, backend_id)
            raise ViewBackendUnavailable(
                "xpra failed to shadow the runtime display"
            ) from exc
        if any(
            int(result.raw.get("exit_code", 1)) != 0
            for result in (installed, started)
        ):
            self._cleanup_or_defer(target, device, display.display, backend_id)
            raise ViewBackendUnavailable("xpra failed to shadow the runtime display")
        try:
            self._incus(
                "config",
                "device",
                "add",
                target,
                device,
                "proxy",
                f"listen=tcp:127.0.0.1:{host_port}",
                f"connect=tcp:127.0.0.1:{self.container_port}",
            )
        except Exception:
            self._cleanup_or_defer(target, device, display.display, backend_id)
            raise
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=0.25):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            self._cleanup_or_defer(target, device, display.display, backend_id)
            raise ViewBackendUnavailable("xpra loopback transport did not become ready")
        return ViewStatus("ACTIVE", backend_id, upstream=("127.0.0.1", host_port))

    def _cleanup_or_defer(
        self, target: str, device: str, display: str, backend_id: str
    ) -> None:
        try:
            self._cleanup_backend(target, device, display)
        except ViewUnavailable as exc:
            raise ViewBackendUnavailable(
                "xpra setup failed and backend cleanup is pending",
                backend_session_id=backend_id,
            ) from exc

    def status(self, backend_session_id: str) -> ViewStatus:
        payload = self._validated_payload(backend_session_id)
        self._require_host()
        try:
            with socket.create_connection(
                ("127.0.0.1", int(payload["port"])), timeout=1
            ):
                return ViewStatus(
                    "ACTIVE",
                    backend_session_id,
                    upstream=("127.0.0.1", int(payload["port"])),
                )
        except (OSError, KeyError, TypeError, ValueError):
            return ViewStatus(
                "ERROR",
                backend_session_id,
                "REMOTE_VIEW_BACKEND_UNAVAILABLE",
                "xpra loopback transport is unavailable",
            )

    def close_session(self, backend_session_id: str) -> None:
        payload = self._validated_payload(backend_session_id)
        self._cleanup_backend(payload["target"], payload["device"], payload["display"])

    def cleanup_expired_sessions(self, backend_session_ids: list[str]) -> None:
        for value in backend_session_ids:
            try:
                self.close_session(value)
            except ViewUnavailable:
                continue
