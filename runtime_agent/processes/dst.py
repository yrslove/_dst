from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path

from app.runtime.world_profile import (
    DEFAULT_EVIDENCE_PATH,
    DEFAULT_USER_ROOT,
    desired_profile_hash,
    read_process_evidence,
    reconcile_world_profile,
    record_process_evidence,
    refresh_process_evidence,
)
from runtime_agent.process_supervisor import ProcessSupervisor


class DSTState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    READY = "READY"
    CRASHED = "CRASHED"
    ERROR = "ERROR"


class DSTProcess:
    def __init__(
        self,
        supervisor: ProcessSupervisor,
        readiness_file: Path,
        *,
        readiness_timeout_seconds: float = 300,
        account_id: int = 0,
        runtime_id: int = 0,
        runtime_generation: int = 1,
        safe_idle_world_profile: bool = False,
        user_root: Path = DEFAULT_USER_ROOT,
        evidence_path: Path = DEFAULT_EVIDENCE_PATH,
    ):
        self.supervisor, self.readiness_file, self._state = (
            supervisor,
            readiness_file,
            DSTState.STOPPED,
        )
        self.readiness_timeout_seconds = readiness_timeout_seconds
        self._terminal_error = False
        self._ever_ready = False
        self._readiness_lost_at: float | None = None
        self.runtime_id = runtime_id
        self.account_id = account_id
        self.runtime_generation = runtime_generation
        self.safe_idle_world_profile = safe_idle_world_profile
        self.user_root = user_root
        self.evidence_path = evidence_path
        self._pending_reconciliation: dict | None = None
        previous_before_start = supervisor.before_start

        def reset_marker() -> None:
            if previous_before_start:
                previous_before_start()
            self.readiness_file.unlink(missing_ok=True)
            self._pending_reconciliation = None
            if self.safe_idle_world_profile:
                self.evidence_path.unlink(missing_ok=True)
                try:
                    self._pending_reconciliation = reconcile_world_profile(
                        user_root=self.user_root
                    )
                except Exception as exc:
                    raise OSError("safe world profile reconciliation failed") from exc

        supervisor.before_start = reset_marker

    def prepare(self):
        return self._state

    def start(self):
        if self._terminal_error:
            return DSTState.ERROR
        self.supervisor.request_start()
        self._state = DSTState.STARTING
        return self._state

    def status(self):
        if self._terminal_error:
            return DSTState.ERROR
        if self.supervisor.status.exhausted:
            return DSTState.ERROR
        if self.supervisor.alive:
            if self._ready_marker() and self.supervisor.alive:
                self._ever_ready = True
                self._readiness_lost_at = None
                if self.safe_idle_world_profile:
                    self._refresh_profile_evidence()
                return DSTState.READY
            if self._ever_ready:
                now = self.supervisor.clock()
                if self._readiness_lost_at is None:
                    self._readiness_lost_at = now
                elif now - self._readiness_lost_at >= 15:
                    # A previously ready game disappeared while its launcher
                    # process group lingered. Stop that group before the next
                    # supervised launch, without making startup terminal.
                    self.supervisor.shutdown(timeout=3)
                    self._ever_ready = False
                    self._readiness_lost_at = None
                    self._state = DSTState.CRASHED
                    return self._state
                return DSTState.RUNNING
            if self.supervisor.running_for >= self.readiness_timeout_seconds:
                self.supervisor.shutdown(timeout=3)
                self._terminal_error = True
                self._state = DSTState.ERROR
                return self._state
            return DSTState.RUNNING
        if self._ever_ready:
            self._ever_ready = False
            self._readiness_lost_at = None
            self._state = DSTState.CRASHED
        return self._state

    def wait_ready(self):
        return self.status() == DSTState.READY

    def stop(self):
        self.supervisor.shutdown()
        self._state = DSTState.STOPPED
        return self._state

    def diagnostics(self):
        return self.supervisor.status.as_dict()

    def world_profile_evidence(self) -> dict:
        if not self.safe_idle_world_profile:
            return {"enabled": False, "status": "DISABLED"}
        evidence = self._refresh_profile_evidence()
        return {"enabled": True, **(evidence or {"status": "UNVERIFIED"})}

    def _refresh_profile_evidence(self) -> dict | None:
        if (
            not self.safe_idle_world_profile
            or not self.supervisor.alive
            or not self._ready_marker()
        ):
            return None
        pid = self.supervisor.status.pid
        identity = self.supervisor.process_identity(pid) if pid else None
        if identity is None:
            return None
        start_ticks = identity[0]
        try:
            cluster_paths = sorted(self.user_root.glob("*/Cluster_1/worldgenoverride.lua"))
            if len(cluster_paths) != 1:
                return None
            config_hash = hashlib.sha256(cluster_paths[0].read_bytes()).hexdigest()
        except OSError:
            return None
        expected_hash = desired_profile_hash()
        current = read_process_evidence(evidence_path=self.evidence_path)
        matching = bool(
            current
            and current.get("runtime_id") == self.runtime_id
            and current.get("runtime_generation") == self.runtime_generation
            and current.get("process_id") == pid
            and current.get("process_start_ticks") == start_ticks
            and current.get("desired_profile_hash") == expected_hash
            and current.get("applied_profile_hash") == config_hash == expected_hash
            and current.get("world_path") == str(cluster_paths[0])
        )
        if not matching:
            reconciliation = self._pending_reconciliation
            if not (
                reconciliation
                and reconciliation.get("desired_profile_hash") == expected_hash
                and reconciliation.get("applied_profile_hash") == config_hash
                and reconciliation.get("path") == str(cluster_paths[0])
            ):
                return None
            current = record_process_evidence(
                reconciliation=reconciliation,
                account_id=self.account_id,
                runtime_id=self.runtime_id,
                runtime_generation=self.runtime_generation,
                process_id=pid,
                process_start_ticks=start_ticks,
                process_started_at=self.supervisor.status.started_at or "",
                evidence_path=self.evidence_path,
            )
        else:
            # Refresh the timestamp only after rechecking the file and the live
            # process identity. This keeps heartbeat evidence current after agent
            # adoption while rejecting evidence from a previous process.
            current = refresh_process_evidence(
                current, evidence_path=self.evidence_path
            )
        return current

    def _ready_marker(self):
        if not self.supervisor.status.started_at:
            return False
        try:
            # before_start unlinks the marker for every generation; do not depend
            # on filesystem timestamp precision after that generation barrier.
            return self.readiness_file.is_file()
        except OSError:
            return False
