from __future__ import annotations

import logging

import httpx

from runtime_agent.config import RuntimeAgentSettings

logger = logging.getLogger("runtime_agent.heartbeat")


def send_heartbeat(
    settings: RuntimeAgentSettings,
    *,
    phase: str,
    steam_running: bool,
    dst_running: bool,
    healthy: bool,
    details: dict,
    capabilities: dict | None = None,
) -> dict:
    payload = {
        "runtime_id": settings.runtime_id,
        "phase": phase,
        "steam_running": steam_running,
        "dst_running": dst_running,
        "healthy": healthy,
        "automation_state": details.get("worker", {}).get("state", "DISABLED"),
        "worker_plugin": details.get("worker", {}).get("plugin", "noop"),
        "worker_state": details.get("worker", {}).get("state", "DISABLED"),
        "worker_last_tick_at": details.get("worker", {}).get("last_tick_at"),
        "worker_last_action": details.get("worker", {}).get("last_action"),
        "worker_last_observation_at": details.get("worker", {}).get(
            "last_observation_at"
        ),
        "worker_error_code": details.get("worker", {}).get("error_code"),
        "worker_restart_count": details.get("worker", {}).get("restart_count", 0),
        "worker": details.get("worker", {}),
        "worker_command_results": details.get("worker_command_results", []),
        "details": details,
        "capabilities": capabilities or {},
        "agent_version": settings.agent_version,
        "protocol_version": settings.protocol_version,
    }
    results = [
        {"id": item.get("id"), "result": item.get("result")}
        for item in payload["worker_command_results"]
        if isinstance(item, dict)
    ]
    if results:
        logger.info(
            "worker_ack_control_plane_submit runtime_id=%s results=%s",
            settings.runtime_id,
            results,
        )
    try:
        response = httpx.post(
            f"{settings.control_plane_url}/api/v1/runtime-agent/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {settings.runtime_token}"},
            timeout=settings.request_timeout_seconds,
        )
        response.raise_for_status()
        value = response.json()
        if results:
            logger.info(
                "worker_ack_control_plane_response runtime_id=%s results=%s "
                "http_status=%s response_ok=%s",
                settings.runtime_id,
                results,
                response.status_code,
                value.get("ok") if isinstance(value, dict) else None,
            )
        return value if isinstance(value, dict) else {"ok": True}
    except (httpx.HTTPError, ValueError) as exc:
        if results:
            logger.warning(
                "worker_ack_control_plane_failed runtime_id=%s results=%s error=%s",
                settings.runtime_id,
                results,
                exc,
            )
        logger.warning("runtime heartbeat failed: %s", exc)
        return {"ok": False, "commands": []}
