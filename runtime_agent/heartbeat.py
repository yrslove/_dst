from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

from runtime_agent.config import RuntimeAgentSettings

logger = logging.getLogger("runtime_agent.heartbeat")


class VerificationState(StrEnum):
    VERIFIED_TRUE = "VERIFIED_TRUE"
    VERIFIED_FALSE = "VERIFIED_FALSE"
    VERIFICATION_UNKNOWN = "VERIFICATION_UNKNOWN"


@dataclass(slots=True)
class RuntimeVerification:
    """Keep transport loss distinct from an explicit control-plane revocation."""

    grace_seconds: float
    state: VerificationState = VerificationState.VERIFICATION_UNKNOWN
    last_verified_at: float | None = None

    def __post_init__(self) -> None:
        if self.grace_seconds <= 0:
            raise ValueError("verification grace must be positive")

    def observe(self, response: dict, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        explicit = response.get("runtime_verified")
        if isinstance(explicit, bool):
            if explicit:
                self.state = VerificationState.VERIFIED_TRUE
                self.last_verified_at = now
            else:
                self.state = VerificationState.VERIFIED_FALSE
                self.last_verified_at = None
            return explicit
        self.state = VerificationState.VERIFICATION_UNKNOWN
        return bool(
            self.last_verified_at is not None
            and now - self.last_verified_at <= self.grace_seconds
        )


def _record_accepted_heartbeat(settings, *, phase: str, healthy: bool) -> None:
    runtime_dir = getattr(settings, "xdg_runtime_dir", None)
    if not runtime_dir:
        return
    marker = Path(runtime_dir) / "heartbeat.json"
    temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
    revision_file = Path(__file__).resolve().parents[1] / "DEPLOYMENT.json"
    try:
        revision = json.loads(revision_file.read_text(encoding="utf-8")).get(
            "commit", "UNKNOWN"
        )
    except (OSError, json.JSONDecodeError):
        revision = "UNKNOWN"
    payload = {
        "runtime_id": settings.runtime_id,
        "agent_pid": os.getpid(),
        "revision": revision,
        "timestamp_unix": time.time(),
        "phase": phase,
        "healthy": healthy,
    }
    try:
        temporary.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        os.replace(temporary, marker)
    except OSError:
        logger.warning("could not persist accepted heartbeat marker", exc_info=True)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
        value = value if isinstance(value, dict) else {"ok": True}
        logger.info(
            "runtime_heartbeat_accepted runtime_id=%s phase=%s healthy=%s",
            settings.runtime_id,
            phase,
            healthy,
        )
        if value.get("ok", True) is not False:
            _record_accepted_heartbeat(settings, phase=phase, healthy=healthy)
        if results:
            logger.info(
                "worker_ack_control_plane_response runtime_id=%s results=%s "
                "http_status=%s response_ok=%s",
                settings.runtime_id,
                results,
                response.status_code,
                value.get("ok") if isinstance(value, dict) else None,
            )
        return value
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
