"""Durable reservation and verified-result updates for pure account intents."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.db import Database
from app.models import AccountScheduleState, utcnow
from app.scheduling import (
    AccountPhase,
    AccountSchedule,
    DailyStatus,
    JobIntent,
    JobType,
    ResourceContext,
    decide_next_job,
)


@dataclass(frozen=True, slots=True)
class VerifiedOutcome:
    """Facts explicitly confirmed by the worker result, never inferred from intent."""

    daily_claimed: bool = False
    weekly_collected: int | None = None
    gift_pending: bool | None = None
    next_daily_due_at: datetime | None = None
    next_weekly_eligible_at: datetime | None = None
    estimated_due_at: datetime | None = None
    estimated_time_to_gift_seconds: float | None = None
    daily_claim_at: datetime | None = None
    weekly_claim_at: datetime | None = None
    replace_timing: bool = False


class AccountScheduler:
    def __init__(self, db: Database):
        self.db = db

    def schedule_next(
        self,
        account_id: int,
        now: datetime,
        resources: ResourceContext,
    ) -> JobIntent | None:
        with self.db.transaction(immediate=True) as session:
            statement = select(AccountScheduleState).where(
                AccountScheduleState.account_id == account_id
            )
            if not self.db.is_sqlite:
                statement = statement.with_for_update()
            row = session.scalar(statement)
            if row is None:
                return None
            intent = decide_next_job(_snapshot(row), now, resources)
            if intent is None:
                return None
            if row.active_job_key is None:
                row.active_job_key = intent.idempotency_key
                row.active_job_type = intent.job_type.value
                session.flush()
            else:
                # Reconstruct the durable reservation after a process restart.
                intent = JobIntent(
                    account_id,
                    JobType(row.active_job_type),
                    row.active_job_key,
                    intent.priority,
                    intent.worker_slot_id,
                )
            return intent

    def complete(
        self,
        account_id: int,
        idempotency_key: str,
        outcome: VerifiedOutcome,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Apply only reported facts and release the active reservation atomically."""
        with self.db.transaction(immediate=True) as session:
            row = self._locked_row(session, account_id)
            if row is None or row.active_job_key != idempotency_key:
                return False
            current = now or utcnow()
            if outcome.weekly_collected is not None:
                if (
                    not row.weekly_collected
                    <= outcome.weekly_collected
                    <= row.weekly_target
                ):
                    raise ValueError(
                        "verified weekly count must be monotonic and within target"
                    )
                row.weekly_collected = outcome.weekly_collected
            if outcome.daily_claimed:
                row.daily_status = DailyStatus.DONE.value
                row.last_daily_claim_at = outcome.daily_claim_at or current
            if outcome.weekly_claim_at is not None:
                row.last_weekly_claim_at = outcome.weekly_claim_at
            if outcome.gift_pending is not None:
                row.pending_gift = outcome.gift_pending
            if outcome.replace_timing:
                row.next_daily_due_at = outcome.next_daily_due_at
                row.next_weekly_eligible_at = outcome.next_weekly_eligible_at
                row.estimated_due_at = outcome.estimated_due_at
                row.estimated_time_to_gift_seconds = (
                    outcome.estimated_time_to_gift_seconds
                )
            if (
                row.daily_status == DailyStatus.DONE.value
                and row.weekly_collected >= row.weekly_target
                and not row.pending_gift
            ):
                row.phase = AccountPhase.DONE.value
            elif row.weekly_target - row.weekly_collected == 1:
                row.phase = AccountPhase.FINAL_COLLECTION.value
            row.active_job_key = None
            row.active_job_type = None
            row.schedule_revision += 1
            return True

    def retryable_failure(self, account_id: int, idempotency_key: str) -> bool:
        """Release a failed attempt while retaining the revision/key for safe retry."""
        with self.db.transaction(immediate=True) as session:
            row = self._locked_row(session, account_id)
            if row is None or row.active_job_key != idempotency_key:
                return False
            row.active_job_key = None
            row.active_job_type = None
            return True

    def _locked_row(self, session, account_id: int) -> AccountScheduleState | None:
        statement = select(AccountScheduleState).where(
            AccountScheduleState.account_id == account_id
        )
        if not self.db.is_sqlite:
            statement = statement.with_for_update()
        return session.scalar(statement)


def _snapshot(row: AccountScheduleState) -> AccountSchedule:
    return AccountSchedule(
        account_id=row.account_id,
        daily_status=DailyStatus(row.daily_status),
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
