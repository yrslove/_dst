from __future__ import annotations

from dataclasses import asdict, dataclass
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
    runtime_token: str
    orchestrator_url: str
    protocol_version: int
    heartbeat_interval: float
    display_backend: str = "xvfb"
    display: str = ":99"
    xdg_runtime_dir: str = "/run/user/1000"
    dbus_session_bus_address: str | None = None
    steam_enabled: bool = True
    dst_enabled: bool = True
    worker_plugin: str = "noop"
    worker_mode: str = "DISABLED"
    worker_autostart: bool = False
    worker_config_schema_version: int = 1

    def environment_file(self) -> bytes:
        # Explicit allow-list: this file is the only bootstrap secret carrier.
        values = {
            "CONTROL_PLANE_URL": self.orchestrator_url,
            "RUNTIME_ID": self.runtime_id,
            "ACCOUNT_ID": self.account_id,
            "NODE_ID": self.node_id,
            "RUNTIME_TOKEN": self.runtime_token,
            "AGENT_PROTOCOL_VERSION": self.protocol_version,
            "RUNTIME_HEARTBEAT_SECONDS": self.heartbeat_interval,
            "DISPLAY_BACKEND": self.display_backend,
            "DISPLAY": self.display,
            "XDG_RUNTIME_DIR": self.xdg_runtime_dir,
            "STEAM_ENABLED": int(self.steam_enabled),
            "DST_ENABLED": int(self.dst_enabled),
            "WORKER_PLUGIN": self.worker_plugin,
            "WORKER_MODE": self.worker_mode,
            "WORKER_AUTOSTART": int(self.worker_autostart),
            "WORKER_CONFIG_SCHEMA_VERSION": self.worker_config_schema_version,
        }
        if self.dbus_session_bus_address:
            values["DBUS_SESSION_BUS_ADDRESS"] = self.dbus_session_bus_address
        return "".join(f"{key}={value}\n" for key, value in values.items()).encode(
            "utf-8"
        )

    def redacted(self) -> dict:
        value = asdict(self)
        value["runtime_token"] = "[REDACTED]"
        return value
