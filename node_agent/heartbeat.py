from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from node_agent.config import NodeAgentSettings

logger = logging.getLogger("node_agent.heartbeat")


def send_heartbeat(
    settings: NodeAgentSettings,
    *,
    resources: dict,
    incus: bool,
    active: int,
    capabilities: dict | None = None,
) -> bool:
    payload = {
        "node_id": settings.node_id,
        "agent_version": settings.agent_version,
        "protocol_version": settings.protocol_version,
        "incus_available": incus,
        "active_runtime_count": active,
        "capabilities": capabilities or {},
        "resources": resources,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response = httpx.post(
            f"{settings.control_plane_url}/api/v1/node-agent/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {settings.node_secret}"},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("node heartbeat failed: %s", exc)
        return False
