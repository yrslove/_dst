from __future__ import annotations

from sqlalchemy import select

from app.db import Database
from app.models import (
    Account,
    AccountState,
    DesiredState,
    Job,
    JobKind,
    JobStatus,
    Node,
    NodeStatus,
    RuntimeInstance,
    RuntimeState,
)
from app.services.jobs import JobQueue


class Scheduler:
    """Determines which desired-state commands need durable jobs."""

    def __init__(self, db: Database, jobs: JobQueue, leadership=None):
        self.db = db
        self.jobs = jobs
        self.leadership = leadership

    def tick(self) -> int:
        if self.leadership and not self.leadership.try_acquire():
            return 0
        queued = 0
        with self.db.transaction(immediate=True) as session:
            rows = session.execute(
                select(RuntimeInstance, Account, Node)
                .join(Account, Account.id == RuntimeInstance.account_id)
                .join(Node, Node.id == RuntimeInstance.node_id)
                .where(RuntimeInstance.active.is_(True))
            ).all()
            for runtime, account, node in rows:
                if (
                    runtime.desired_state != DesiredState.RUNNING
                    or runtime.verified_at is None
                    or runtime.state not in {RuntimeState.READY, RuntimeState.STOPPED}
                    or account.status not in {AccountState.READY, AccountState.QUEUED}
                    or not account.enabled
                    or not node.enabled
                    or node.draining
                    or node.maintenance
                    or node.status != NodeStatus.ONLINE
                ):
                    continue
                active = session.scalar(
                    select(Job.id).where(
                        Job.runtime_id == runtime.id,
                        Job.kind == JobKind.START_RUNTIME,
                        Job.status.in_(
                            [
                                JobStatus.PENDING,
                                JobStatus.LEASED,
                                JobStatus.RUNNING,
                                JobStatus.RETRY,
                            ]
                        ),
                    )
                )
                if active is not None:
                    continue
                last = session.scalar(
                    select(Job)
                    .where(
                        Job.runtime_id == runtime.id,
                        Job.kind == JobKind.START_RUNTIME,
                    )
                    .order_by(Job.id.desc())
                    .limit(1)
                )
                if last and last.status == JobStatus.FAILED:
                    continue
                key = f"scheduler:{runtime.id}:v{runtime.command_version}"
                if session.scalar(select(Job.id).where(Job.idempotency_key == key)):
                    continue
                self.jobs.enqueue_in_session(
                    session,
                    kind=JobKind.START_RUNTIME,
                    account_id=account.id,
                    runtime_id=runtime.id,
                    node_id=node.id,
                    request_id=None,
                    idempotency_key=key,
                    payload={"reason": "desired_state_recovery"},
                    priority=80,
                )
                queued += 1
        return queued
