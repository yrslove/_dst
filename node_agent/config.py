from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NodeAgentSettings:
    control_plane_url: str
    node_id: int
    node_secret: str
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
        return cls(
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
