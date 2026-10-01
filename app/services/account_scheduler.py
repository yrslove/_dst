"""Durable account reward decisions routed through the existing job queue."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.db import Database
from app.models import (
    Account,
    AccountScheduleState,
    AccountState,
    GameplayTask,
    GameplayTaskStatus,
    Job,
    JobKind,
    JobStatus,
    Node,
    NodeStatus,
    RuntimeInstance,
    RuntimeLease,
    utcnow,
)
from app.scheduling import (
    AccountPhase,
    AccountSchedule,
    DailyStatus,
    JobIntent,
    JobType,
    ResourceContext,
    WeeklyState,
    WorkerSlot,
    decide_next_job,
)
from app.services.gameplay import INWORLD_WEEKLY_CLAIM_TASK
from app.services.jobs import ACTIVE_JOB_STATUSES, JobQueue
from app.services.leases import LeaseService, occupied_runtime_ids

DEFAULT_LONG_SESSION_SECONDS = 600


@dataclass(frozen=True, slots=True)
class VerifiedOutcome:
    """Facts explicitly confirmed by the worker result, never inferred from intent."""

    daily_claimed: bool = False
    daily_unavailable: bool = False
    weekly_claim_id: str | None = None
    gift_pending: bool | None = None
    next_daily_due_at: datetime | None = None
    next_weekly_eligible_at: datetime | None = None
    estimated_due_at: datetime | None = None
    estimated_time_to_gift_seconds: float | None = None
    daily_claim_at: datetime | None = None
    weekly_claim_at: datetime | None = None
    replace_timing: bool = False


class AccountScheduler:
    def __init__(
        self,
        db: Database,
        jobs: JobQueue | None = None,
        leases: LeaseService | None = None,
    ):
        self.db = db
        self.jobs = jobs
        self.leases = leases

    def schedule_next(
        self,
        account_id: int,
        now: datetime,
        resources: ResourceContext,
    ) -> JobIntent | None:
        if self.jobs is None:
            raise RuntimeError("AccountScheduler requires the existing JobQueue")
        self._recover_succeeded_schedule(account_id)
        with self.db.transaction(immediate=True) as session:
            statement = select(AccountScheduleState).where(
                AccountScheduleState.account_id == account_id
            )
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None or row.paused:
                return None
            account = session.get(Account, account_id)
            if account is None or not account.enabled or account.status not in {
                AccountState.READY,
                AccountState.QUEUED,
                AccountState.RUNNING,
            }:
                return None

            if row.active_job_key is not None:
                # A process restart observes the same durable job reservation.
                job = session.scalar(
                    select(Job).where(Job.idempotency_key == row.active_job_key)
                )
                if job is None:
                    return None
                active_type = JobType(row.active_job_type)
                if job.status in ACTIVE_JOB_STATUSES and _reservation_still_wanted(
                    row, active_type
                ):
                    return JobIntent(
                        account_id,
                        active_type,
                        row.active_job_key,
                        _priority(active_type),
                        resources.available_slots[0].slot_id
                        if resources.available_slots
                        else None,
                    )
                if job.status in {JobStatus.PENDING, JobStatus.RETRY}:
                    job.status = JobStatus.CANCELLED
                    job.completed_at = now
                    task_id = job.payload.get("gameplay_task_id")
                    task = session.get(GameplayTask, task_id) if task_id else None
                    if task is not None and task.status in {
                        GameplayTaskStatus.PENDING,
                        GameplayTaskStatus.RUNNING,
                    }:
                        task.status = GameplayTaskStatus.CANCELLED
                        task.completed_at = now
                        task.updated_at = now
                    row.active_job_key = None
                    row.active_job_type = None
                    row.schedule_revision += 1
                else:
                    return None

            intent = decide_next_job(_snapshot(row), now, resources)
            if intent is None:
                return None
            runtime_statement = select(RuntimeInstance).where(
                RuntimeInstance.account_id == account_id,
                RuntimeInstance.active.is_(True),
            )
            if not self.db.is_sqlite:
                runtime_statement = runtime_statement.with_for_update()
            runtime = session.scalar(runtime_statement)
            if runtime is None:
                return None
            node = session.get(Node, runtime.node_id)
            if not _slot_available(session, runtime, node):
                return None

            kind = _job_kind(intent.job_type)
            active_job = session.scalar(
                select(Job.id).where(
                    Job.runtime_id == runtime.id,
                    Job.kind == str(kind),
                    Job.status.in_([str(status) for status in ACTIVE_JOB_STATUSES]),
                )
            )
            active_task = session.scalar(
                select(GameplayTask.id).where(
                    GameplayTask.account_id == account_id,
                    GameplayTask.kind == str(kind),
                    GameplayTask.status.in_(
                        [GameplayTaskStatus.PENDING, GameplayTaskStatus.RUNNING]
                    ),
                )
            )
            if active_job is not None or active_task is not None:
                return None
            task = _new_task(account_id, runtime.id, kind, intent.job_type, row, now)
            session.add(task)
            session.flush()
            payload = {
                "account_schedule_intent": intent.job_type.value,
                "account_schedule_revision": row.schedule_revision,
                "gameplay_task_id": task.id,
            }
            if kind == JobKind.LONG_SESSION:
                estimate = row.estimated_time_to_gift_seconds
                duration = (
                    DEFAULT_LONG_SESSION_SECONDS
                    if intent.job_type == JobType.CLAIM_PENDING_GIFT
                    or estimate is None
                    else int(estimate)
                )
                payload["requested_duration"] = min(max(duration, 600), 5400)
                task.result_json = {
                    "requested_duration": payload["requested_duration"],
                    "state": "PENDING",
                    "checkpoints": 0,
                    "recoveries": 0,
                    "gift_transitions": [],
                    "confirmations": [],
                }

            job = self.jobs.enqueue_in_session(
                session,
                kind=kind,
                account_id=account_id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                request_id=None,
                idempotency_key=intent.idempotency_key,
                payload=payload,
                priority=intent.priority,
            )
            if job.idempotency_key != intent.idempotency_key:
                # Another active job owns this runtime/kind; leave state untouched.
                session.delete(task)
                return None
            row.active_job_key = intent.idempotency_key
            row.active_job_type = intent.job_type.value
            session.flush()
            return intent

    def tick(self, *, now: datetime | None = None) -> int:
        """Queue due account intents using existing runtime capacity and JobQueue."""
        current = now or utcnow()
        with self.db.session() as session:
            account_ids = list(session.scalars(select(AccountScheduleState.account_id)))
        scheduled = 0
        for account_id in account_ids:
            intent = self.schedule_next(
                account_id,
                current,
                ResourceContext((WorkerSlot(f"account:{account_id}"),)),
            )
            if intent is not None:
                scheduled += 1
        return scheduled

    def pause(self, account_id: int) -> dict:
        """Pause future automatic work; drain existing executors before manual input."""
        with self.db.transaction(immediate=True) as session:
            statement = select(AccountScheduleState).where(AccountScheduleState.account_id == account_id)
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                raise ValueError("account schedule not found")
            row.paused = True
            jobs = list(session.scalars(select(Job).where(
                Job.account_id == account_id,
                Job.status.in_(list(ACTIVE_JOB_STATUSES)),
            )))
            for job in jobs:
                if job.status in {JobStatus.PENDING, JobStatus.RETRY} and job.idempotency_key == row.active_job_key:
                    job.status = JobStatus.CANCELLED
                    job.completed_at = utcnow()
                    task_id = job.payload.get("gameplay_task_id")
                    task = session.get(GameplayTask, task_id) if task_id else None
                    if task is not None:
                        task.status = GameplayTaskStatus.CANCELLED
                        task.completed_at = task.updated_at = job.completed_at
                    row.active_job_key = row.active_job_type = None
            return {"account_id": account_id, "paused": True,
                    "active_jobs": [job.id for job in jobs if job.status in ACTIVE_JOB_STATUSES]}

    def complete(
        self,
        account_id: int,
        idempotency_key: str,
        outcome: VerifiedOutcome,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Atomically apply verified outcome, complete the Job, and release intent."""
        runtime_id = None
        with self.db.transaction(immediate=True) as session:
            row = self._locked_row(session, account_id)
            if row is None or row.active_job_key != idempotency_key:
                return False
            current = now or utcnow()
            job = session.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            if job is not None:
                runtime_id = job.runtime_id
                if job.status in ACTIVE_JOB_STATUSES:
                    job.status = JobStatus.SUCCEEDED
                    job.completed_at = current
                task_id = job.payload.get("gameplay_task_id")
                task = session.get(GameplayTask, task_id) if task_id else None
                if task is not None and task.status in {
                    GameplayTaskStatus.PENDING,
                    GameplayTaskStatus.RUNNING,
                }:
                    task.completed_at = current
                    task.updated_at = current
                    if job.kind == JobKind.LONG_SESSION:
                        task.status = GameplayTaskStatus.COMPLETED
                        task.result_json = {
                            **(task.result_json or {}),
                            "state": "COMPLETED",
                            "schedule_intent": job.payload.get(
                                "account_schedule_intent"
                            ),
                        }
                    elif outcome.daily_claimed:
                        digest = hashlib.sha256(
                            idempotency_key.encode("utf-8")
                        ).hexdigest()
                        task.status = GameplayTaskStatus.SUCCEEDED
                        task.confirmation_key = digest
                        task.claim_confirmed_at = outcome.daily_claim_at or current
                        task.claim_persisted_at = current
                        task.result_json = {
                            "semantic": "DAILY_GIFT_CONFIRMED",
                            "schedule_job": idempotency_key,
                            "observed_at": task.claim_confirmed_at.isoformat(),
                        }
                    elif outcome.daily_unavailable:
                        task.status = GameplayTaskStatus.NO_REWARD_AVAILABLE
                        task.result_json = {"semantic": "NO_REWARD_AVAILABLE"}
                    else:
                        task.status = GameplayTaskStatus.NEEDS_ATTENTION
                        task.error_code = "SCHEDULE_RESULT_UNVERIFIED"
                        task.error_message = "Daily job completed without a verified outcome"
            if outcome.weekly_claim_id:
                self._persist_weekly_claim(session, row, outcome, current, runtime_id)
            if outcome.daily_claimed:
                row.daily_status = DailyStatus.DONE.value
                row.last_daily_claim_at = outcome.daily_claim_at or current
            elif outcome.daily_unavailable:
                row.daily_status = DailyStatus.UNKNOWN.value
                row.next_daily_due_at = None
            if outcome.gift_pending is not None:
                row.pending_gift = outcome.gift_pending
            if outcome.replace_timing:
                row.next_daily_due_at = outcome.next_daily_due_at
                row.next_weekly_eligible_at = outcome.next_weekly_eligible_at
                row.estimated_due_at = outcome.estimated_due_at
                row.estimated_time_to_gift_seconds = (
                    outcome.estimated_time_to_gift_seconds
                )
            if outcome.weekly_claim_at is not None:
                row.last_weekly_claim_at = outcome.weekly_claim_at
            if (
                row.active_job_type
                in {JobType.WEEKLY_FARM.value, JobType.FINAL_COLLECTION.value}
                and not outcome.replace_timing
            ):
                row.next_weekly_eligible_at = None
                row.estimated_due_at = None
            if (
                row.weekly_state == WeeklyState.SYNCED.value
                and row.weekly_collected >= row.weekly_target
            ):
                row.phase = AccountPhase.DONE.value
            elif (
                row.weekly_state == WeeklyState.SYNCED.value
                and row.weekly_target - row.weekly_collected == 1
            ):
                row.phase = AccountPhase.FINAL_COLLECTION.value
            else:
                row.phase = AccountPhase.FARMING.value
            row.active_job_key = None
            row.active_job_type = None
            row.schedule_revision += 1
        if runtime_id is not None and self.leases is not None:
            self.leases.release_slot(runtime_id)
        return True

    def sync_weekly_boundary(self, account_id: int, *, target: int | None = None) -> bool:
        """Initialize a new cycle only when its boundary is explicitly confirmed."""
        with self.db.transaction(immediate=True) as session:
            row = self._locked_row(session, account_id)
            if row is None:
                return False
            row.weekly_state = WeeklyState.SYNCED.value
            row.weekly_collected = 0
            row.confirmed_claims_current_observation = 0
            if target is not None:
                if target < 0:
                    raise ValueError("weekly target must be non-negative")
                row.weekly_target = target
            row.phase = AccountPhase.FARMING.value
            row.schedule_revision += 1
            return True

    def reset_daily_boundary(self, account_id: int) -> bool:
        """Mark daily work pending after an explicitly confirmed daily boundary."""
        with self.db.transaction(immediate=True) as session:
            row = self._locked_row(session, account_id)
            if row is None:
                return False
            row.daily_status = DailyStatus.PENDING.value
            row.schedule_revision += 1
            return True

    def retryable_failure(self, account_id: int, idempotency_key: str) -> bool:
        with self.db.transaction(immediate=True) as session:
            row = self._locked_row(session, account_id)
            if row is None or row.active_job_key != idempotency_key:
                return False
            row.active_job_key = None
            row.active_job_type = None
            return True

    def _persist_weekly_claim(self, session, row, outcome, current, runtime_id):
        claim_id = outcome.weekly_claim_id
        if not isinstance(claim_id, str) or not claim_id or len(claim_id) > 512:
            raise ValueError("verified weekly claim requires a bounded identity")
        digest = hashlib.sha256(claim_id.encode("utf-8")).hexdigest()
        existing = session.scalar(
            select(GameplayTask.id).where(
                GameplayTask.account_id == row.account_id,
                GameplayTask.kind == INWORLD_WEEKLY_CLAIM_TASK,
                GameplayTask.confirmation_key == digest,
            )
        )
        if existing is not None:
            return
        if runtime_id is None:
            raise ValueError("weekly claim receipt requires its runtime")
        confirmed_at = outcome.weekly_claim_at or current
        receipt = GameplayTask(
            account_id=row.account_id,
            runtime_id=runtime_id,
            kind=INWORLD_WEEKLY_CLAIM_TASK,
            status=GameplayTaskStatus.SUCCEEDED,
            started_at=confirmed_at,
            updated_at=current,
            completed_at=current,
            confirmation_key=digest,
            claim_confirmed_at=confirmed_at,
            claim_persisted_at=current,
            result_json={
                "semantic": "IN_WORLD_GIFT_CONFIRMED",
                "claim_id": claim_id,
                "observed_at": confirmed_at.isoformat(),
                "weekly_state_at_confirmation": row.weekly_state,
            },
        )
        session.add(receipt)
        if row.weekly_state == WeeklyState.SYNCED.value:
            row.weekly_collected += 1
        else:
            row.confirmed_claims_current_observation += 1
        row.pending_gift = False

    def _locked_row(self, session, account_id: int) -> AccountScheduleState | None:
        statement = select(AccountScheduleState).where(
            AccountScheduleState.account_id == account_id
        )
        if not self.db.is_sqlite:
            statement = statement.with_for_update()
        return session.scalar(statement)

    def _recover_succeeded_schedule(self, account_id: int) -> None:
        with self.db.session() as session:
            state = session.get(AccountScheduleState, account_id)
            key = state.active_job_key if state is not None else None
            if key is None:
                return
            job = session.scalar(select(Job).where(Job.idempotency_key == key))
            if job is None or job.status != JobStatus.SUCCEEDED:
                return
            task_id = job.payload.get("gameplay_task_id")
            task = session.get(GameplayTask, task_id) if task_id else None
            daily_confirmed = (
                job.kind == JobKind.DAILY_GIFT_CLAIM
                and task is not None
                and task.status == GameplayTaskStatus.SUCCEEDED
                and isinstance(task.result_json, dict)
                and task.result_json.get("semantic") == "DAILY_GIFT_CONFIRMED"
            )
            daily_unavailable = (
                job.kind == JobKind.DAILY_GIFT_CLAIM
                and task is not None
                and task.status == GameplayTaskStatus.NO_REWARD_AVAILABLE
            )
            claim_at = task.claim_confirmed_at if daily_confirmed else None
            completed_at = job.completed_at
        self.complete(
            account_id,
            key,
            VerifiedOutcome(
                daily_claimed=daily_confirmed,
                daily_unavailable=daily_unavailable,
                daily_claim_at=claim_at,
            ),
            now=completed_at,
        )


