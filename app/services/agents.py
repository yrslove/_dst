from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.errors import (
    AuthenticationRequired,
    NodeProtocolMismatch,
    ProtocolMismatch,
    RuntimeNotFound,
)
from app.domain.state import (
    GAME_READY_PHASES,
    transition_account,
    transition_node,
    transition_runtime,
)
from app.models import (
    Account,
    AccountState,
    DesiredState,
    ErrorCode,
    GameplayTask,
    GameplayTaskStatus,
    Node,
    NodeHeartbeat,
    NodeResourceSnapshot,
    NodeStatus,
    RuntimeHeartbeat,
    RuntimeInstance,
    RuntimeState,
    WorkerCommand,
    WorkerRun,
    WorkerStatus,
    utcnow,
)
from app.schemas import NodeHeartbeatRequest, RuntimeHeartbeatRequest
from app.services.gameplay import (
    persist_worker_gift_progress,
    persist_worker_inworld_gift_confirmation,
)
from app.services.leases import LeaseService
from app.services.records import add_event
from app.services.security import ensure_utc, token_matches

SENSITIVE_DETAIL_KEYS = {"password", "secret", "token", "cookie", "authorization"}


def _parse_agent_datetime(value) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=utcnow().tzinfo)
    except ValueError:
        return None


def _bounded_int(value, default: int = 0, maximum: int = 1_000_000) -> int:
    try:
        return min(max(0, int(value)), maximum)
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_float(value, maximum: float = 10**9) -> float:
    try:
        return min(max(0.0, float(value)), maximum)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _safe_details(value, *, depth: int = 0):
    """Keep bounded diagnostics useful without persisting credentials or blobs."""
    if depth > 7:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key)[:80]: "[REDACTED]"
            if any(word in str(key).lower() for word in SENSITIVE_DETAIL_KEYS)
            else _safe_details(item, depth=depth + 1)
            for key, item in list(value.items())[:32]
        }
    if isinstance(value, list):
        return [_safe_details(item, depth=depth + 1) for item in value[:32]]
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:500]


