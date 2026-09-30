"""Pure decision logic for account work; execution stays behind worker adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum


class DailyStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    PENDING = "PENDING"
    DONE = "DONE"


class WeeklyState(StrEnum):
    UNSYNCED_CURRENT_CYCLE = "UNSYNCED_CURRENT_CYCLE"
    SYNCED = "SYNCED"


class AccountPhase(StrEnum):
    FARMING = "FARMING"
    FINAL_COLLECTION = "FINAL_COLLECTION"
    DONE = "DONE"


class JobType(StrEnum):
    DAILY_MAINTENANCE = "DAILY_MAINTENANCE"
    WEEKLY_FARM = "WEEKLY_FARM"
    FINAL_COLLECTION = "FINAL_COLLECTION"
    CLAIM_PENDING_GIFT = "CLAIM_PENDING_GIFT"


@dataclass(frozen=True, slots=True)
class AccountSchedule:
    account_id: int
    daily_status: DailyStatus
    weekly_state: WeeklyState
    weekly_collected: int | None
    weekly_target: int
    phase: AccountPhase = AccountPhase.FARMING
    pending_gift: bool = False
    next_daily_due_at: datetime | None = None
    next_weekly_eligible_at: datetime | None = None
    estimated_due_at: datetime | None = None
    last_daily_claim_at: datetime | None = None
    last_weekly_claim_at: datetime | None = None
    estimated_time_to_gift_seconds: float | None = None
    schedule_revision: int = 0
    active_job_key: str | None = None
    active_job_type: JobType | None = None

    def __post_init__(self) -> None:
        if (
            (self.weekly_collected is not None and self.weekly_collected < 0)
            or self.weekly_target < 0
        ):
            raise ValueError("weekly counts must be non-negative")
        if (self.weekly_state == WeeklyState.SYNCED) != (
            self.weekly_collected is not None
        ):
            raise ValueError("synced weekly state requires an exact count")
        if (
            self.estimated_time_to_gift_seconds is not None
            and self.estimated_time_to_gift_seconds < 0
        ):
            raise ValueError("estimated time to gift must be non-negative")


@dataclass(frozen=True, slots=True)
class WorkerSlot:
    """A capacity token supplied by a future worker/VPS adapter."""

    slot_id: str


@dataclass(frozen=True, slots=True)
class ResourceContext:
    available_slots: tuple[WorkerSlot, ...] = ()


@dataclass(frozen=True, slots=True)
class JobIntent:
    account_id: int
    job_type: JobType
    idempotency_key: str
    priority: int
    worker_slot_id: str | None = None


_PRIORITY = {
    JobType.CLAIM_PENDING_GIFT: 10,
    JobType.FINAL_COLLECTION: 20,
    JobType.DAILY_MAINTENANCE: 30,
    JobType.WEEKLY_FARM: 40,
}


def _due(value: datetime | None, now: datetime) -> bool:
    if value is None:
        return True
    due_at = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    current = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now
    return due_at <= current


def decide_next_job(
    account_state: AccountSchedule,
    now: datetime,
    resource_context: ResourceContext,
) -> JobIntent | None:
    """Return the highest-priority due intent, independent of storage/execution."""
    if account_state.active_job_key:
        if (
            account_state.weekly_state == WeeklyState.UNSYNCED_CURRENT_CYCLE
            and account_state.active_job_type
            in {JobType.WEEKLY_FARM, JobType.FINAL_COLLECTION}
        ):
            return None
        return JobIntent(
            account_id=account_state.account_id,
            job_type=account_state.active_job_type or JobType.WEEKLY_FARM,
            idempotency_key=account_state.active_job_key,
            priority=_PRIORITY[account_state.active_job_type or JobType.WEEKLY_FARM],
        )

    selected: JobType | None = None
    if account_state.pending_gift:
        selected = JobType.CLAIM_PENDING_GIFT
    elif account_state.daily_status == DailyStatus.PENDING and _due(
        account_state.next_daily_due_at, now
    ):
        selected = JobType.DAILY_MAINTENANCE
    elif account_state.weekly_state == WeeklyState.SYNCED:
        weekly_complete = account_state.weekly_collected >= account_state.weekly_target
        weekly_due_at = (
            account_state.next_weekly_eligible_at or account_state.estimated_due_at
        )
        if not weekly_complete and (
            weekly_due_at is None or _due(weekly_due_at, now)
        ):
            selected = (
                JobType.FINAL_COLLECTION
                if account_state.phase == AccountPhase.FINAL_COLLECTION
                or account_state.weekly_target - account_state.weekly_collected == 1
                else JobType.WEEKLY_FARM
            )

    if selected is None or not resource_context.available_slots:
        return None
    key = f"account:{account_state.account_id}:schedule:v{account_state.schedule_revision}:{selected}"
    return JobIntent(
        account_state.account_id,
        selected,
        key,
        _PRIORITY[selected],
        resource_context.available_slots[0].slot_id,
    )