def _snapshot(row: AccountScheduleState) -> AccountSchedule:
    return AccountSchedule(
        account_id=row.account_id,
        daily_status=DailyStatus(row.daily_status),
        weekly_state=WeeklyState(row.weekly_state),
        weekly_collected=row.weekly_collected,
        weekly_target=row.weekly_target,
        phase=AccountPhase(row.phase),
        pending_gift=row.pending_gift,
        next_daily_due_at=row.next_daily_due_at,
        next_weekly_eligible_at=row.next_weekly_eligible_at,
        estimated_due_at=row.estimated_due_at,
        last_daily_claim_at=row.last_daily_claim_at,
        last_weekly_claim_at=row.last_weekly_claim_at,
        estimated_time_to_gift_seconds=row.estimated_time_to_gift_seconds,
        schedule_revision=row.schedule_revision,
        active_job_key=row.active_job_key,
        active_job_type=JobType(row.active_job_type) if row.active_job_type else None,
    )


def _job_kind(job_type: JobType) -> JobKind:
    return (
        JobKind.DAILY_GIFT_CLAIM
        if job_type == JobType.DAILY_MAINTENANCE
        else JobKind.LONG_SESSION
    )


def _priority(job_type: JobType) -> int:
    return {
        JobType.CLAIM_PENDING_GIFT: 10,
        JobType.FINAL_COLLECTION: 20,
        JobType.DAILY_MAINTENANCE: 30,
        JobType.WEEKLY_FARM: 40,
    }[job_type]


