from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

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
    RuntimeLease,
    RuntimeState,
    utcnow,
)
from app.providers.base import (
    InstanceNotFound,
    ProviderError,
    RuntimeDescriptor,
    RuntimeProvider,
)
from app.services.execution_lock import execution_lock
from app.services.leases import occupied_runtime_ids
from app.services.records import add_event
from app.services.security import ensure_utc


class Reconciler:
    """Observe under the same account lock used by lifecycle execution.

    Expiry alone never releases capacity. Only an authoritative STOPPED/missing
    observation made without concurrent lifecycle work can release a reservation.
    Incus RUNNING alone never clears an agent-health error.
    """

    def __init__(self, db: Database, provider: RuntimeProvider, leadership=None):
        self.db = db
        self.provider = provider
        self.leadership = leadership

    def tick(self) -> int:
        if self.leadership and not self.leadership.try_acquire():
            return 0
        with self.db.session() as session:
            rows = session.execute(
                select(RuntimeInstance, Node)
                .join(Node, Node.id == RuntimeInstance.node_id)
                .where(RuntimeInstance.active.is_(True))
            ).all()
            descriptors = [
                RuntimeDescriptor(
                    id=r.id,
                    account_id=r.account_id,
                    node_id=r.node_id,
                    external_id=r.external_id,
                    provider=r.provider,
                    incus_remote=n.incus_remote,
                    image_version=r.image_version,
                    runtime_generation=r.runtime_generation,
                    network_profile=r.network_profile,
                )
                for r, n in rows
                if r.state
                not in {
                    RuntimeState.NEW,
                    RuntimeState.PROVISIONING,
                    RuntimeState.DESTROYED,
                }
            ]
        changed = 0
        for descriptor in descriptors:
            with execution_lock(self.db, descriptor.account_id) as acquired:
                if acquired:
                    changed += self._observe(descriptor)
        return changed

    def _observe(self, descriptor: RuntimeDescriptor) -> int:
        with self.db.session() as session:
            if session.scalar(
                select(Job.id).where(
                    Job.account_id == descriptor.account_id,
                    Job.status.in_([JobStatus.LEASED, JobStatus.RUNNING]),
                )
            ):
                return 0
        missing = False
        try:
            actual = self.provider.inspect(descriptor).state
        except InstanceNotFound:
            missing, actual = True, RuntimeState.STOPPED
        except ProviderError:
            return 0
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            runtime = session.get(RuntimeInstance, descriptor.id)
            if not runtime or not runtime.active:
                return 0
            node_stmt = select(Node).where(Node.id == runtime.node_id)
            if not self.db.is_sqlite:
                node_stmt = node_stmt.with_for_update()
            node = session.scalar(node_stmt)
            account = session.get(Account, runtime.account_id)
            before = runtime.state
            leases = list(
                session.scalars(
                    select(RuntimeLease).where(
                        RuntimeLease.runtime_id == runtime.id,
                        RuntimeLease.released_at.is_(None),
                    )
                )
            )
            if actual == RuntimeState.RUNNING:
                if not leases:
                    # Reserve observed capacity, including externally started containers.
                    session.add(
                        RuntimeLease(
                            node_id=runtime.node_id,
                            runtime_id=runtime.id,
                            lease_token=uuid.uuid4().hex,
                            expires_at=now + timedelta(seconds=120),
                        )
                    )
                    session.flush()
                if (
                    node
                    and len(occupied_runtime_ids(session, node.id))
                    > node.max_active_slots
                ):
                    if node.status != NodeStatus.ERROR:
                        transition_node(node, NodeStatus.ERROR)
                    add_event(
                        session,
                        level="ERROR",
                        kind=ErrorCode.NO_CAPACITY,
                        message="Provider reports more active runtimes than node capacity",
                        node_id=node.id,
                    )
                # Preserve STALE/ERROR until a fresh authenticated readiness report
                # is explicitly verified; provider liveness is not runtime health.
            elif actual == RuntimeState.STOPPED:
                for lease in leases:
                    lease.released_at = now
                run = session.scalar(
                    select(Run).where(
                        Run.runtime_id == runtime.id, Run.ended_at.is_(None)
                    )
                )
                if run:
                    run.ended_at = now
                    run.duration_seconds = max(
                        0, int((now - ensure_utc(run.started_at)).total_seconds())
                    )
                    run.result = "MISSING" if missing else "OBSERVED_STOPPED"
                if missing:
                    if runtime.state != RuntimeState.ERROR:
                        transition_runtime(runtime, RuntimeState.ERROR)
                    runtime.last_error_code = ErrorCode.RUNTIME_NOT_FOUND
                    runtime.last_error_message = (
                        "Provider confirms runtime missing; explicit rebuild required"
                    )
                    if account and account.enabled:
                        transition_account(account, AccountState.NEEDS_ATTENTION)
                elif runtime.state in {
                    RuntimeState.STARTING,
                    RuntimeState.RUNNING,
                    RuntimeState.STOPPING,
                    RuntimeState.STALE,
                }:
                    if runtime.state in {RuntimeState.RUNNING, RuntimeState.STARTING}:
                        transition_runtime(runtime, RuntimeState.STOPPING)
                    transition_runtime(runtime, RuntimeState.STOPPED)
                    if (
                        account
                        and account.enabled
                        and account.status != AccountState.NEEDS_ATTENTION
                    ):
                        transition_account(
                            account,
                            AccountState.READY
                            if runtime.verified_at
                            else AccountState.NEEDS_LOGIN,
                        )
            if before == runtime.state:
                return 0
            add_event(
                session,
                level="WARNING",
                kind="RUNTIME_RECONCILED",
                message=f"Provider {actual}; runtime {before} -> {runtime.state}",
                account_id=runtime.account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
            )
            return 1
