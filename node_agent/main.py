from __future__ import annotations

import logging
import signal
import threading

from app.subprocess_env import purge_sensitive_environment
from node_agent.capabilities import discover
from node_agent.config import NodeAgentSettings
from node_agent.heartbeat import send_heartbeat
from node_agent.incus import active_runtime_count, incus_available
from node_agent.resources import collect_resources

logger = logging.getLogger("node_agent")
stop_event = threading.Event()


def _stop(*_args) -> None:
    stop_event.set()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stop_event.clear()
    settings = NodeAgentSettings.from_env()
    purge_sensitive_environment()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop_event.is_set():
        available = incus_available()
        send_heartbeat(
            settings,
            resources=collect_resources(),
            incus=available,
            active=active_runtime_count() if available else 0,
            capabilities=discover(available),
        )
        stop_event.wait(settings.heartbeat_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