def _reservation_still_wanted(row, job_type: JobType) -> bool:
    if row.pending_gift:
        return job_type == JobType.CLAIM_PENDING_GIFT
    if job_type == JobType.CLAIM_PENDING_GIFT:
        return False
    if job_type == JobType.DAILY_MAINTENANCE:
        return row.daily_status == DailyStatus.PENDING.value
    if job_type in {JobType.WEEKLY_FARM, JobType.FINAL_COLLECTION}:
        return (
            row.weekly_state == WeeklyState.SYNCED.value
            and row.weekly_collected < row.weekly_target
        )
    return True


def _new_task(account_id, runtime_id, kind, job_type, row, now):
    return GameplayTask(
        account_id=account_id,
        runtime_id=runtime_id,
        kind=str(kind),
        status=GameplayTaskStatus.PENDING,
        started_at=now,
        updated_at=now,
        result_json=(
            {"state": "PENDING", "schedule_intent": job_type.value}
            if kind == JobKind.LONG_SESSION
            else None
        ),
    )


def _slot_available(session, runtime, node) -> bool:
    if (
        node is None
        or not node.enabled
        or node.draining
        or node.maintenance
        or node.status != NodeStatus.ONLINE
    ):
        return False
    active_lease = session.scalar(
        select(RuntimeLease).where(
            RuntimeLease.runtime_id == runtime.id,
            RuntimeLease.released_at.is_(None),
        )
    )
    if active_lease is not None:
        owner = session.get(Job, active_lease.job_id) if active_lease.job_id else None
        return owner is None or owner.status not in ACTIVE_JOB_STATUSES
    occupied = occupied_runtime_ids(session, node.id)
    # A running account reuses its existing runtime slot; a stopped runtime needs
    # one free slot before its durable Job can be created.
    return runtime.id in occupied or len(occupied) < node.max_active_slots
