from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Database
from app.models import Account, Job, JobAttempt, JobKind, JobStatus, utcnow
from app.services.execution_lock import execution_lock
from app.services.security import ensure_utc

ACTIVE_JOB_STATUSES = {
    JobStatus.PENDING,
    JobStatus.LEASED,
    JobStatus.RUNNING,
    JobStatus.RETRY,
}


def serialize_job(job: Job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "account_id": job.account_id,
        "runtime_id": job.runtime_id,
        "node_id": job.node_id,
        "status": job.status,
        "priority": job.priority,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "run_after": job.run_after.isoformat() if job.run_after else None,
        "leased_until": job.leased_until.isoformat() if job.leased_until else None,
        "lease_owner": job.lease_owner,
        "last_error_code": job.last_error_code,
        "last_error": job.last_error,
        "request_id": job.request_id,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


class JobQueue:
    def __init__(
        self,
        db: Database,
        *,
        lease_seconds: int,
        retry_base_seconds: int,
        max_attempts: int,
    ):
        self.db = db
        self.lease_seconds = lease_seconds
        self.retry_base_seconds = retry_base_seconds
        self.max_attempts = max_attempts

    def enqueue_in_session(
        self,
        session: Session,
        *,
        kind: JobKind | str,
        account_id: int | None,
        runtime_id: int | None,
        node_id: int | None,
        request_id: str | None,
        idempotency_key: str,
        payload: dict | None = None,
        priority: int = 100,
        max_attempts: int | None = None,
    ) -> Job:
        kind_value = str(kind)
        existing = session.scalar(
            select(Job).where(Job.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return existing
        if runtime_id is not None:
            existing = session.scalar(
                select(Job)
                .where(
                    Job.runtime_id == runtime_id,
                    Job.kind == kind_value,
                    Job.status.in_([str(item) for item in ACTIVE_JOB_STATUSES]),
                )
                .order_by(Job.id.desc())
            )
            if existing is not None:
                return existing
        job = Job(
            kind=kind_value,
            account_id=account_id,
            runtime_id=runtime_id,
            node_id=node_id,
            status=JobStatus.PENDING,
            priority=priority,
            max_attempts=max_attempts or self.max_attempts,
            run_after=utcnow(),
            idempotency_key=idempotency_key,
            request_id=request_id,
            payload=payload or {},
        )
        try:
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:
            job = session.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            if job is None and runtime_id is not None:
                job = session.scalar(
                    select(Job)
                    .where(
                        Job.runtime_id == runtime_id,
                        Job.kind == kind_value,
                        Job.status.in_([str(item) for item in ACTIVE_JOB_STATUSES]),
                    )
                    .order_by(Job.id.desc())
                )
            if job is None:
                raise
        return job

    def enqueue(
        self,
        *,
        kind: JobKind | str,
        account_id: int | None,
        runtime_id: int | None,
        node_id: int | None,
        request_id: str | None,
        idempotency_key: str | None = None,
        payload: dict | None = None,
        priority: int = 100,
    ) -> Job:
        with self.db.transaction(immediate=True) as session:
            job = self.enqueue_in_session(
                session,
                kind=kind,
                account_id=account_id,
                runtime_id=runtime_id,
                node_id=node_id,
                request_id=request_id,
                idempotency_key=idempotency_key
                or f"{kind}:{runtime_id}:{uuid.uuid4().hex}",
                payload=payload,
                priority=priority,
            )
            session.flush()
            session.expunge(job)
            return job

    def reclaim_expired(self, session: Session) -> int:
        now = utcnow()
        statement = select(Job).where(
            Job.status.in_([JobStatus.LEASED, JobStatus.RUNNING]),
            Job.leased_until.is_not(None),
            Job.leased_until < now,
        )
        if not self.db.is_sqlite:
            statement = statement.with_for_update(skip_locked=True)
        jobs = list(session.scalars(statement))
        recovered = 0
        for job in jobs:
            with execution_lock(self.db, job.account_id or -job.id) as acquired:
                if not acquired:
                    continue
                self._reclaim(session, job, now)
                recovered += 1
        return recovered

    def _reclaim(self, session: Session, job: Job, now) -> None:
        if job.attempt_count >= job.max_attempts:
            job.status = JobStatus.FAILED
            job.completed_at = now
            job.last_error = "executor lease expired after final attempt"
        else:
            job.status = JobStatus.RETRY
            job.run_after = now
        job.lease_owner = None
        job.leased_until = None
        attempt = session.scalar(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id, JobAttempt.completed_at.is_(None))
            .order_by(JobAttempt.id.desc())
        )
        if attempt:
            attempt.status = "ABANDONED"
            attempt.error_message = "executor lease expired"
            attempt.completed_at = now

    def claim(self, worker_id: str) -> Job | None:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            self.reclaim_expired(session)
            now = utcnow()
            statement = (
                select(Job)
                .where(
                    Job.status.in_([JobStatus.PENDING, JobStatus.RETRY]),
                    Job.run_after <= now,
                    or_(Job.leased_until.is_(None), Job.leased_until < now),
                )
                .order_by(Job.priority.asc(), Job.created_at.asc(), Job.id.asc())
                .limit(1)
            )
            if not self.db.is_sqlite:
                statement = statement.with_for_update(skip_locked=True)
            job = session.scalar(statement)
            if job is None:
                return None
            if job.account_id is not None:
                account_stmt = select(Account).where(Account.id == job.account_id)
                if not self.db.is_sqlite:
                    account_stmt = account_stmt.with_for_update(skip_locked=True)
                if session.scalar(account_stmt) is None:
                    return None
                busy = session.scalar(
                    select(Job.id).where(
                        Job.account_id == job.account_id,
                        Job.id != job.id,
                        Job.status.in_([JobStatus.LEASED, JobStatus.RUNNING]),
                    )
                )
                if busy is not None:
                    return None
            job.status = JobStatus.LEASED
            job.lease_owner = f"{worker_id[:60]}:{uuid.uuid4().hex}"
            job.leased_until = now + timedelta(seconds=self.lease_seconds)
            job.attempt_count += 1
            if job.started_at is None:
                job.started_at = now
            session.add(
                JobAttempt(
                    job_id=job.id,
                    attempt_number=job.attempt_count,
                    worker_id=job.lease_owner,
                    status=JobStatus.RUNNING,
                )
            )
            session.flush()
            session.expunge(job)
            return job

    def mark_running(self, job_id: int, worker_id: str) -> bool:
        with self.db.transaction(immediate=True) as session:
            job = self._owned(session, job_id, worker_id)
            if job is None or job.status != JobStatus.LEASED:
                return False
            job.status = JobStatus.RUNNING
            job.leased_until = utcnow() + timedelta(seconds=self.lease_seconds)
            return True

    def renew(self, job_id: int, worker_id: str) -> bool:
        with self.db.transaction(immediate=True) as session:
            job = self._owned(session, job_id, worker_id)
            if job is None:
                return False
            job.leased_until = utcnow() + timedelta(seconds=self.lease_seconds)
            return True

    def succeed(self, job_id: int, worker_id: str) -> None:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            job = self._owned(session, job_id, worker_id)
            if job is None:
                return
            job.status = JobStatus.SUCCEEDED
            job.completed_at = now
            job.leased_until = None
            job.lease_owner = None
            attempt = session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == job_id,
                    JobAttempt.attempt_number == job.attempt_count,
                )
            )
            if attempt:
                attempt.status = JobStatus.SUCCEEDED
                attempt.completed_at = now

    def fail(
        self, job_id: int, worker_id: str, *, code: str, message: str, retryable: bool
    ) -> None:
        now = utcnow()
        with self.db.transaction(immediate=True) as session:
            job = self._owned(session, job_id, worker_id)
            if job is None:
                return
            job.last_error_code = code
            job.last_error = message[:2000]
            attempt = session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == job_id,
                    JobAttempt.attempt_number == job.attempt_count,
                )
            )
            if attempt:
                attempt.error_code = code
                attempt.error_message = message[:2000]
                attempt.completed_at = now
            if retryable and job.attempt_count < job.max_attempts:
                job.status = JobStatus.RETRY
                job.run_after = now + timedelta(
                    seconds=self.retry_base_seconds
                    * (2 ** max(job.attempt_count - 1, 0))
                )
                if attempt:
                    attempt.status = JobStatus.RETRY
            else:
                job.status = JobStatus.FAILED
                job.completed_at = now
                if attempt:
                    attempt.status = JobStatus.FAILED
            job.leased_until = None
            job.lease_owner = None

    def _owned(self, session: Session, job_id: int, owner: str) -> Job | None:
        statement = select(Job).where(Job.id == job_id)
        if not self.db.is_sqlite:
            statement = statement.with_for_update()
        job = session.scalar(statement)
        if (
            job is None
            or job.lease_owner != owner
            or job.status not in {JobStatus.LEASED, JobStatus.RUNNING}
            or not job.leased_until
            or ensure_utc(job.leased_until) <= utcnow()
        ):
            return None
        return job

    def owns(self, job: Job) -> bool:
        with self.db.session() as session:
            return self._owned(session, job.id, job.lease_owner) is not None

    def get(self, job_id: int) -> Job | None:
        with self.db.session() as session:
            job = session.get(Job, job_id)
            if job:
                session.expunge(job)
            return job

    def list(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.db.session() as session:
            jobs = list(
                session.scalars(
                    select(Job).order_by(Job.id.desc()).offset(offset).limit(limit)
                )
            )
            return [serialize_job(job) for job in jobs]
