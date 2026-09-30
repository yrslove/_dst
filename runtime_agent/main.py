from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
import threading
from pathlib import Path

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
reload_event = threading.Event()


def _adoption_identity(name: str) -> tuple[int, int] | None:
    value = os.getenv(f"RUNTIME_ADOPT_{name}")
    if not value:
        return None
    pid, start_ticks = value.split(":", 1)
    return int(pid), int(start_ticks)

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


def _reload(*_args) -> None:
    reload_event.set()
    stop_event.set()


def _handoff_environment(display, steam, dst, *, runtime_token: str) -> dict[str, str] | None:
    identities = {
        "DISPLAY": display.adoption_identity(),
        "STEAM": steam.adoption_identity(),
        "DST": dst.adoption_identity(),
    }
    if any(identity is None for identity in identities.values()):
        return None
    environment = os.environ.copy()
    # The bearer was removed from os.environ before managed children started.
    # Restore it only in the replacement agent's exec environment.
    environment["RUNTIME_TOKEN"] = runtime_token
    for name, identity in identities.items():
        assert identity is not None
        environment[f"RUNTIME_ADOPT_{name}"] = f"{identity[0]}:{identity[1]}"
    return environment


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    stop_event.clear()
    reload_event.clear()
    settings = RuntimeAgentSettings.from_env()
    revision_file = Path(__file__).resolve().parents[1] / "DEPLOYMENT.json"
    try:
        deployed_revision = json.loads(revision_file.read_text(encoding="utf-8")).get(
            "commit", "INVALID"
        )
    except (OSError, json.JSONDecodeError):
        deployed_revision = "UNDEPLOYED"
    logger.info("runtime_agent_started deployed_revision=%s", deployed_revision)
    # Settings retains the runtime bearer in parent memory. Child processes inherit
    # no control-plane or account credentials through os.environ.
    purge_sensitive_environment()
    for name in _CHILD_ENVIRONMENT_SECRETS:
        os.environ.pop(name, None)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGHUP, _reload)
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
    dst_environment = dict(process_environment)
    adopted_game_pid = os.getenv("DST_ADOPT_GAME_PID")
    if adopted_game_pid:
        if not adopted_game_pid.isdecimal() or int(adopted_game_pid) <= 1:
            raise RuntimeError("DST_ADOPT_GAME_PID must be a positive process id")
        dst_environment["DST_ADOPT_GAME_PID"] = adopted_game_pid
        os.environ.pop("DST_ADOPT_GAME_PID", None)
    dst = ProcessSupervisor(
        "dst",
        settings.dst_command,
        max_restarts=settings.max_restarts,
        backoff_seconds=settings.restart_backoff_seconds,
        environment=dst_environment,
    )
    adoption = {name: _adoption_identity(name) for name in ("DISPLAY", "STEAM", "DST")}
    if adoption["DISPLAY"] or adoption["STEAM"] or adoption["DST"]:
        if not adoption["DISPLAY"] or not adoption["STEAM"]:
            raise RuntimeError("agent replacement requires managed display and Steam identities")
        display.adopt(*adoption["DISPLAY"])
        steam.adopt(*adoption["STEAM"])
        if adoption["DST"]:
            dst.adopt(*adoption["DST"])
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
        account_id=settings.account_id,
        runtime_id=settings.runtime_id,
        runtime_generation=settings.runtime_generation,
        safe_idle_world_profile=settings.safe_idle_world_profile,
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
                    "world_profile": dst_process.world_profile_evidence(),
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
        try:
            worker_report = worker.shutdown()
        except Exception:
            logger.exception("runtime cleanup failed for worker")
            worker_report = None
        if reload_event.is_set():
            try:
                identities = _handoff_environment(
                    display, steam, dst, runtime_token=settings.runtime_token
                )
                runtime_ready = (
                    worker_report is not None
                    and worker_report.healthy
                    and display.status().value == "READY"
                    and steam_process.status() == SteamState.READY
                    and dst_process.status() == DSTState.READY
                    and steam.alive
                    and dst.alive
                )
                if runtime_ready and identities is not None:
                    logger.info(
                        "runtime_agent_reload_preserving_managed_processes "
                        "runtime_id=%s display_pid=%s steam_pid=%s dst_pid=%s",
                        settings.runtime_id,
                        identities["RUNTIME_ADOPT_DISPLAY"].split(":", 1)[0],
                        identities["RUNTIME_ADOPT_STEAM"].split(":", 1)[0],
                        identities["RUNTIME_ADOPT_DST"].split(":", 1)[0],
                    )
                    os.execve(
                        sys.executable,
                        [sys.executable, "-m", "runtime_agent.main"],
                        identities,
                    )
                logger.warning(
                    "runtime_agent_reload_falling_back_to_full_shutdown "
                    "runtime_id=%s worker_healthy=%s display=%s steam=%s dst=%s",
                    settings.runtime_id,
                    bool(worker_report and worker_report.healthy),
                    display.status(),
                    steam_process.status(),
                    dst_process.status(),
                )
            except Exception:
                logger.exception("agent-only reload failed; using full shutdown")
        cleanup_steps = (
            ("dst", lambda: dst.shutdown(timeout=3)),
            ("steam", lambda: steam.shutdown(timeout=3)),
            ("display", lambda: display.stop(timeout=3)),
        )
        for component, cleanup in cleanup_steps:
            try:
                cleanup()
            except Exception:
                logger.exception("runtime cleanup failed for %s", component)
        if worker_report is not None:
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
    return 1 if reload_event.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
