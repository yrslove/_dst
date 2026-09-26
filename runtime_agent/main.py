from __future__ import annotations

import logging
import os
import shutil
import signal
import threading

from app.runtime.display import GRAPHICAL_ENVIRONMENT_KEYS
from app.subprocess_env import purge_sensitive_environment
from runtime_agent.config import RuntimeAgentSettings
from runtime_agent.display import DisplayEnvironment, DisplayManager
from runtime_agent.heartbeat import send_heartbeat
from runtime_agent.process_supervisor import ProcessSupervisor
from runtime_agent.processes import DSTProcess, SteamProcess
from runtime_agent.processes.dst import DSTState
from runtime_agent.processes.steam import SteamState
from runtime_agent.worker_bridge import WorkerBridge

logger = logging.getLogger("runtime_agent")
stop_event = threading.Event()

_CHILD_ENVIRONMENT_SECRETS = {
    "RUNTIME_TOKEN",
    "NODE_TOKEN",
    "DST_FARM_SECRET_KEY",
    "ADMIN_PASSWORD",
    "STEAM_PASSWORD",
    "EMAIL_PASSWORD",
}


def _stop(*_args) -> None:
    stop_event.set()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stop_event.clear()
    settings = RuntimeAgentSettings.from_env()
    # Settings retains the runtime bearer in parent memory. Child processes inherit
    # no control-plane or account credentials through os.environ.
    purge_sensitive_environment()
    for name in _CHILD_ENVIRONMENT_SECRETS:
        os.environ.pop(name, None)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    display = DisplayManager(
        backend=settings.display_backend,
        environment=DisplayEnvironment(
            settings.display,
            settings.xdg_runtime_dir,
            settings.dbus_session_bus_address,
            settings.xauthority,
        ),
        xvfb_command=settings.xvfb_command,
        readiness_timeout_seconds=settings.display_readiness_timeout_seconds,
    )
    process_environment = display.environment.as_environ()
    for name in GRAPHICAL_ENVIRONMENT_KEYS:
        os.environ.pop(name, None)
    os.environ.update(process_environment)
    steam = ProcessSupervisor(
        "steam",
        settings.steam_command,
        max_restarts=settings.max_restarts,
        backoff_seconds=settings.restart_backoff_seconds,
        environment=process_environment,
    )
    dst = ProcessSupervisor(
        "dst",
        settings.dst_command,
        max_restarts=settings.max_restarts,
        backoff_seconds=settings.restart_backoff_seconds,
        environment=process_environment,
    )
    assert settings.worker_config is not None
    worker = WorkerBridge(
        settings.account_id,
        settings.runtime_id,
        display.environment,
        settings.worker_config,
        max_restarts=settings.max_restarts,
        restart_backoff_seconds=settings.restart_backoff_seconds,
        runtime_generation=settings.runtime_generation,
    )
    steam_process = SteamProcess(
        steam,
        settings.steam_ready_file,
        needs_login_file=settings.steam_needs_login_file,
        readiness_timeout_seconds=settings.steam_readiness_timeout_seconds,
    )
    dst_process = DSTProcess(
        dst,
        settings.dst_ready_file,
        readiness_timeout_seconds=settings.dst_readiness_timeout_seconds,
    )
    phase = "BOOTING"
    worker_initialized = False
    worker_report = worker.tick()
    try:
        while not stop_event.is_set():
            game_ready_now = False
            # Always reap/report children, even if an upstream readiness probe fails.
            steam.tick()
            dst.tick()
            display_ready = display.ensure_ready()
            if not display_ready:
                phase = (
                    "DISPLAY_STARTING"
                    if display.status() not in {"UNSUPPORTED", "ERROR"}
                    else "NEEDS_ATTENTION"
                )
                if display.status() in {"UNSUPPORTED", "ERROR"}:
                    dst.shutdown(timeout=3)
                    steam.shutdown(timeout=3)
            else:
                phase = "DISPLAY_READY"
                if settings.auto_launch_steam:
                    steam_process.start()
                    steam.tick()
                    phase = "STEAM_STARTING"
                    steam_state = steam_process.status()
                    if steam_state in {SteamState.ERROR, SteamState.NEEDS_LOGIN}:
                        phase = "NEEDS_ATTENTION"
                        dst.shutdown(timeout=3)
                    elif steam_state == SteamState.READY:
                        phase = "STEAM_READY"
                        if settings.auto_launch_dst:
                            dst_process.start()
                            dst.tick()
                            phase = "DST_STARTING"
                            dst_state = dst_process.status()
                            if dst_state == DSTState.ERROR:
                                phase = "NEEDS_ATTENTION"
                            elif dst_state == DSTState.READY:
                                # Close the probe/process-exit window before publishing
                                # GAME_READY or granting worker ownership.
                                steam.tick()
                                dst.tick()
                                steam_state = steam_process.status()
                                dst_state = dst_process.status()
                            if (
                                steam_state == SteamState.READY
                                and dst_state == DSTState.READY
                                and steam.alive
                                and dst.alive
                            ):
                                game_ready_now = True
                                phase = "GAME_READY"
                                if not worker_initialized:
                                    worker.on_game_ready()
                                    worker_initialized = True
                                    if not settings.worker_config.autostart:
                                        worker.pause()
                                worker_report = worker.tick()
                    else:
                        # A Steam restart invalidates the downstream game launch.
                        # Stop DST now so a fresh Steam readiness cycle cannot run
                        # concurrently with the previous game process.
                        dst.shutdown(timeout=3)
            if worker_initialized and not game_ready_now:
                worker.on_game_lost()
                worker_initialized = False
            worker_report = worker.tick()
            response = send_heartbeat(
                settings,
                phase=phase,
                steam_running=steam.alive,
                dst_running=dst.alive,
                healthy=phase in {"DISPLAY_READY", "STEAM_READY", "GAME_READY"},
                details={
                    "display": display.diagnostics(),
                    "steam": steam.status.as_dict(),
                    "dst": dst.status.as_dict(),
                    "readiness": {
                        "steam": steam_process.status(),
                        "dst": dst_process.status(),
                        "real_node_validation": "TODO_REAL_NODE_VALIDATION",
                    },
                    "worker": worker_report.as_dict(),
                    "worker_command_results": worker.acknowledgements(),
                },
                capabilities={
                    "display_available": display_ready,
                    "steam_available": steam.alive,
                    "dst_available": dst.alive,
                    "remote_view_available": display_ready
                    and shutil.which("xpra") is not None,
                    "gpu_visible": "UNKNOWN",
                    "worker_plugin": worker_report.plugin,
                    "worker_version": worker_report.version,
                    "worker_config_version": worker_report.config_version,
                    "worker_process_isolated": True,
                },
            )
            worker.set_runtime_verified(bool(response.get("runtime_verified", False)))
            worker.apply_commands(response.get("commands", []))
            stop_event.wait(settings.heartbeat_seconds)
    finally:
        cleanup_steps = (
            ("worker", worker.shutdown),
            ("dst", lambda: dst.shutdown(timeout=3)),
            ("steam", lambda: steam.shutdown(timeout=3)),
            ("display", lambda: display.stop(timeout=3)),
        )
        for component, cleanup in cleanup_steps:
            try:
                result = cleanup()
                if component == "worker":
                    worker_report = result
            except Exception:
                logger.exception("runtime cleanup failed for %s", component)
        send_heartbeat(
            settings,
            phase="SHUTTING_DOWN",
            steam_running=False,
            dst_running=False,
            healthy=False,
            details={
                "worker": worker_report.as_dict(),
                "worker_command_results": worker.acknowledgements(),
            },
            capabilities={"worker_plugin": worker_report.plugin},
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
