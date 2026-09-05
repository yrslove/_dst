from __future__ import annotations

import base64
import json
import os
import re
import shutil
import socket
import subprocess
import uuid

from app.providers.base import ProviderError, RuntimeDescriptor, RuntimeProvider
from app.providers.view.base import (
    RuntimeViewProvider,
    ViewBackendUnavailable,
    ViewStatus,
    ViewUnavailable,
)
from app.runtime.display import DisplayEnvironment

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SAFE_DISPLAY = re.compile(r":[0-9]{1,4}(?:\.[0-9]+)?")


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
    def _decode(value: str) -> dict:
        try:
            padding = "=" * (-len(value) % 4)
            payload = json.loads(base64.urlsafe_b64decode(value + padding))
        except Exception as exc:
            raise ViewUnavailable("invalid xpra backend session identifier") from exc
        if not isinstance(payload, dict):
            raise ViewUnavailable("invalid xpra backend session identifier")
        return payload

    def _incus(
        self, *args: str, allow_failure: bool = False
    ) -> subprocess.CompletedProcess:
        self._require_host()
        try:
            result = subprocess.run(
                ["incus", *args],
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
            probe = self.runtime_provider.execute(
                runtime,
                (
                    "sh",
                    "-lc",
                    f"command -v xpra >/dev/null && test -S /tmp/.X11-unix/X{display.display[1:].split('.')[0]}",
                ),
                timeout=10,
            )
        except ProviderError as exc:
            raise ViewBackendUnavailable("xpra runtime probe failed") from exc
        if int(probe.raw.get("exit_code", 1)) != 0:
            raise ViewBackendUnavailable(
                "xpra or the runtime display socket is unavailable"
            )

    def create_session(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> ViewStatus:
        host_port = self._free_loopback_port()
        nonce = uuid.uuid4().hex[:12]
        device = f"view-{nonce}"
        target = self._target(runtime.external_id, runtime.incus_remote)
        launch = (
            "install -d -m 0700 /run/dst-runtime/xpra && "
            f"xpra shadow {display.display} --daemon=yes --exit-with-client=no "
            f"--bind-tcp=127.0.0.1:{self.container_port} --html=on --auth=none --tcp-auth=none "
            "--socket-dir=/run/dst-runtime/xpra"
        )
        command = ("sh", "-lc", launch)
        try:
            started = self.runtime_provider.execute(runtime, command, timeout=20)
        except ProviderError as exc:
            raise ViewBackendUnavailable(
                "xpra failed to shadow the runtime display"
            ) from exc
        if int(started.raw.get("exit_code", 1)) != 0:
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
            self._incus(
                "exec",
                target,
                "--",
                "xpra",
                "stop",
                display.display,
                allow_failure=True,
            )
            raise
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
            self._incus(
                "config", "device", "remove", target, device, allow_failure=True
            )
            self._incus(
                "exec",
                target,
                "--",
                "xpra",
                "stop",
                display.display,
                allow_failure=True,
            )
            raise ViewUnavailable(
                "xpra backend session identifier exceeds storage limit"
            )
        return ViewStatus("ACTIVE", backend_id, upstream=("127.0.0.1", host_port))

    def status(self, backend_session_id: str) -> ViewStatus:
        payload = self._decode(backend_session_id)
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
        payload = self._decode(backend_session_id)
        target, device, display = (
            payload.get("target"),
            payload.get("device"),
            payload.get("display"),
        )
        if not all(isinstance(item, str) for item in (target, device, display)):
            raise ViewUnavailable("invalid xpra backend session identifier")
        self._incus("config", "device", "remove", target, device, allow_failure=True)
        self._incus("exec", target, "--", "xpra", "stop", display, allow_failure=True)

    def cleanup_expired_sessions(self, backend_session_ids: list[str]) -> None:
        for value in backend_session_ids:
            try:
                self.close_session(value)
            except ViewUnavailable:
                continue
