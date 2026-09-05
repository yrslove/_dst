from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.db import Database
from app.domain.errors import NoCapacity, NodeOffline
from app.domain.state import transition_runtime
from app.models import (
    Job,
    JobStatus,
    Node,
    NodeStatus,
    RuntimeInstance,
    RuntimeLease,
    RuntimeState,
    utcnow,
)
from app.services.security import ensure_utc

ACTIVE_RUNTIME_STATES = {
    RuntimeState.STARTING,
    RuntimeState.RUNNING,
    RuntimeState.STOPPING,
}


def occupied_runtime_ids(session, node_id: int) -> set[int]:
    occupied = set(
        session.scalars(
            select(RuntimeInstance.id).where(
                RuntimeInstance.node_id == node_id,
                RuntimeInstance.state.in_(
                    [str(state) for state in ACTIVE_RUNTIME_STATES]
                ),
            )
        )
    )
    # Expiry is a request to reconcile, never evidence that a container stopped.
    occupied.update(
        session.scalars(
            select(RuntimeLease.runtime_id).where(
                RuntimeLease.node_id == node_id,
                RuntimeLease.released_at.is_(None),
            )
        )
    )
    return occupied


class LeaseService:
    def __init__(
        self, db: Database, *, lease_seconds: int, node_stale_seconds: int = 45
    ):
        self.db = db
        self.lease_seconds = lease_seconds
        self.node_stale_seconds = node_stale_seconds

    def claim_slot(self, runtime_id: int, job_id: int) -> RuntimeLease:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            runtime_stmt = select(RuntimeInstance).where(
                RuntimeInstance.id == runtime_id
            )
            if not self.db.is_sqlite:
                runtime_stmt = runtime_stmt.with_for_update()
            runtime = session.scalar(runtime_stmt)
            if runtime is None or not runtime.active:
                raise KeyError(runtime_id)
            node_stmt = select(Node).where(Node.id == runtime.node_id)
            if not self.db.is_sqlite:
                node_stmt = node_stmt.with_for_update()
            node = session.scalar(node_stmt)
            if node is None:
                raise NodeOffline("runtime node does not exist")
            if (
                not node.enabled
                or node.maintenance
                or node.draining
                or node.status != NodeStatus.ONLINE
            ):
                raise NodeOffline(f"node {node.name} does not accept starts")
            if node.provider != "mock" and (
                not node.last_heartbeat_at
                or ensure_utc(node.last_heartbeat_at)
                < now - timedelta(seconds=self.node_stale_seconds)
            ):
                raise NodeOffline("node heartbeat expired")

            existing = session.scalar(
                select(RuntimeLease).where(
                    RuntimeLease.runtime_id == runtime_id,
                    RuntimeLease.released_at.is_(None),
                )
            )
            if existing:
                existing.expires_at = now + timedelta(seconds=self.lease_seconds)
                existing.job_id = job_id
                if runtime.state not in {RuntimeState.STARTING, RuntimeState.RUNNING}:
                    transition_runtime(runtime, RuntimeState.STARTING)
                session.flush()
                session.expunge(existing)
                return existing
            occupied = occupied_runtime_ids(session, node.id)
            if runtime.id not in occupied and len(occupied) >= node.max_active_slots:
                raise NoCapacity(
                    f"no capacity on {node.name} ({len(occupied)}/{node.max_active_slots})"
                )
            if runtime.state != RuntimeState.STARTING:
                transition_runtime(runtime, RuntimeState.STARTING)
            lease = RuntimeLease(
                node_id=node.id,
                runtime_id=runtime.id,
                job_id=job_id,
                lease_token=uuid.uuid4().hex,
                leased_at=now,
                expires_at=now + timedelta(seconds=self.lease_seconds),
            )
            session.add(lease)
            session.flush()
            session.expunge(lease)
            return lease

    def renew_slot(self, runtime_id: int) -> bool:
        with self.db.transaction(immediate=True) as session:
            lease = session.scalar(
                select(RuntimeLease).where(
                    RuntimeLease.runtime_id == runtime_id,
                    RuntimeLease.released_at.is_(None),
                )
            )
            if lease is None:
                return False
            lease.expires_at = utcnow() + timedelta(seconds=self.lease_seconds)
            return True

    def release_slot(self, runtime_id: int) -> int:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            leases = list(
                session.scalars(
                    select(RuntimeLease).where(
                        RuntimeLease.runtime_id == runtime_id,
                        RuntimeLease.released_at.is_(None),
                    )
                )
            )
            for lease in leases:
                lease.released_at = now
            return len(leases)

    def recover_expired(self) -> int:
        now = utcnow()
        recovered = 0
        with self.db.transaction(immediate=True) as session:
            leases = list(
                session.scalars(
                    select(RuntimeLease).where(
                        RuntimeLease.released_at.is_(None),
                        RuntimeLease.expires_at <= now,
                    )
                )
            )
            for lease in leases:
                runtime = session.get(RuntimeInstance, lease.runtime_id)
                job = session.get(Job, lease.job_id) if lease.job_id else None
                job_alive = bool(
                    job
                    and job.status in {JobStatus.LEASED, JobStatus.RUNNING}
                    and job.leased_until
                    and ensure_utc(job.leased_until) > now
                )
                if job_alive or (
                    runtime
                    and runtime.state
                    not in {RuntimeState.STOPPED, RuntimeState.DESTROYED}
                ):
                    continue
                lease.released_at = now
                recovered += 1
            return recovered

    def active_count(self, node_id: int) -> int:
        with self.db.session() as session:
            return len(occupied_runtime_ids(session, node_id))