class AgentService:
    def __init__(self, db: Database, leases: LeaseService, settings: Settings):
        self.db = db
        self.leases = leases
        self.settings = settings

    def runtime_heartbeat(
        self, payload: RuntimeHeartbeatRequest, token: str | None
    ) -> dict:
        if payload.protocol_version != self.settings.agent_protocol_version:
            raise ProtocolMismatch("runtime agent protocol version is incompatible")
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            statement = select(RuntimeInstance).where(
                RuntimeInstance.id == payload.runtime_id
            )
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            runtime = session.scalar(statement)
            if runtime is None:
                raise RuntimeNotFound(f"runtime {payload.runtime_id} not found")
            if not token_matches(token or "", runtime.token_hash):
                raise AuthenticationRequired("invalid runtime agent token")
            if not runtime.active:
                raise AuthenticationRequired("runtime generation is inactive")
            identity = {
                "account_id": runtime.account_id,
                "node_id": runtime.node_id,
                "runtime_generation": runtime.runtime_generation,
                "runtime_image_version": runtime.image_version,
            }
            for field, expected in identity.items():
                reported = getattr(payload, field)
                if field == "runtime_image_version" and reported == "unknown":
                    reported = None
                if reported is not None and reported != expected:
                    raise AuthenticationRequired(
                        f"runtime identity mismatch for {field}"
                    )
            if runtime.state not in {
                RuntimeState.STARTING,
                RuntimeState.RUNNING,
                RuntimeState.STOPPING,
                RuntimeState.STALE,
            } and not (
                runtime.state == RuntimeState.ERROR
                and runtime.desired_state == DesiredState.RUNNING
            ):
                raise AuthenticationRequired("runtime is not accepting heartbeats")
            runtime.last_heartbeat_at = now
            runtime.agent_version = payload.agent_version
            # Steam sign-in is distinct from full runtime verification. Once the
            # managed Steam adapter has proved readiness, the account is past the
            # login step even while DST/game verification is still pending.
            if (
                runtime.verified_at is None
                and runtime.state == RuntimeState.RUNNING
                and payload.steam_running
                and payload.healthy
                and payload.phase
                in {"STEAM_READY", "DST_STARTING", "GAME_READY", "WORKER_IDLE"}
            ):
                account = session.get(Account, runtime.account_id)
                if account and account.status == AccountState.NEEDS_LOGIN:
                    transition_account(account, AccountState.VERIFYING)
                    add_event(
                        session,
                        level="INFO",
                        kind="STEAM_AUTHENTICATED",
                        message="Steam is ready; runtime verification is pending",
                        account_id=account.id,
                        runtime_id=runtime.id,
                    )
            latest = session.scalar(
                select(RuntimeHeartbeat)
                .where(RuntimeHeartbeat.runtime_id == runtime.id)
                .order_by(RuntimeHeartbeat.received_at.desc())
                .limit(1)
            )
            if latest is None or ensure_utc(latest.received_at) < now - timedelta(
                seconds=self.settings.resource_sample_seconds
            ):
                session.add(
                    RuntimeHeartbeat(
                        runtime_id=runtime.id,
                        phase=payload.phase,
                        steam_running=payload.steam_running,
                        dst_running=payload.dst_running,
                        healthy=payload.healthy,
                        agent_version=payload.agent_version,
                        protocol_version=payload.protocol_version,
                        details=_safe_details(
                            {
                                "capabilities": payload.capabilities,
                                "diagnostics": payload.details,
                            }
                        ),
                        received_at=now,
                    )
                )
            worker = session.get(WorkerStatus, runtime.id)
            report = _safe_details(payload.worker)
            requested_mode = str(report.get("mode", "DISABLED"))
            if requested_mode not in {"DISABLED", "OBSERVE", "ACTIVE"}:
                requested_mode = "DISABLED"
            gate_error = None
            if requested_mode == "ACTIVE" and (
                runtime.verified_at is None or runtime.state != RuntimeState.RUNNING
            ):
                requested_mode = "DISABLED"
                gate_error = "WORKER_DISABLED"
            values = {
                "phase": payload.phase,
                "steam_running": payload.steam_running,
                "dst_running": payload.dst_running,
                "healthy": payload.healthy,
                "automation_state": "NEEDS_ATTENTION"
                if gate_error
                else payload.worker_state,
                "worker_plugin": str(report.get("plugin", payload.worker_plugin))[:80],
                "worker_version": str(report.get("version", "unknown"))[:32],
                "worker_config_version": _bounded_int(
                    report.get("config_version", 1), default=1, maximum=10_000
                ),
                "worker_mode": requested_mode,
                "last_tick_at": _parse_agent_datetime(
                    report.get("last_tick_at") or payload.worker_last_tick_at
                ),
                "last_action": str(
                    report.get("last_action") or payload.worker_last_action
                )[:80]
                if report.get("last_action") or payload.worker_last_action
                else None,
                "last_observation_at": _parse_agent_datetime(
                    report.get("last_observation_at")
                    or payload.worker_last_observation_at
                ),
                "error_code": gate_error
                or (
                    str(report.get("error_code") or payload.worker_error_code)[:80]
                    if report.get("error_code") or payload.worker_error_code
                    else None
                ),
                "restart_count": _bounded_int(
                    report.get("restart_count", payload.worker_restart_count),
                    maximum=1_000,
                ),
                "details": _safe_details(
                    {
                        "capabilities": payload.capabilities,
                        "diagnostics": payload.details,
                        "reported_identity": {
                            "account_id": payload.account_id,
                            "node_id": payload.node_id,
                            "runtime_generation": payload.runtime_generation,
                            "runtime_image_version": payload.runtime_image_version,
                            "process_identities": payload.process_identities,
                        },
                    }
                ),
                "updated_at": now,
            }
            if worker is None:
                worker = WorkerStatus(runtime_id=runtime.id, **values)
                session.add(worker)
            else:
                for key, value in values.items():
                    setattr(worker, key, value)
            account = session.get(Account, runtime.account_id)
            node = session.get(Node, runtime.node_id)
            current_runtime_id = session.scalar(
                select(RuntimeInstance.id).where(
                    RuntimeInstance.account_id == runtime.account_id,
                    RuntimeInstance.active.is_(True),
                )
            )
            if (
                runtime.state == RuntimeState.STALE
                and runtime.desired_state == DesiredState.RUNNING
                and runtime.active
                and runtime.verified_at is not None
                and current_runtime_id == runtime.id
                and runtime.last_error_code == ErrorCode.AGENT_STALE
                and account is not None
                and account.enabled
                and account.status
                in {AccountState.RUNNING, AccountState.NEEDS_ATTENTION}
                and node is not None
                and node.status == NodeStatus.ONLINE
                and payload.healthy
                and payload.steam_running
                and payload.dst_running
                and payload.phase in GAME_READY_PHASES
                and worker.healthy
                and worker.steam_running
                and worker.dst_running
                and worker.phase in GAME_READY_PHASES
                and (
                    worker.error_code is None
                    or (
                        worker.error_code == "WORKER_DISABLED"
                        and (
                            gate_error == "WORKER_DISABLED"
                            or (
                                requested_mode == "DISABLED"
                                and payload.worker_state == "DISABLED"
                                and report.get("error_code") == "WORKER_DISABLED"
                            )
                        )
                        and (
                            not (report.get("error_code") or payload.worker_error_code)
                            or (
                                requested_mode == "DISABLED"
                                and payload.worker_state == "DISABLED"
                                and report.get("error_code") == "WORKER_DISABLED"
                                and payload.worker_error_code
                                in {None, "WORKER_DISABLED"}
                            )
                        )
                    )
                )
            ):
                transition_runtime(runtime, RuntimeState.RUNNING)
                runtime.last_error_code = None
                runtime.last_error_message = None
                if account.status == AccountState.NEEDS_ATTENTION:
                    transition_account(account, AccountState.RUNNING)
                add_event(
                    session,
                    level="INFO",
                    kind="RUNTIME_RECOVERED",
                    message="Runtime recovered from stale heartbeat after readiness report",
                    account_id=account.id,
                    runtime_id=runtime.id,
                    node_id=node.id,
                )
            for result in payload.worker_command_results[:32]:
                if not isinstance(result, dict):
                    continue
                try:
                    command_id = int(result.get("id"))
                except (TypeError, ValueError):
                    continue
                command = session.get(WorkerCommand, command_id)
                if (
                    command
                    and command.runtime_id == runtime.id
                    and command.status != "COMPLETED"
                ):
                    command.status = "COMPLETED"
                    command.completed_at = now
                    command.result = str(result.get("result", "OK"))[:80]
            self._update_worker_run(session, runtime, report, requested_mode, now)
            desired_worker_state = worker.desired_worker_state
            target_mode = None
            active_gameplay_task = session.scalar(
                select(GameplayTask.id)
                .where(
                    GameplayTask.runtime_id == runtime.id,
                    GameplayTask.status.in_(
                        [GameplayTaskStatus.PENDING, GameplayTaskStatus.RUNNING]
                    ),
                )
                .limit(1)
            )
            if desired_worker_state == "DISABLED" and worker.worker_mode != "DISABLED":
                target_mode = "DISABLED"
            elif (
                desired_worker_state == "ACTIVE_STATIONARY"
                and active_gameplay_task is None
                and runtime.state == RuntimeState.RUNNING
                and runtime.verified_at is not None
                and payload.phase in GAME_READY_PHASES
                and requested_mode != "ACTIVE"
            ):
                target_mode = "ACTIVE"
            unresolved = session.scalar(
                select(WorkerCommand.id)
                .where(
                    WorkerCommand.runtime_id == runtime.id,
                    WorkerCommand.status.in_(["PENDING", "DELIVERED"]),
                )
                .limit(1)
            )
            if target_mode and unresolved is None:
                session.add(
                    WorkerCommand(
                        runtime_id=runtime.id,
                        command="SET_MODE",
                        payload={"mode": target_mode},
                        status="PENDING",
                        created_by="durable_worker_intent",
                        created_at=now,
                    )
                )
            commands = list(
                session.scalars(
                    select(WorkerCommand)
                    .where(
                        WorkerCommand.runtime_id == runtime.id,
                        WorkerCommand.status.in_(["PENDING", "DELIVERED"]),
                    )
                    .order_by(WorkerCommand.id)
                    # Worker lifecycle commands are stateful. Deliver exactly one
                    # unresolved command so STOP/RESUME and PAUSE/RESUME cannot
                    # race each other inside the isolated worker process.
                    .limit(1)
                )
            )
            for command in commands:
                command.status = "DELIVERED"
                command.delivered_at = now
            response = {
                "ok": True,
                "runtime_verified": runtime.verified_at is not None
                and runtime.state == RuntimeState.RUNNING,
                "runtime_state": str(runtime.state),
                "runtime_image_version": runtime.image_version,
                "control_plane_last_seen_at": now.isoformat(),
                "control_plane_verified_at": (
                    ensure_utc(runtime.verified_at).isoformat()
                    if runtime.verified_at
                    else None
                ),
                "computed_stale_reason": (
                    str(runtime.last_error_code)
                    if runtime.state == RuntimeState.STALE
                    else ("VERIFICATION_NOT_ESTABLISHED" if not runtime.verified_at else None)
                ),
                "desired_worker_state": desired_worker_state,
                "commands": [
                    {"id": item.id, "command": item.command, "payload": item.payload}
                    for item in commands
                ],
            }
        self.leases.renew_slot(payload.runtime_id)
        return response

    def _update_worker_run(
        self, session, runtime: RuntimeInstance, report: dict, mode: str, now
    ) -> None:
        active_states = {
            "OBSERVING",
            "READY",
            "IDLE_ACTIVITY",
            "NAVIGATING",
            "INTERACTING",
            "WAITING",
            "RECOVERING",
        }
        current = session.scalar(
            select(WorkerRun)
            .where(WorkerRun.runtime_id == runtime.id, WorkerRun.ended_at.is_(None))
            .order_by(WorkerRun.id.desc())
            .limit(1)
        )
        state = str(report.get("state", "DISABLED"))
        telemetry = (
            report.get("telemetry") if isinstance(report.get("telemetry"), dict) else {}
        )
        if state in active_states and mode in {"OBSERVE", "ACTIVE"} and current is None:
            current = WorkerRun(
                runtime_id=runtime.id,
                account_id=runtime.account_id,
                plugin=str(report.get("plugin", "unknown"))[:80],
                mode=mode,
                started_at=now,
            )
            session.add(current)
        if current:
            current.worker_active_seconds = _bounded_float(
                telemetry.get("worker_active_seconds", 0)
            )
            current.pause_seconds = _bounded_float(telemetry.get("pause_seconds", 0))
            current.actions_count = _bounded_int(telemetry.get("actions_count", 0))
            current.recoveries = _bounded_int(telemetry.get("recoveries", 0))
            if state in {"DISABLED", "STOPPED", "ERROR", "NEEDS_ATTENTION"}:
                current.ended_at = now
                current.result = state
        persist_worker_gift_progress(
            session,
            account_id=runtime.account_id,
            runtime_id=runtime.id,
            worker_run=current,
            report=report,
            worker_state=state,
            now=now,
            sqlite=self.db.is_sqlite,
        )
        persist_worker_inworld_gift_confirmation(
            session,
            account_id=runtime.account_id,
            runtime_id=runtime.id,
            worker_run=current,
            report=report,
            now=now,
            sqlite=self.db.is_sqlite,
        )

    def node_heartbeat(self, payload: NodeHeartbeatRequest, token: str | None) -> None:
        if payload.protocol_version != self.settings.agent_protocol_version:
            raise NodeProtocolMismatch("node agent protocol version is incompatible")
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            statement = select(Node).where(Node.id == payload.node_id)
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            node = session.scalar(statement)
            if node is None:
                raise RuntimeNotFound(f"node {payload.node_id} not found")
            if not token_matches(token or "", node.token_hash):
                raise AuthenticationRequired("invalid node agent token")
            node.last_heartbeat_at = now
            node.agent_version = payload.agent_version
            if not node.maintenance and not node.draining:
                target = (
                    NodeStatus.ONLINE if payload.incus_available else NodeStatus.ERROR
                )
                if node.status != target:
                    transition_node(node, target)
            latest = session.scalar(
                select(NodeHeartbeat)
                .where(NodeHeartbeat.node_id == node.id)
                .order_by(NodeHeartbeat.received_at.desc())
                .limit(1)
            )
            sample = latest is None or ensure_utc(latest.received_at) < now - timedelta(
                seconds=self.settings.resource_sample_seconds
            )
            if sample:
                session.add(
                    NodeHeartbeat(
                        node_id=node.id,
                        agent_version=payload.agent_version,
                        protocol_version=payload.protocol_version,
                        incus_available=payload.incus_available,
                        active_runtime_count=payload.active_runtime_count,
                        received_at=now,
                    )
                )
                metrics = payload.resources
                session.add(
                    NodeResourceSnapshot(
                        node_id=node.id,
                        cpu_percent=metrics.cpu_percent,
                        load_1=metrics.load_1,
                        load_5=metrics.load_5,
                        load_15=metrics.load_15,
                        ram_used_bytes=metrics.ram_used_bytes,
                        ram_total_bytes=metrics.ram_total_bytes,
                        disk_used_bytes=metrics.disk_used_bytes,
                        disk_total_bytes=metrics.disk_total_bytes,
                        gpu_present=metrics.gpu_present,
                        gpu_utilization=metrics.gpu_utilization,
                        vram_used_bytes=metrics.vram_used_bytes,
                        vram_total_bytes=metrics.vram_total_bytes,
                        active_runtime_count=payload.active_runtime_count,
                        captured_at=now,
                    )
                )
            if sample or not payload.incus_available:
                add_event(
                    session,
                    level="WARNING" if not payload.incus_available else "INFO",
                    kind="NODE_HEARTBEAT",
                    message="Node heartbeat received",
                    node_id=node.id,
                    metadata={
                        "incus_available": payload.incus_available,
                        "capabilities": _safe_details(payload.capabilities),
                    },
                )
