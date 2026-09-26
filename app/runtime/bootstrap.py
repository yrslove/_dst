from __future__ import annotations

import logging

from app.db import Database
from app.models import RuntimeInstance, utcnow
from app.providers.base import RuntimeDescriptor, RuntimeProvider
from app.runtime.bootstrap_errors import BootstrapFailed
from app.runtime.bootstrap_models import BootstrapPhase, RuntimeAgentConfig
from app.services.records import add_event

logger = logging.getLogger("runtime.bootstrap")

SYSTEMD_UNIT = """[Unit]
Description=DST runtime supervisor agent
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=dst
Group=dst
RuntimeDirectory=dst-runtime
RuntimeDirectoryMode=0700
EnvironmentFile=/etc/dst-runtime/agent.env
WorkingDirectory=/opt/dst-orchestrator
ExecStart=/opt/dst-orchestrator/.venv/bin/python -m runtime_agent.main
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
NoNewPrivileges=true
PrivateTmp=false
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/run/dst-runtime /home/dst

[Install]
WantedBy=multi-user.target
"""


class RuntimeBootstrapService:
    """Versioned, resumable runtime bootstrap without retaining runtime secrets.

    Each completed phase is committed before moving on. Retrying repeats only the
    current phase, whose provider operation must itself be idempotent.
    """

    def __init__(self, db: Database, provider: RuntimeProvider, *, version: int = 1):
        self.db = db
        self.provider = provider
        self.version = version

    def bootstrap(
        self,
        descriptor: RuntimeDescriptor,
        config: RuntimeAgentConfig,
        *,
        correlation_id: str | None = None,
    ) -> BootstrapPhase:
        if descriptor.id != config.runtime_id:
            raise BootstrapFailed("runtime configuration does not match descriptor")
        phase = self._current_phase(descriptor.id)
        phases = list(BootstrapPhase)
        start = phases.index(phase) + 1 if phase else 0
        try:
            for next_phase in phases[start:]:
                self._apply(next_phase, descriptor, config, correlation_id)
                self._record(descriptor.id, next_phase, correlation_id)
            return BootstrapPhase.BOOTSTRAP_COMPLETE
        except Exception as exc:
            self._record_failure(descriptor.id, exc, correlation_id)
            if isinstance(exc, BootstrapFailed):
                raise
            raise BootstrapFailed(
                "runtime bootstrap phase failed; inspect redacted diagnostics"
            ) from exc

    def _current_phase(self, runtime_id: int) -> BootstrapPhase | None:
        with self.db.session() as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            if runtime is None:
                raise BootstrapFailed("runtime not found")
            if runtime.bootstrap_version > self.version:
                raise BootstrapFailed(
                    "runtime bootstrap version is newer than this control plane"
                )
            if runtime.bootstrap_version < self.version:
                return None
            if runtime.bootstrap_phase and runtime.bootstrap_phase not in {
                str(phase) for phase in BootstrapPhase
            }:
                raise BootstrapFailed("runtime contains an unknown bootstrap phase")
            return (
                BootstrapPhase(runtime.bootstrap_phase)
                if runtime.bootstrap_phase
                else None
            )

    def _apply(
        self,
        phase: BootstrapPhase,
        runtime: RuntimeDescriptor,
        config: RuntimeAgentConfig,
        correlation_id: str | None,
    ) -> None:
        logger.info(
            "runtime bootstrap phase",
            extra={
                "event": "runtime.bootstrap.started",
                "runtime_id": runtime.id,
                "phase": phase,
            },
        )
        if phase == BootstrapPhase.RUNTIME_CREATED:
            # Provisioning owns creation. Bootstrap runs only after the instance
            # has started, so repeating ensure() here would blur the exactly-once
            # provision boundary and could invoke destructive provider logic.
            self.provider.inspect(runtime, correlation_id=correlation_id)
        elif phase == BootstrapPhase.BASE_CONFIG_APPLIED:
            self.provider.execute(
                runtime,
                ("/usr/bin/install", "-d", "-m", "0750", "/etc/dst-runtime"),
                correlation_id=correlation_id,
            )
        elif phase == BootstrapPhase.AGENT_FILES_INSTALLED:
            # Runtime image owns package installation; validate the exact paths used
            # by ExecStart instead of an unrelated compatibility launcher.
            self.provider.execute(
                runtime,
                ("/usr/bin/test", "-x", "/opt/dst-orchestrator/.venv/bin/python"),
                correlation_id=correlation_id,
            )
            self.provider.execute(
                runtime,
                ("/usr/bin/test", "-r", "/opt/dst-orchestrator/runtime_agent/main.py"),
                correlation_id=correlation_id,
            )
        elif phase == BootstrapPhase.AGENT_CONFIGURED:
            self.provider.put_file(
                runtime,
                "/etc/dst-runtime/agent.env",
                config.environment_file(),
                mode=0o600,
                correlation_id=correlation_id,
            )
        elif phase == BootstrapPhase.AGENT_SERVICE_INSTALLED:
            self.provider.put_file(
                runtime,
                "/etc/systemd/system/dst-runtime-agent.service",
                SYSTEMD_UNIT.encode("utf-8"),
                mode=0o644,
                correlation_id=correlation_id,
            )
            self.provider.execute(
                runtime,
                ("/bin/systemctl", "daemon-reload"),
                correlation_id=correlation_id,
            )
        elif phase == BootstrapPhase.DISPLAY_CONFIGURED:
            self.provider.execute(
                runtime,
                (
                    "/usr/bin/install",
                    "-d",
                    "-o",
                    "dst",
                    "-g",
                    "dst",
                    "-m",
                    "0700",
                    "/run/dst-runtime",
                ),
                correlation_id=correlation_id,
            )
        elif phase in {
            BootstrapPhase.STEAM_RUNTIME_PREPARED,
            BootstrapPhase.DST_RUNTIME_PREPARED,
        }:
            # Presence only: readiness remains an agent-reported, real-node concern.
            self.provider.execute(
                runtime, ("/usr/bin/true",), correlation_id=correlation_id
            )
        elif phase == BootstrapPhase.BOOTSTRAP_COMPLETE:
            # Start only after every prerequisite is committed. Repeating this
            # operation after a partial failure is safe and refreshes changed env.
            self.provider.execute(
                runtime,
                ("/bin/systemctl", "enable", "dst-runtime-agent.service"),
                correlation_id=correlation_id,
            )
            # `start` is a no-op for an existing service, but token rotation and
            # configuration repair require the new EnvironmentFile to be loaded.
            self.provider.execute(
                runtime,
                ("/bin/systemctl", "restart", "dst-runtime-agent.service"),
                correlation_id=correlation_id,
            )

    def _record(
        self, runtime_id: int, phase: BootstrapPhase, request_id: str | None
    ) -> None:
        with self.db.transaction(immediate=True) as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            if runtime is None:
                raise BootstrapFailed("runtime disappeared during bootstrap")
            runtime.bootstrap_version = self.version
            runtime.bootstrap_phase = phase
            runtime.bootstrap_error_code = None
            runtime.bootstrap_error_message = None
            if phase == BootstrapPhase.BOOTSTRAP_COMPLETE:
                runtime.bootstrap_completed_at = utcnow()
            add_event(
                session,
                level="INFO",
                kind="RUNTIME_BOOTSTRAP_PHASE_COMPLETED",
                message=f"Runtime bootstrap completed {phase}",
                runtime_id=runtime.id,
                account_id=runtime.account_id,
                node_id=runtime.node_id,
                request_id=request_id,
                metadata={"bootstrap_version": self.version, "phase": phase},
            )

    def _record_failure(
        self, runtime_id: int, exc: Exception, request_id: str | None
    ) -> None:
        with self.db.transaction(immediate=True) as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            if runtime is None:
                return
            runtime.bootstrap_error_code = "RUNTIME_BOOTSTRAP_FAILED"
            runtime.bootstrap_error_message = str(exc)[:1000]
            add_event(
                session,
                level="ERROR",
                kind="RUNTIME_BOOTSTRAP_FAILED",
                message="Runtime bootstrap failed; retry resumes from the saved phase",
                runtime_id=runtime.id,
                account_id=runtime.account_id,
                node_id=runtime.node_id,
                request_id=request_id,
            )
