from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db import Database
from app.domain.errors import AccountNotFound, NodeOffline
from app.domain.state import transition_account, transition_node
from app.models import (
    Account,
    AccountScheduleState,
    AccountSecret,
    AccountState,
    AuditEvent,
    DesiredState,
    Event,
    GameplayTask,
    GameplayTaskStatus,
    Job,
    JobKind,
    Node,
    NodeResourceSnapshot,
    NodeStatus,
    Run,
    RuntimeImage,
    RuntimeInstance,
    RuntimeState,
    WorkerStatus,
    utcnow,
)
from app.schemas import AccountCreate
from app.services.jobs import ACTIVE_JOB_STATUSES, JobQueue
from app.services.leases import occupied_runtime_ids
from app.services.records import add_audit, add_event
from app.services.runtime_images import RuntimeImageNotVerified
from app.services.secrets import SecretsService
from app.services.security import ensure_utc, generate_token, hash_token


class DuplicateAccountError(RuntimeError):
    pass


class AccountBusyError(RuntimeError):
    pass


def _dt(value):
    return value.isoformat() if value else None


class AccountService:
    def __init__(
        self,
        db: Database,
        secrets_service: SecretsService,
        jobs: JobQueue,
        settings: Settings,
    ):
        self.db = db
        self.secrets = secrets_service
        self.jobs = jobs
        self.settings = settings

    def ensure_default_node(self) -> int:
        with self.db.transaction(immediate=True) as session:
            node = session.scalar(
                select(Node).where(Node.name == self.settings.default_node_name)
            )
            if node is None:
                node = Node(
                    name=self.settings.default_node_name,
                    provider=self.settings.runtime_provider,
                    incus_remote=self.settings.incus_remote,
                    max_active_slots=self.settings.default_node_max_active_slots,
                    token_hash=(
                        hash_token(self.settings.default_node_secret)
                        if self.settings.default_node_secret
                        else None
                    ),
                    status=(
                        NodeStatus.ONLINE
                        if self.settings.environment in {"development", "test"}
                        and self.settings.runtime_provider == "mock"
                        else NodeStatus.OFFLINE
                    ),
                )
                session.add(node)
                session.flush()
            return node.id

    def create(
        self,
        payload: AccountCreate,
        *,
        node_id: int,
        actor: str,
        request_id: str,
    ) -> tuple[dict, Job]:
        try:
            with self.db.transaction(immediate=True) as session:
                node = session.get(Node, node_id)
                if node is None:
                    raise NodeOffline("selected node does not exist")
                account = Account(
                    label=payload.label,
                    steam_username=payload.steam_username,
                    email=payload.email,
                    klei_email=payload.klei_email or payload.email,
                    notes=payload.notes,
                    status=AccountState.NEW,
                )
                session.add(account)
                session.flush()
                session.add(
                    AccountScheduleState(
                        account_id=account.id,
                        daily_status="UNKNOWN",
                        weekly_state="UNSYNCED_CURRENT_CYCLE",
                        weekly_collected=None,
                        confirmed_claims_current_observation=0,
                        weekly_target=8,
                        phase="FARMING",
                        pending_gift=False,
                        schedule_revision=0,
                    )
                )
                session.add(
                    AccountSecret(
                        account_id=account.id,
                        steam_password_enc=self.secrets.encrypt(payload.steam_password),
                        email_password_enc=self.secrets.encrypt(payload.email_password),
                    )
                )
                runtime = RuntimeInstance(
                    account_id=account.id,
                    node_id=node.id,
                    provider=node.provider,
                    external_id=f"dst-{account.id:06d}-g1",
                    network_profile=payload.network_profile or self.settings.incus_profile,
                    runtime_generation=1,
                    image_version=self.settings.current_image_version,
                    state=RuntimeState.NEW,
                    desired_state=DesiredState.STOPPED,
                    token_hash=hash_token(runtime_token := generate_token()),
                    runtime_token_enc=self.secrets.encrypt(runtime_token),
                )
                session.add(runtime)
                session.flush()
                job = self.jobs.enqueue_in_session(
                    session,
                    kind=JobKind.PROVISION_RUNTIME,
                    account_id=account.id,
                    runtime_id=runtime.id,
                    node_id=node.id,
                    request_id=request_id,
                    idempotency_key=f"provision:{runtime.id}:g1",
                )
                add_event(
                    session,
                    level="INFO",
                    kind="ACCOUNT_CREATED",
                    message="Account and runtime generation created",
                    account_id=account.id,
                    runtime_id=runtime.id,
                    node_id=node.id,
                    request_id=request_id,
                )
                add_audit(
                    session,
                    actor=actor,
                    action="ADD_ACCOUNT",
                    entity_type="account",
                    entity_id=account.id,
                    request_id=request_id,
                )
                session.flush()
                account_id = account.id
                job_id = job.id
        except IntegrityError as exc:
            if (
                "steam_username" in str(exc).lower()
                or "accounts.steam_username" in str(exc).lower()
            ):
                raise DuplicateAccountError(payload.steam_username) from exc
            raise
        account_data = self.get(account_id)
        job = self.jobs.get(job_id)
        assert job is not None
        return account_data, job

    def _active_runtime(
        self, session, account_id: int, *, lock: bool = False
    ) -> RuntimeInstance:
        statement = select(RuntimeInstance).where(
            RuntimeInstance.account_id == account_id,
            RuntimeInstance.active.is_(True),
        )
        if lock and not self.db.is_sqlite:
            statement = statement.with_for_update()
        runtime = session.scalar(statement)
        if runtime is None:
            raise AccountNotFound(f"account {account_id} has no active runtime")
        return runtime

    def command(
        self,
        account_id: int,
        kind: JobKind,
        *,
        actor: str,
        request_id: str,
        idempotency_key: str | None = None,
        payload: dict | None = None,
    ) -> Job:
        if kind == JobKind.LONG_SESSION:
            duration = (payload or {}).get("requested_duration")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or not 600 <= duration <= 5400
            ):
                raise ValueError("session duration must be 600..5400 seconds")
        with self.db.transaction(immediate=True) as session:
            account_stmt = select(Account).where(Account.id == account_id)
            if not self.db.is_sqlite:
                account_stmt = account_stmt.with_for_update()
            account = session.scalar(account_stmt)
            if account is None:
                raise AccountNotFound(f"account {account_id} not found")
            if idempotency_key:
                replay = session.scalar(
                    select(Job).where(
                        Job.idempotency_key
                        == f"client:{account_id}:{kind}:{idempotency_key}"
                    )
                )
                if replay is not None:
                    session.expunge(replay)
                    return replay
            runtime = self._active_runtime(session, account_id, lock=True)
            node = session.get(Node, runtime.node_id)
            if node is None:
                raise NodeOffline("runtime node does not exist")
            active_command = session.scalar(
                select(Job)
                .where(
                    Job.account_id == account_id,
                    Job.status.in_([str(item) for item in ACTIVE_JOB_STATUSES]),
                )
                .order_by(Job.id.desc())
            )
            if active_command is not None:
                if active_command.kind != str(kind):
                    raise AccountBusyError(
                        f"wait for active {active_command.kind} job {active_command.id}"
                    )
                session.expunge(active_command)
                return active_command

            if kind == JobKind.SETUP_RUNTIME:
                if (
                    not account.enabled
                    or runtime.verified_at
                    or runtime.state
                    not in {
                        RuntimeState.NEEDS_LOGIN,
                        RuntimeState.STOPPED,
                        RuntimeState.ERROR,
                        RuntimeState.STALE,
                    }
                ):
                    raise AccountBusyError(
                        "setup requires an unverified provisioned runtime"
                    )
                runtime.desired_state = DesiredState.RUNNING
                runtime.command_version += 1
            elif kind in {JobKind.START_RUNTIME, JobKind.RESTART_RUNTIME}:
                if not account.enabled:
                    raise AccountBusyError("disabled account cannot be started")
                if (
                    node.maintenance
                    or node.draining
                    or not node.enabled
                    or node.status != NodeStatus.ONLINE
                ):
                    raise NodeOffline(f"node {node.name} does not accept starts")
                if runtime.state in {
                    RuntimeState.NEW,
                    RuntimeState.PROVISIONING,
                    RuntimeState.NEEDS_LOGIN,
                }:
                    raise AccountBusyError(f"runtime is not verified ({runtime.state})")
                if runtime.verified_at is None:
                    raise AccountBusyError("complete SETUP and VERIFY before START")
                previous = runtime.desired_state
                runtime.desired_state = DesiredState.RUNNING
                if (
                    previous != DesiredState.RUNNING
                    or runtime.state
                    in {
                        RuntimeState.ERROR,
                        RuntimeState.STALE,
                        RuntimeState.STOPPED,
                    }
                    or kind == JobKind.RESTART_RUNTIME
                ):
                    runtime.command_version += 1
                if account.status not in {AccountState.RUNNING, AccountState.QUEUED}:
                    transition_account(account, AccountState.QUEUED)
            elif kind in {JobKind.DAILY_GIFT_CLAIM, JobKind.LONG_SESSION}:
                if not account.enabled or runtime.verified_at is None:
                    raise AccountBusyError(
                        "daily gift task requires a verified enabled account"
                    )
                if (
                    node.maintenance
                    or node.draining
                    or not node.enabled
                    or node.status != NodeStatus.ONLINE
                ):
                    raise NodeOffline(
                        f"node {node.name} does not accept gameplay tasks"
                    )
                active_task = session.scalar(
                    select(GameplayTask).where(
                        GameplayTask.account_id == account.id,
                        GameplayTask.kind == str(kind),
                        GameplayTask.status.in_(["PENDING", "RUNNING"]),
                    )
                )
                if active_task is not None:
                    raise AccountBusyError(
                        f"daily gift task {active_task.id} is still {active_task.status}"
                    )
                if runtime.desired_state != DesiredState.RUNNING:
                    runtime.command_version += 1
                runtime.desired_state = DesiredState.RUNNING
                if runtime.state in {RuntimeState.STOPPED, RuntimeState.STALE}:
                    runtime.command_version += 1
                if account.status not in {AccountState.RUNNING, AccountState.QUEUED}:
                    transition_account(account, AccountState.QUEUED)
            elif kind == JobKind.STOP_RUNTIME:
                if runtime.desired_state != DesiredState.STOPPED:
                    runtime.command_version += 1
                runtime.desired_state = DesiredState.STOPPED
            elif kind in {
                JobKind.REBUILD_RUNTIME,
                JobKind.VERIFY_RUNTIME,
                JobKind.MOVE_RUNTIME,
            }:
                runtime.command_version += 1
                if kind == JobKind.REBUILD_RUNTIME:
                    image_version = (payload or {}).get("image_version")
                    if image_version is None:
                        image_version = self.settings.current_image_version
                    if not isinstance(image_version, str) or not image_version:
                        raise RuntimeImageNotVerified(
                            "rebuild requires a valid runtime image version"
                        )
                    image = session.get(RuntimeImage, image_version)
                    if (
                        image is None
                        or image.provider != runtime.provider
                        or not image.verified
                    ):
                        raise RuntimeImageNotVerified(
                            "selected runtime image is not verified for this provider"
                        )
                    payload = {**(payload or {}), "image_version": image_version}
                    runtime.desired_state = DesiredState.STOPPED

            key = (
                f"client:{account_id}:{kind}:{idempotency_key}"
                if idempotency_key
                else (
                    f"daily-gift:{runtime.id}:{uuid.uuid4().hex}"
                    if kind in {JobKind.DAILY_GIFT_CLAIM, JobKind.LONG_SESSION}
                    else f"command:{runtime.id}:{kind}:v{runtime.command_version}"
                )
            )
            existing = session.scalar(select(Job).where(Job.idempotency_key == key))
            if existing is not None:
                session.expunge(existing)
                return existing
            task = None
            job_payload = {
                **(payload or {}),
                "command_version": runtime.command_version,
            }
            if kind in {JobKind.DAILY_GIFT_CLAIM, JobKind.LONG_SESSION}:
                now = utcnow()
                task = GameplayTask(
                    account_id=account.id,
                    runtime_id=runtime.id,
                    kind=str(kind),
                    status=GameplayTaskStatus.PENDING,
                    result_json=(
                        {
                            "requested_duration": job_payload["requested_duration"],
                            "state": "PENDING",
                            "checkpoints": 0,
                            "recoveries": 0,
                            "gift_transitions": [],
                            "confirmations": [],
                        }
                        if kind == JobKind.LONG_SESSION
                        else None
                    ),
                    started_at=now,
                    updated_at=now,
                )
                session.add(task)
                session.flush()
                job_payload["gameplay_task_id"] = task.id
            job = self.jobs.enqueue_in_session(
                session,
                kind=kind,
                account_id=account.id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=request_id,
                idempotency_key=key,
                payload=job_payload,
            )
            add_event(
                session,
                level="INFO",
                kind=f"{kind}_QUEUED",
                message=f"{kind} queued",
                account_id=account.id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=request_id,
            )
            add_audit(
                session,
                actor=actor,
                action=str(kind).removesuffix("_RUNTIME"),
                entity_type="account",
                entity_id=account.id,
                request_id=request_id,
                metadata={"job_id": job.id},
            )
            session.flush()
            session.expunge(job)
            return job

    def list(self) -> list[dict]:
        with self.db.session() as session:
            rows = session.execute(
                select(Account, RuntimeInstance, Node, WorkerStatus)
                .join(
                    RuntimeInstance,
                    (RuntimeInstance.account_id == Account.id)
                    & RuntimeInstance.active.is_(True),
                )
                .join(Node, Node.id == RuntimeInstance.node_id)
                .outerjoin(WorkerStatus, WorkerStatus.runtime_id == RuntimeInstance.id)
                .order_by(Account.id)
            ).all()
            return [
                self._serialize(account, runtime, node, worker)
                for account, runtime, node, worker in rows
            ]

    def get(self, account_id: int) -> dict:
        with self.db.session() as session:
            row = session.execute(
                select(Account, RuntimeInstance, Node, WorkerStatus)
                .join(
                    RuntimeInstance,
                    (RuntimeInstance.account_id == Account.id)
                    & RuntimeInstance.active.is_(True),
                )
                .join(Node, Node.id == RuntimeInstance.node_id)
                .outerjoin(WorkerStatus, WorkerStatus.runtime_id == RuntimeInstance.id)
                .where(Account.id == account_id)
            ).first()
            if row is None:
                raise AccountNotFound(f"account {account_id} not found")
            return self._serialize(*row)

    @staticmethod
    def _serialize(
        account: Account,
        runtime: RuntimeInstance,
        node: Node,
        worker: WorkerStatus | None,
    ) -> dict:
        return {
            "id": account.id,
            "label": account.label,
            "steam_username": account.steam_username,
            "email": account.email,
            "klei_email": account.klei_email,
            "notes": account.notes,
            "enabled": account.enabled,
            "status": account.status,
            "created_at": _dt(account.created_at),
            "updated_at": _dt(account.updated_at),
            "runtime_id": runtime.id,
            "runtime_generation": runtime.runtime_generation,
            "verified_at": _dt(runtime.verified_at),
            "image_version": runtime.image_version,
            "bootstrap_version": runtime.bootstrap_version,
            "bootstrap_phase": runtime.bootstrap_phase,
            "bootstrap_completed_at": _dt(runtime.bootstrap_completed_at),
            "external_id": runtime.external_id,
            "state": runtime.state,
            "desired_state": runtime.desired_state,
            "network_profile": runtime.network_profile,
            "last_error_code": runtime.last_error_code,
            "last_error_message": runtime.last_error_message,
            "last_heartbeat_at": _dt(runtime.last_heartbeat_at),
            "node_id": node.id,
            "node_name": node.name,
            "node_status": node.status,
            "worker_phase": worker.phase if worker else None,
            "steam_running": worker.steam_running if worker else False,
            "dst_running": worker.dst_running if worker else False,
            "worker_healthy": worker.healthy if worker else False,
            "automation_state": worker.automation_state if worker else "NOOP",
            "worker_plugin": worker.worker_plugin if worker else "unknown",
            "worker_version": worker.worker_version if worker else None,
            "worker_config_version": worker.worker_config_version if worker else None,
            "worker_mode": worker.worker_mode if worker else "DISABLED",
            "worker_state": worker.automation_state if worker else "UNKNOWN",
            "worker_last_tick_at": _dt(worker.last_tick_at) if worker else None,
            "worker_last_action": worker.last_action if worker else None,
            "worker_last_observation_at": _dt(worker.last_observation_at)
            if worker
            else None,
            "worker_error_code": worker.error_code if worker else None,
            "worker_restart_count": worker.restart_count if worker else 0,
            "runtime_health": {
                "container": runtime.state,
                "agent": "ONLINE" if runtime.last_heartbeat_at else "OFFLINE",
                "display": (
                    worker.details.get("diagnostics", {})
                    .get("display", {})
                    .get("state")
                    if worker
                    else "UNKNOWN"
                ),
                "steam": (
                    worker.details.get("diagnostics", {})
                    .get("readiness", {})
                    .get("steam")
                    if worker
                    else "UNKNOWN"
                ),
                "dst": (
                    worker.details.get("diagnostics", {})
                    .get("readiness", {})
                    .get("dst")
                    if worker
                    else "UNKNOWN"
                ),
                "worker": worker.automation_state if worker else "NOOP",
            },
        }

    def history(self, account_id: int) -> list[dict]:
        with self.db.session() as session:
            if session.get(Account, account_id) is None:
                raise AccountNotFound(f"account {account_id} not found")
            runtimes = list(
                session.scalars(
                    select(RuntimeInstance)
                    .where(RuntimeInstance.account_id == account_id)
                    .order_by(RuntimeInstance.runtime_generation.desc())
                )
            )
            return [
                {
                    "id": runtime.id,
                    "generation": runtime.runtime_generation,
                    "active": runtime.active,
                    "external_id": runtime.external_id,
                    "image_version": runtime.image_version,
                    "state": runtime.state,
                    "node_id": runtime.node_id,
                    "created_at": _dt(runtime.created_at),
                }
                for runtime in runtimes
            ]

    def runs(self, account_id: int, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.db.session() as session:
            if session.get(Account, account_id) is None:
                raise AccountNotFound(f"account {account_id} not found")
            records = list(
                session.scalars(
                    select(Run)
                    .where(Run.account_id == account_id)
                    .order_by(Run.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [
                {
                    "id": record.id,
                    "account_id": record.account_id,
                    "runtime_id": record.runtime_id,
                    "node_id": record.node_id,
                    "started_at": _dt(record.started_at),
                    "ended_at": _dt(record.ended_at),
                    "duration_seconds": record.duration_seconds,
                    "result": record.result,
                    "start_reason": record.start_reason,
                    "error_code": record.error_code,
                }
                for record in records
            ]

    def set_enabled(
        self, account_id: int, enabled: bool, *, actor: str, request_id: str
    ) -> dict:
        with self.db.transaction(immediate=True) as session:
            statement = select(Account).where(Account.id == account_id)
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            account = session.scalar(statement)
            if account is None:
                raise AccountNotFound(f"account {account_id} not found")
            runtime = self._active_runtime(session, account_id, lock=True)
            pending = session.scalar(
                select(Job.id).where(
                    Job.account_id == account_id,
                    Job.status.in_(list(ACTIVE_JOB_STATUSES)),
                )
            )
            if not enabled and (
                pending or runtime.id in occupied_runtime_ids(session, runtime.node_id)
            ):
                raise AccountBusyError(
                    "finish pending commands and confirm stopped runtime before disabling"
                )
            if not enabled and runtime.state in {
                RuntimeState.STARTING,
                RuntimeState.RUNNING,
            }:
                raise AccountBusyError(
                    "stop active runtime before disabling the account"
                )
            account.enabled = enabled
            if enabled:
                target = {
                    RuntimeState.NEEDS_LOGIN: AccountState.NEEDS_LOGIN,
                    RuntimeState.ERROR: AccountState.NEEDS_ATTENTION,
                }.get(runtime.state, AccountState.READY)
                transition_account(account, target)
            else:
                transition_account(account, AccountState.DISABLED)
            add_audit(
                session,
                actor=actor,
                action="ENABLE_ACCOUNT" if enabled else "DISABLE_ACCOUNT",
                entity_type="account",
                entity_id=account_id,
                request_id=request_id,
            )
        return self.get(account_id)

    def nodes(self) -> list[dict]:
        with self.db.session() as session:
            nodes = list(session.scalars(select(Node).order_by(Node.id)))
            result = []
            for node in nodes:
                active = len(occupied_runtime_ids(session, node.id))
                runtime_count = (
                    session.scalar(
                        select(func.count(RuntimeInstance.id)).where(
                            RuntimeInstance.node_id == node.id,
                            RuntimeInstance.active.is_(True),
                        )
                    )
                    or 0
                )
                snapshot = session.scalar(
                    select(NodeResourceSnapshot)
                    .where(NodeResourceSnapshot.node_id == node.id)
                    .order_by(NodeResourceSnapshot.captured_at.desc())
                    .limit(1)
                )
                result.append(
                    {
                        "id": node.id,
                        "name": node.name,
                        "provider": node.provider,
                        "incus_remote": node.incus_remote,
                        "status": node.status,
                        "enabled": node.enabled,
                        "maintenance": node.maintenance,
                        "draining": node.draining,
                        "max_active_slots": node.max_active_slots,
                        "active_slots": active,
                        "runtime_count": runtime_count,
                        "last_heartbeat_at": _dt(node.last_heartbeat_at),
                        "agent_version": node.agent_version,
                        "resources": (
                            {
                                "cpu_percent": snapshot.cpu_percent,
                                "ram_used_bytes": snapshot.ram_used_bytes,
                                "ram_total_bytes": snapshot.ram_total_bytes,
                                "gpu_present": snapshot.gpu_present,
                                "gpu_utilization": snapshot.gpu_utilization,
                                "vram_used_bytes": snapshot.vram_used_bytes,
                                "vram_total_bytes": snapshot.vram_total_bytes,
                                "captured_at": _dt(snapshot.captured_at),
                            }
                            if snapshot
                            else None
                        ),
                    }
                )
            return result

    def node_action(
        self, node_id: int, action: str, *, actor: str, request_id: str
    ) -> dict:
        with self.db.transaction(immediate=True) as session:
            statement = select(Node).where(Node.id == node_id)
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            node = session.scalar(statement)
            if node is None:
                raise KeyError(node_id)
            active = len(occupied_runtime_ids(session, node.id))
            if action == "drain":
                node.draining = True
                node.maintenance = False
                transition_node(node, NodeStatus.DRAINING)
            elif action == "maintenance":
                if active:
                    raise AccountBusyError(
                        "drain and stop all runtimes before maintenance"
                    )
                node.draining = False
                node.maintenance = True
                transition_node(node, NodeStatus.MAINTENANCE)
            elif action == "exit-maintenance":
                node.maintenance = False
                node.draining = False
                transition_node(
                    node,
                    NodeStatus.ONLINE
                    if node.provider == "mock"
                    or (
                        node.last_heartbeat_at
                        and ensure_utc(node.last_heartbeat_at)
                        > utcnow() - timedelta(seconds=self.settings.node_stale_seconds)
                    )
                    else NodeStatus.OFFLINE,
                )
            elif action == "enable":
                node.enabled = True
            elif action == "disable":
                if active:
                    raise AccountBusyError(
                        "drain and stop all runtimes before disabling node"
                    )
                node.enabled = False
            else:
                raise ValueError("unknown node action")
            add_audit(
                session,
                actor=actor,
                action=f"NODE_{action.upper().replace('-', '_')}",
                entity_type="node",
                entity_id=node.id,
                request_id=request_id,
            )
        return next(node for node in self.nodes() if node["id"] == node_id)

    def rotate_node_token(self, node_id: int, *, actor: str, request_id: str) -> str:
        token = generate_token()
        with self.db.transaction(immediate=True) as session:
            node = session.get(Node, node_id)
            if node is None:
                raise KeyError(node_id)
            node.token_hash = hash_token(token)
            add_audit(
                session,
                actor=actor,
                action="ROTATE_NODE_TOKEN",
                entity_type="node",
                entity_id=node_id,
                request_id=request_id,
            )
        return token

    def rotate_runtime_token(
        self, runtime_id: int, *, actor: str, request_id: str
    ) -> str:
        token = generate_token()
        with self.db.transaction(immediate=True) as session:
            statement = select(RuntimeInstance).where(RuntimeInstance.id == runtime_id)
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            runtime = session.scalar(statement)
            if runtime is None:
                raise KeyError(runtime_id)
            if not runtime.active or runtime.state not in {
                RuntimeState.NEEDS_LOGIN,
                RuntimeState.READY,
                RuntimeState.STOPPED,
            }:
                raise AccountBusyError(
                    "runtime token rotation requires a stopped, active runtime"
                )
            active_job = session.scalar(
                select(Job.id).where(
                    Job.runtime_id == runtime.id,
                    Job.status.in_([str(item) for item in ACTIVE_JOB_STATUSES]),
                )
            )
            if active_job is not None:
                raise AccountBusyError(
                    f"runtime token rotation requires job {active_job} to finish"
                )
            runtime.token_hash = hash_token(token)
            runtime.runtime_token_enc = self.secrets.encrypt(token)
            # The next SETUP/START resumes at AGENT_CONFIGURED while the
            # instance is running; Incus cannot exec bootstrap commands in a
            # stopped container.
            if runtime.bootstrap_phase in {
                "AGENT_CONFIGURED",
                "AGENT_SERVICE_INSTALLED",
                "DISPLAY_CONFIGURED",
                "STEAM_RUNTIME_PREPARED",
                "DST_RUNTIME_PREPARED",
                "BOOTSTRAP_COMPLETE",
            }:
                runtime.bootstrap_phase = "AGENT_FILES_INSTALLED"
            runtime.bootstrap_completed_at = None
            runtime.bootstrap_error_code = None
            runtime.bootstrap_error_message = None
            runtime.command_version += 1
            add_audit(
                session,
                actor=actor,
                action="ROTATE_RUNTIME_TOKEN",
                entity_type="runtime",
                entity_id=runtime_id,
                request_id=request_id,
            )
        return token

    def events(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        level: str | None = None,
        entity: str | None = None,
    ) -> list[dict]:
        with self.db.session() as session:
            statement = select(Event)
            if level:
                statement = statement.where(Event.level == level.upper())
            if entity == "ACCOUNT":
                statement = statement.where(Event.account_id.is_not(None))
            elif entity == "RUNTIME":
                statement = statement.where(Event.runtime_id.is_not(None))
            elif entity == "NODE":
                statement = statement.where(Event.node_id.is_not(None))
            rows = list(
                session.scalars(
                    statement.order_by(Event.id.desc()).offset(offset).limit(limit)
                )
            )
            return [
                {
                    "id": event.id,
                    "account_id": event.account_id,
                    "runtime_id": event.runtime_id,
                    "node_id": event.node_id,
                    "level": event.level,
                    "kind": event.kind,
                    "message": event.message,
                    "request_id": event.request_id,
                    "created_at": _dt(event.created_at),
                }
                for event in rows
            ]

    def audit_events(self, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.db.session() as session:
            rows = list(
                session.scalars(
                    select(AuditEvent)
                    .order_by(AuditEvent.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            )
            return [
                {
                    "id": record.id,
                    "actor": record.actor,
                    "action": record.action,
                    "entity_type": record.entity_type,
                    "entity_id": record.entity_id,
                    "request_id": record.request_id,
                    "metadata": record.metadata_json,
                    "created_at": _dt(record.created_at),
                }
                for record in rows
            ]
