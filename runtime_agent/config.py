from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from runtime_agent.gameworker.config import WorkerConfig


@dataclass(frozen=True, slots=True)
class RuntimeAgentSettings:
    control_plane_url: str
    runtime_id: int
    runtime_token: str
    account_id: int
    node_id: int = 0
    heartbeat_seconds: float = 5
    request_timeout_seconds: float = 5
    agent_version: str = "1.0.0"
    protocol_version: int = 1
    auto_launch_steam: bool = False
    auto_launch_dst: bool = False
    display_backend: str = "xvfb"
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
    worker_config: WorkerConfig | None = None

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
        return cls(
            control_plane_url=url.rstrip("/"),
            runtime_id=runtime_id,
            runtime_token=token,
            account_id=int(os.getenv("ACCOUNT_ID", "0")),
            node_id=int(os.getenv("NODE_ID", "0")),
            heartbeat_seconds=float(os.getenv("RUNTIME_HEARTBEAT_SECONDS", "5")),
            request_timeout_seconds=float(
                os.getenv("AGENT_REQUEST_TIMEOUT_SECONDS", "5")
            ),
            agent_version=os.getenv("RUNTIME_AGENT_VERSION", "1.0.0"),
            protocol_version=int(os.getenv("AGENT_PROTOCOL_VERSION", "1")),
            auto_launch_steam=os.getenv("AUTO_LAUNCH_STEAM", "0") == "1",
            auto_launch_dst=os.getenv("AUTO_LAUNCH_DST", "0") == "1",
            display_backend=os.getenv("DISPLAY_BACKEND", "xvfb").lower(),
            worker_plugin=worker_plugin,
            xvfb_command=tuple(
                shlex.split(
                    os.getenv(
                        "XVFB_COMMAND",
                        f"Xvfb {display} -screen 0 1280x720x24 -nolisten tcp",
                    )
                )
            ),
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
            worker_config=WorkerConfig.from_env(plugin=worker_plugin),
        )
