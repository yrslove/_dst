from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class NodeAgentSettings:
    control_plane_url: str
    node_id: int
    node_secret: str = field(repr=False)
    heartbeat_seconds: float = 10
    request_timeout_seconds: float = 5
    agent_version: str = "1.0.0"
    protocol_version: int = 1

    @classmethod
    def from_env(cls) -> NodeAgentSettings:
        node_id = int(os.getenv("NODE_ID", "0"))
        secret = os.getenv("NODE_SECRET", "")
        url = os.getenv("CONTROL_PLANE_URL", "").rstrip("/")
        if node_id < 1 or not secret or not url:
            raise ValueError("NODE_ID, NODE_SECRET, and CONTROL_PLANE_URL are required")
        settings = cls(
            control_plane_url=url,
            node_id=node_id,
            node_secret=secret,
            heartbeat_seconds=float(os.getenv("NODE_HEARTBEAT_SECONDS", "10")),
            request_timeout_seconds=float(
                os.getenv("AGENT_REQUEST_TIMEOUT_SECONDS", "5")
            ),
            agent_version=os.getenv("NODE_AGENT_VERSION", "1.0.0"),
            protocol_version=int(os.getenv("AGENT_PROTOCOL_VERSION", "1")),
        )
        parsed = urlsplit(settings.control_plane_url)
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
            settings.heartbeat_seconds <= 0
            or settings.request_timeout_seconds <= 0
            or settings.protocol_version < 1
        ):
            raise ValueError("node agent intervals and protocol version must be positive")
        return settings
