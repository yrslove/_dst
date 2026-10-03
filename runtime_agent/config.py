from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from runtime_agent.gameworker.config import WorkerConfig


def _strict_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class RuntimeAgentSettings:
    control_plane_url: str
    runtime_id: int
    runtime_token: str = field(repr=False)
    account_id: int
    runtime_generation: int = 1
    runtime_image_version: str = "unknown"
    node_id: int = 0
    heartbeat_seconds: float = 5
    request_timeout_seconds: float = 5
    agent_version: str = "1.0.0"
    protocol_version: int = 1
    auto_launch_steam: bool = False
    auto_launch_dst: bool = False
    safe_idle_world_profile: bool = False
    display_backend: str = "xvfb"
    display: str = ":99"
    xdg_runtime_dir: str | None = None
    dbus_session_bus_address: str | None = None
    xauthority: str | None = None
    worker_plugin: str = "noop"
    xvfb_command: tuple[str, ...] = (
        "Xvfb",
        ":99",
        "-screen",
        "0",
        "1280x720x24",
        "-nolisten",
        "tcp",
    )
    steam_command: tuple[str, ...] = ("steam", "-silent")
    dst_command: tuple[str, ...] = ("steam", "-applaunch", "322330")
    steam_ready_file: Path = Path("/run/dst-runtime/steam.ready")
    steam_needs_login_file: Path = Path("/run/dst-runtime/steam.needs-login")
    dst_ready_file: Path = Path("/run/dst-runtime/dst.ready")
    max_restarts: int = 3
    restart_backoff_seconds: float = 2
    display_readiness_timeout_seconds: float = 15
    steam_readiness_timeout_seconds: float = 120
    dst_readiness_timeout_seconds: float = 300
    worker_config: WorkerConfig | None = None

    def validate(self) -> None:
        parsed = urlsplit(self.control_plane_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("CONTROL_PLANE_URL must be an absolute HTTP(S) URL")
        if (
            self.runtime_id < 1
            or self.account_id < 1
            or self.runtime_generation < 1
            or self.node_id < 1
        ):
            raise ValueError(
                "RUNTIME_ID, ACCOUNT_ID, RUNTIME_GENERATION, and NODE_ID "
                "must be positive"
            )
        if self.heartbeat_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("runtime heartbeat and request timeouts must be positive")
        if self.display_backend != "xvfb":
            raise ValueError("DISPLAY_BACKEND must be xvfb")
        if not re.fullmatch(r":[0-9]{1,4}(?:\.[0-9]+)?", self.display):
            raise ValueError("DISPLAY must identify a local X server")
        if self.xauthority and not PurePosixPath(self.xauthority).is_absolute():
            raise ValueError("XAUTHORITY must be an absolute runtime path")
        if not self.xvfb_command or not self.steam_command or not self.dst_command:
            raise ValueError("runtime process commands must not be empty")
        if any(
            "\x00" in item
            for command in (self.xvfb_command, self.steam_command, self.dst_command)
            for item in command
        ):
            raise ValueError("runtime process commands contain a NUL byte")
        if self.xauthority:
            try:
                auth_index = self.xvfb_command.index("-auth")
                configured_auth = self.xvfb_command[auth_index + 1]
            except (ValueError, IndexError) as exc:
                raise ValueError(
                    "XVFB_COMMAND must use the configured XAUTHORITY"
                ) from exc
            if configured_auth != self.xauthority:
                raise ValueError("XVFB_COMMAND and XAUTHORITY do not match")
        if self.max_restarts < 0 or self.restart_backoff_seconds < 0:
            raise ValueError("process restart settings must not be negative")
        if min(
            self.display_readiness_timeout_seconds,
            self.steam_readiness_timeout_seconds,
            self.dst_readiness_timeout_seconds,
        ) <= 0:
            raise ValueError("readiness timeouts must be positive")
        if self.auto_launch_dst and not self.auto_launch_steam:
            raise ValueError("AUTO_LAUNCH_DST requires AUTO_LAUNCH_STEAM")
        if self.auto_launch_steam and self.steam_command == ("steam", "-silent"):
            raise ValueError(
                "STEAM_COMMAND must be a readiness-aware launcher when auto-launch is enabled"
            )
        if (
            self.auto_launch_dst
            and self.dst_command[0].lower().endswith("steam")
            and "-applaunch" in self.dst_command
        ):
            raise ValueError(
                "DST_COMMAND must remain attached to the managed game process; "
                "steam -applaunch is not supervisable"
            )
        paths = {
            self.steam_ready_file,
            self.steam_needs_login_file,
            self.dst_ready_file,
        }
        if len(paths) != 3 or any(
            not PurePosixPath(path.as_posix()).is_absolute() for path in paths
        ):
            raise ValueError("runtime marker paths must be distinct absolute paths")

    @classmethod
    def from_env(cls) -> RuntimeAgentSettings:
        url = os.getenv("CONTROL_PLANE_URL") or os.getenv("ORCHESTRATOR_URL", "")
        runtime_id = int(os.getenv("RUNTIME_ID", "0"))
        token = os.getenv("RUNTIME_TOKEN", "")
        if not url or runtime_id < 1 or not token:
            raise ValueError(
                "CONTROL_PLANE_URL, RUNTIME_ID, and RUNTIME_TOKEN are required"
            )
        worker_plugin = os.getenv("WORKER_PLUGIN", "noop").lower()
        display = os.getenv("DISPLAY", ":99")
        xauthority = os.getenv("XAUTHORITY") or None
        raw_xvfb_command = os.getenv("XVFB_COMMAND")
        xvfb_command = (
            tuple(shlex.split(raw_xvfb_command))
            if raw_xvfb_command
            else (
                "Xvfb",
                display,
                "-screen",
                "0",
                "1280x720x24",
                "-nolisten",
                "tcp",
                *(("-auth", xauthority) if xauthority else ()),
            )
        )
        settings = cls(
            control_plane_url=url.rstrip("/"),
            runtime_id=runtime_id,
            runtime_token=token,
            account_id=int(os.getenv("ACCOUNT_ID", "0")),
            runtime_generation=int(os.getenv("RUNTIME_GENERATION", "1")),
            runtime_image_version=os.getenv("RUNTIME_IMAGE_VERSION", "unknown"),
            node_id=int(os.getenv("NODE_ID", "0")),
            heartbeat_seconds=float(os.getenv("RUNTIME_HEARTBEAT_SECONDS", "5")),
            request_timeout_seconds=float(
                os.getenv("AGENT_REQUEST_TIMEOUT_SECONDS", "5")
            ),
            agent_version=os.getenv("RUNTIME_AGENT_VERSION", "1.0.0"),
            protocol_version=int(os.getenv("AGENT_PROTOCOL_VERSION", "1")),
            auto_launch_steam=_strict_bool("AUTO_LAUNCH_STEAM"),
            auto_launch_dst=_strict_bool("AUTO_LAUNCH_DST"),
            safe_idle_world_profile=_strict_bool("SAFE_IDLE_WORLD_PROFILE"),
            display_backend=os.getenv("DISPLAY_BACKEND", "xvfb").lower(),
            display=display,
            xdg_runtime_dir=os.getenv("XDG_RUNTIME_DIR") or None,
            dbus_session_bus_address=os.getenv("DBUS_SESSION_BUS_ADDRESS") or None,
            xauthority=xauthority,
            worker_plugin=worker_plugin,
            xvfb_command=xvfb_command,
            steam_command=tuple(
                shlex.split(os.getenv("STEAM_COMMAND", "steam -silent"))
            ),
            dst_command=tuple(
                shlex.split(os.getenv("DST_COMMAND", "steam -applaunch 322330"))
            ),
            steam_ready_file=Path(
                os.getenv("STEAM_READY_FILE", "/run/dst-runtime/steam.ready")
            ),
            steam_needs_login_file=Path(
                os.getenv(
                    "STEAM_NEEDS_LOGIN_FILE", "/run/dst-runtime/steam.needs-login"
                )
            ),
            dst_ready_file=Path(
                os.getenv("DST_READY_FILE", "/run/dst-runtime/dst.ready")
            ),
            max_restarts=int(os.getenv("PROCESS_MAX_RESTARTS", "3")),
            restart_backoff_seconds=float(
                os.getenv("PROCESS_RESTART_BACKOFF_SECONDS", "2")
            ),
            display_readiness_timeout_seconds=float(
                os.getenv("DISPLAY_READINESS_TIMEOUT_SECONDS", "15")
            ),
            steam_readiness_timeout_seconds=float(
                os.getenv("STEAM_READINESS_TIMEOUT_SECONDS", "120")
            ),
            dst_readiness_timeout_seconds=float(
                os.getenv("DST_READINESS_TIMEOUT_SECONDS", "300")
            ),
            worker_config=WorkerConfig.from_env(plugin=worker_plugin),
        )
        settings.validate()
        return settings
