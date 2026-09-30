from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import Database, migrate
from app.models import Account, AccountScheduleState
from app.scheduling import (
    AccountPhase,
    AccountSchedule,
    DailyStatus,
    JobType,
    ResourceContext,
    WorkerSlot,
    decide_next_job,
)
from app.services.account_scheduler import AccountScheduler, VerifiedOutcome

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
SLOT = ResourceContext((WorkerSlot("slot-1"),))


def _state(**changes) -> AccountSchedule:
    values = {
        "account_id": 17,
        "daily_status": DailyStatus.DONE,
        "weekly_collected": 0,
        "weekly_target": 5,
        "next_weekly_eligible_at": NOW - timedelta(minutes=1),
    }
    values.update(changes)
    return AccountSchedule(**values)


def test_daily_pending_and_weekly_done_selects_daily():
    state = _state(
        daily_status=DailyStatus.PENDING,
        weekly_collected=5,
        weekly_target=5,
    )
    assert decide_next_job(state, NOW, SLOT).job_type == JobType.DAILY_MAINTENANCE


def test_daily_done_and_weekly_incomplete_selects_weekly_farm():
    assert decide_next_job(_state(), NOW, SLOT).job_type == JobType.WEEKLY_FARM


def test_pending_gift_has_highest_priority():
    intent = decide_next_job(
        _state(daily_status=DailyStatus.PENDING, pending_gift=True), NOW, SLOT
    )
    assert intent.job_type == JobType.CLAIM_PENDING_GIFT
    assert intent.priority < 20


def test_weekly_near_completion_selects_final_collection():
    state = _state(weekly_collected=4, weekly_target=5)
    assert decide_next_job(state, NOW, SLOT).job_type == JobType.FINAL_COLLECTION


def test_weekly_complete_and_account_done_have_no_job():
    state = _state(
        weekly_collected=5,
        weekly_target=5,
        phase=AccountPhase.DONE,
    )
    assert decide_next_job(state, NOW, SLOT) is None


def test_account_completely_done_has_no_job():
    state = _state(
        daily_status=DailyStatus.DONE,
        weekly_collected=5,
        weekly_target=5,
        pending_gift=False,
    )
    assert decide_next_job(state, NOW, SLOT) is None


def test_unavailable_worker_slot_blocks_new_job():
    assert decide_next_job(_state(), NOW, ResourceContext()) is None


def test_due_time_and_configuration_estimate_are_respected():
    state = _state(
        weekly_collected=2,
        next_weekly_eligible_at=None,
        estimated_due_at=NOW + timedelta(minutes=1),
        estimated_time_to_gift_seconds=900,
    )
    assert decide_next_job(state, NOW, SLOT) is None
    assert (
        decide_next_job(state, NOW + timedelta(minutes=1), SLOT).job_type
        == JobType.WEEKLY_FARM
    )


def test_weekly_timing_unknown_does_not_guess_eligibility():
    state = _state(next_weekly_eligible_at=None, estimated_due_at=None)
    assert decide_next_job(state, NOW, SLOT) is None


def _database(tmp_path) -> tuple[Database, int]:
    url = f"sqlite:///{(tmp_path / 'scheduler.sqlite3').as_posix()}"
    migrate(url)
    db = Database(url)
    with db.transaction() as session:
        account = Account(label="scheduled", steam_username="scheduler-test")
        session.add(account)
        session.flush()
        account_id = account.id
        session.add(
            AccountScheduleState(
                account_id=account_id,
                daily_status=DailyStatus.PENDING,
                weekly_collected=0,
                weekly_target=4,
                phase=AccountPhase.FARMING,
                pending_gift=False,
                next_daily_due_at=NOW,
                next_weekly_eligible_at=NOW + timedelta(hours=1),
                estimated_time_to_gift_seconds=3600,
                schedule_revision=3,
            )
        )
    return db, account_id


def test_success_updates_state_from_verified_worker_facts(tmp_path):
    db, account_id = _database(tmp_path)
    scheduler = AccountScheduler(db)
    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent.job_type == JobType.DAILY_MAINTENANCE

    assert scheduler.complete(
        account_id,
        intent.idempotency_key,
        VerifiedOutcome(daily_claimed=True, gift_pending=False),
        now=NOW,
    )
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.daily_status == DailyStatus.DONE
        assert state.last_daily_claim_at.replace(tzinfo=timezone.utc) == NOW
        assert state.weekly_collected == 0
        assert state.active_job_key is None
        assert state.schedule_revision == 4


def test_retryable_failure_allows_same_idempotent_job_to_retry(tmp_path):
    db, account_id = _database(tmp_path)
    scheduler = AccountScheduler(db)
    first = scheduler.schedule_next(account_id, NOW, SLOT)
    assert scheduler.retryable_failure(account_id, first.idempotency_key)
    retried = scheduler.schedule_next(account_id, NOW, SLOT)
    assert retried.idempotency_key == first.idempotency_key


def test_duplicate_scheduling_and_restart_recover_same_reservation(tmp_path):
    db, account_id = _database(tmp_path)
    first_scheduler = AccountScheduler(db)
    first = first_scheduler.schedule_next(account_id, NOW, SLOT)
    duplicate = first_scheduler.schedule_next(account_id, NOW, SLOT)
    assert duplicate.idempotency_key == first.idempotency_key

    # A new service/database handle models a process restart reading durable state.
    reloaded_db = Database(db.url)
    reloaded = AccountScheduler(reloaded_db).schedule_next(
        account_id, NOW + timedelta(days=1), ResourceContext()
    )
    assert reloaded.idempotency_key == first.idempotency_key
    assert reloaded.job_type == first.job_type
    reloaded_db.dispose()


def test_stale_result_cannot_update_a_new_or_released_job(tmp_path):
    db, account_id = _database(tmp_path)
    scheduler = AccountScheduler(db)
    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert scheduler.retryable_failure(account_id, intent.idempotency_key)
    assert not scheduler.complete(
        account_id,
        intent.idempotency_key,
        VerifiedOutcome(daily_claimed=True),
        now=NOW,
    )
