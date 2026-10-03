from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum


class BootstrapPhase(StrEnum):
    RUNTIME_CREATED = "RUNTIME_CREATED"
    BASE_CONFIG_APPLIED = "BASE_CONFIG_APPLIED"
    AGENT_FILES_INSTALLED = "AGENT_FILES_INSTALLED"
    AGENT_CONFIGURED = "AGENT_CONFIGURED"
    AGENT_SERVICE_INSTALLED = "AGENT_SERVICE_INSTALLED"
    DISPLAY_CONFIGURED = "DISPLAY_CONFIGURED"
    STEAM_RUNTIME_PREPARED = "STEAM_RUNTIME_PREPARED"
    DST_RUNTIME_PREPARED = "DST_RUNTIME_PREPARED"
    BOOTSTRAP_COMPLETE = "BOOTSTRAP_COMPLETE"


@dataclass(frozen=True, slots=True)
class RuntimeAgentConfig:
    runtime_id: int
    account_id: int
    node_id: int
    runtime_token: str = field(repr=False)
    orchestrator_url: str
    protocol_version: int
    heartbeat_interval: float
    runtime_generation: int = 1
    runtime_image_version: str = "unknown"
    display_backend: str = "xvfb"
    display: str = ":99"
    xdg_runtime_dir: str | None = "/run/dst-runtime"
    dbus_session_bus_address: str | None = None
    xauthority: str | None = None
    steam_enabled: bool = False
    dst_enabled: bool = False
    steam_command: str | None = None
    dst_command: str | None = None
    display_readiness_timeout: float = 15
    steam_readiness_timeout: float = 120
    dst_readiness_timeout: float = 300
    worker_plugin: str = "noop"
    worker_mode: str = "DISABLED"
    worker_autostart: bool = False
    worker_config_schema_version: int = 1
    worker_calibration_profile: str = "dst-1280x720-linux-v1"
    worker_calibration_verified: bool = False
    safe_idle_world: bool = False

    def environment_file(self) -> bytes:
        # Explicit allow-list: this file is the only bootstrap secret carrier.
        values = {
            "CONTROL_PLANE_URL": self.orchestrator_url,
            "RUNTIME_ID": self.runtime_id,
            "RUNTIME_GENERATION": self.runtime_generation,
            "RUNTIME_IMAGE_VERSION": self.runtime_image_version,
            "ACCOUNT_ID": self.account_id,
            "NODE_ID": self.node_id,
            "RUNTIME_TOKEN": self.runtime_token,
            "AGENT_PROTOCOL_VERSION": self.protocol_version,
            "RUNTIME_HEARTBEAT_SECONDS": self.heartbeat_interval,
            "DISPLAY_BACKEND": self.display_backend,
            "DISPLAY": self.display,
            "AUTO_LAUNCH_STEAM": int(self.steam_enabled),
            "AUTO_LAUNCH_DST": int(self.dst_enabled),
            "STEAM_READY_FILE": "/run/dst-runtime/steam.ready",
            "STEAM_NEEDS_LOGIN_FILE": "/run/dst-runtime/steam.needs-login",
            "DST_READY_FILE": "/run/dst-runtime/dst.ready",
            "SAFE_IDLE_WORLD_PROFILE": int(self.safe_idle_world),
            "DISPLAY_READINESS_TIMEOUT_SECONDS": self.display_readiness_timeout,
            "STEAM_READINESS_TIMEOUT_SECONDS": self.steam_readiness_timeout,
            "DST_READINESS_TIMEOUT_SECONDS": self.dst_readiness_timeout,
            "WORKER_PLUGIN": self.worker_plugin,
            "WORKER_MODE": self.worker_mode,
            "WORKER_AUTOSTART": int(self.worker_autostart),
            "WORKER_CONFIG_SCHEMA_VERSION": self.worker_config_schema_version,
            "WORKER_CALIBRATION_PROFILE": self.worker_calibration_profile,
            "WORKER_CALIBRATION_VERIFIED": int(self.worker_calibration_verified),
        }
        if self.safe_idle_world:
            values["WORKER_OBSERVATION_INTERVAL"] = 12
        if self.xdg_runtime_dir:
            values["XDG_RUNTIME_DIR"] = self.xdg_runtime_dir
        if self.dbus_session_bus_address:
            values["DBUS_SESSION_BUS_ADDRESS"] = self.dbus_session_bus_address
        if self.xauthority:
            values["XAUTHORITY"] = self.xauthority
        if self.steam_command:
            values["STEAM_COMMAND"] = self.steam_command
        if self.dst_command:
            values["DST_COMMAND"] = self.dst_command
        lines = []
        for key, value in values.items():
            rendered = str(value)
            if any(character in rendered for character in ("\x00", "\n", "\r")):
                raise ValueError(f"invalid control character in {key}")
            rendered = rendered.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{rendered}"\n')
        return "".join(lines).encode("utf-8")

    def redacted(self) -> dict:
        value = asdict(self)
        value["runtime_token"] = "[REDACTED]"
        return value
