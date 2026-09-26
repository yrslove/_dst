from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.domain.state import transition_account, transition_node, transition_runtime
from app.models import (
    Account,
    AccountState,
    ErrorCode,
    Job,
    JobStatus,
    Node,
    NodeStatus,
    Run,
    RuntimeInstance,
    RuntimeState,
    WorkerStatus,
    utcnow,
)
from app.services.execution_lock import execution_lock
from app.services.leases import LeaseService
from app.services.records import add_event
from app.services.security import ensure_utc


class Watchdog:
    def __init__(self, db: Database, leases: LeaseService, settings: Settings):
        self.db, self.leases, self.settings = db, leases, settings

    def tick(self) -> dict[str, int]:
        now = utcnow()
        node_cutoff = now - timedelta(seconds=self.settings.node_stale_seconds)
        runtime_cutoff = now - timedelta(seconds=self.settings.watchdog_stale_seconds)
        result = {
            "nodes_offline": 0,
            "runtimes_stale": 0,
            "process_failures": 0,
            "leases_recovered": 0,
        }
        with self.db.transaction(immediate=True) as session:
            statement = select(Node).where(
                Node.status.in_([NodeStatus.ONLINE, NodeStatus.DRAINING])
            )
            if not self.db.is_sqlite:
                statement = statement.with_for_update(skip_locked=True)
            for node in session.scalars(statement):
                if node.provider == "mock" and self.settings.environment in {
                    "development",
                    "test",
                }:
                    continue
                if (
                    not node.last_heartbeat_at
                    or ensure_utc(node.last_heartbeat_at) < node_cutoff
                ):
                    transition_node(node, NodeStatus.OFFLINE)
                    result["nodes_offline"] += 1
                    add_event(
                        session,
                        level="ERROR",
                        kind=ErrorCode.NODE_OFFLINE,
                        message="Node heartbeat is stale",
                        node_id=node.id,
                    )
        with self.db.session() as session:
            candidates = list(
                session.execute(
                    select(RuntimeInstance.id, RuntimeInstance.account_id).where(
                        RuntimeInstance.active.is_(True),
                        RuntimeInstance.state.in_(
                            [RuntimeState.STARTING, RuntimeState.RUNNING]
                        ),
                    )
                )
            )
        for runtime_id, account_id in candidates:
            with execution_lock(self.db, account_id) as acquired:
                if not acquired:
                    continue
                with self.db.transaction(immediate=True) as session:
                    account_stmt = select(Account).where(Account.id == account_id)
                    if not self.db.is_sqlite:
                        account_stmt = account_stmt.with_for_update()
                    account = session.scalar(account_stmt)
                    if session.scalar(
                        select(Job.id).where(
                            Job.account_id == account_id,
                            Job.status.in_([JobStatus.LEASED, JobStatus.RUNNING]),
                        )
                    ):
                        continue
                    statement = select(RuntimeInstance).where(
                        RuntimeInstance.id == runtime_id
                    )
                    if not self.db.is_sqlite:
                        statement = statement.with_for_update()
                    runtime = session.scalar(statement)
                    if not runtime or runtime.state not in {
                        RuntimeState.STARTING,
                        RuntimeState.RUNNING,
                    }:
                        continue
                    node = session.get(Node, runtime.node_id)
                    worker = session.get(WorkerStatus, runtime_id)
                    error = None
                    if node and node.status == NodeStatus.OFFLINE:
                        error = ErrorCode.NODE_OFFLINE
                    elif self._runtime_heartbeat_baseline(session, runtime) < runtime_cutoff:
                        error = ErrorCode.AGENT_STALE
                    elif worker and worker.phase in {
                        "STEAM_READY",
                        "DST_STARTING",
                        "GAME_READY",
                        "WORKER_IDLE",
                    }:
                        if not worker.steam_running:
                            error = ErrorCode.STEAM_NOT_RUNNING
                        elif (
                            worker.phase in {"GAME_READY", "WORKER_IDLE"}
                            and not worker.dst_running
                        ):
                            error = ErrorCode.DST_NOT_RUNNING
                    if error:
                        transition_runtime(runtime, RuntimeState.STALE)
                        runtime.last_error_code = error
                        runtime.last_error_message = f"Detected {error}; capacity retained pending reconciliation"
                        if account and account.enabled:
                            transition_account(account, AccountState.NEEDS_ATTENTION)
                        result["runtimes_stale"] += 1
                        if error in {
                            ErrorCode.STEAM_NOT_RUNNING,
                            ErrorCode.DST_NOT_RUNNING,
                        }:
                            result["process_failures"] += 1
                        add_event(
                            session,
                            level="ERROR",
                            kind=error,
                            message=runtime.last_error_message,
                            account_id=account_id,
                            runtime_id=runtime_id,
                            node_id=runtime.node_id,
                        )
        result["leases_recovered"] = self.leases.recover_expired()
        return result

    @staticmethod
    def _runtime_heartbeat_baseline(session, runtime: RuntimeInstance):
        baseline = ensure_utc(runtime.last_heartbeat_at or runtime.updated_at)
        started_at = session.scalar(
            select(Run.started_at)
            .where(Run.runtime_id == runtime.id, Run.ended_at.is_(None))
            .order_by(Run.started_at.desc())
            .limit(1)
        )
        return max(baseline, ensure_utc(started_at)) if started_at else baseline
