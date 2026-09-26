from __future__ import annotations

from sqlalchemy import select

from app.db import Database
from app.models import (
    RuntimeInstance,
    RuntimeState,
    WorkerCommand,
    WorkerStatus,
    utcnow,
)
from app.services.records import add_audit


class WorkerControlError(RuntimeError):
    pass


class WorkerControlService:
    def __init__(self, db: Database):
        self.db = db

    def command(
        self,
        account_id: int,
        command: str,
        *,
        actor: str,
        request_id: str,
        payload: dict | None = None,
    ) -> dict:
        command = command.upper()
        if command not in {"PAUSE", "RESUME", "STOP", "SET_MODE"}:
            raise WorkerControlError("unsupported worker command")
        with self.db.transaction(immediate=True) as session:
            runtime = session.scalar(
                select(RuntimeInstance).where(
                    RuntimeInstance.account_id == account_id,
                    RuntimeInstance.active.is_(True),
                )
            )
            if runtime is None:
                raise WorkerControlError("active runtime not found")
            values = dict(payload or {})
            safety_command = command in {"PAUSE", "STOP"}
            if runtime.state != RuntimeState.RUNNING and not (
                safety_command
                and runtime.state
                in {RuntimeState.STARTING, RuntimeState.STOPPING, RuntimeState.STALE}
            ):
                raise WorkerControlError(
                    f"worker command is unavailable while runtime is {runtime.state}"
                )
            if command == "SET_MODE":
                mode = values.get("mode")
                if mode not in {"DISABLED", "OBSERVE", "ACTIVE"}:
                    raise WorkerControlError("invalid worker mode")
                if mode == "ACTIVE" and runtime.verified_at is None:
                    raise WorkerControlError(
                        "runtime must be verified before ACTIVE worker mode"
                    )
            record = WorkerCommand(
                runtime_id=runtime.id,
                command=command,
                payload=values,
                status="PENDING",
                created_by=actor,
                created_at=utcnow(),
            )
            session.add(record)
            session.flush()
            add_audit(
                session,
                actor=actor,
                action=f"WORKER_{command}",
                entity_type="runtime",
                entity_id=runtime.id,
                request_id=request_id,
                metadata={
                    "worker_command_id": record.id,
                    **({"mode": values.get("mode")} if command == "SET_MODE" else {}),
                },
            )
            return {
                "id": record.id,
                "runtime_id": runtime.id,
                "command": command,
                "status": record.status,
                "payload": values,
            }

    def status(self, account_id: int) -> dict:
        with self.db.session() as session:
            runtime = session.scalar(
                select(RuntimeInstance).where(
                    RuntimeInstance.account_id == account_id,
                    RuntimeInstance.active.is_(True),
                )
            )
            if runtime is None:
                raise WorkerControlError("active runtime not found")
            worker = session.get(WorkerStatus, runtime.id)
            if worker is None:
                return {
                    "runtime_id": runtime.id,
                    "worker_plugin": "unknown",
                    "worker_mode": "DISABLED",
                    "worker_state": "UNKNOWN",
                }
            return {
                "runtime_id": runtime.id,
                "worker_plugin": worker.worker_plugin,
                "worker_version": worker.worker_version,
                "worker_config_version": worker.worker_config_version,
                "worker_mode": worker.worker_mode,
                "worker_state": worker.automation_state,
                "last_tick_at": worker.last_tick_at.isoformat()
                if worker.last_tick_at
                else None,
                "last_action": worker.last_action,
                "last_observation_at": worker.last_observation_at.isoformat()
                if worker.last_observation_at
                else None,
                "worker_error_code": worker.error_code,
                "worker_restart_count": worker.restart_count,
                "details": worker.details,
            }
