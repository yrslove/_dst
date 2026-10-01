from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db import Database, migrate
from app.models import (
    Account,
    AccountScheduleState,
    AccountState,
    GameplayTask,
    Job,
    JobKind,
    JobStatus,
    Node,
    NodeStatus,
    RuntimeInstance,
    RuntimeLease,
    RuntimeState,
)
from app.scheduling import (
    DailyStatus,
    JobType,
    ResourceContext,
    WeeklyState,
    WorkerSlot,
)
from app.services.account_scheduler import AccountScheduler, VerifiedOutcome
from app.services.gameplay import persist_worker_inworld_gift_confirmation
from app.services.jobs import JobQueue
from app.services.leases import LeaseService
from app.services.scheduler import Scheduler

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
SLOT = ResourceContext((WorkerSlot("node-slot-1"),))


def _database(tmp_path):
    url = f"sqlite:///{(tmp_path / 'scheduler.sqlite3').as_posix()}"
    migrate(url, "0010_long_session")
    db = Database(url)
    with db.transaction() as session:
        node = Node(
            name="test-node",
            provider="mock",
            status=NodeStatus.ONLINE,
            max_active_slots=1,
        )
        session.add(node)
        session.flush()
        account = Account(
            label="scheduler-test",
            steam_username=f"scheduler-{tmp_path.name}",
            status=AccountState.RUNNING,
        )
        session.add(account)
        session.flush()
        runtime = RuntimeInstance(
            account_id=account.id,
            node_id=node.id,
            provider="mock",
            external_id=f"runtime-{account.id}",
            image_version="test-image",
            state=RuntimeState.STOPPED,
            active=True,
        )
        session.add(runtime)
        session.flush()
        account_id = account.id
        runtime_id = runtime.id
        node_id = node.id
    migrate(url)
    jobs = JobQueue(
        db, lease_seconds=60, retry_base_seconds=1, max_attempts=2
    )
    leases = LeaseService(db, lease_seconds=60)
    scheduler = AccountScheduler(db, jobs, leases)
    return db, scheduler, jobs, leases, account_id, runtime_id, node_id


def _update_state(db, account_id, **values):
    with db.transaction() as session:
        state = session.get(AccountScheduleState, account_id)
        for key, value in values.items():
            setattr(state, key, value)


def _job(db, account_id):
    with db.session() as session:
        return session.scalar(
            select(Job).where(Job.account_id == account_id).order_by(Job.id.desc())
        )


def test_a_unsynced_account_is_parked_without_job_or_worker_slot(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    with db.session() as session:
        baseline = session.get(AccountScheduleState, account_id)
        assert baseline.daily_status == DailyStatus.UNKNOWN.value
        assert baseline.weekly_state == WeeklyState.UNSYNCED_CURRENT_CYCLE.value
        assert baseline.weekly_collected is None
        assert baseline.weekly_target == 8
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.UNSYNCED_CURRENT_CYCLE.value,
        weekly_collected=None,
        confirmed_claims_current_observation=1,
        weekly_target=8,
        daily_status=DailyStatus.UNKNOWN.value,
    )

    assert Scheduler(
        db, scheduler.jobs, account_scheduler=scheduler
    ).tick() == 0
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.active_job_key is None
        assert session.scalar(select(func.count(Job.id))) == 0
        assert session.scalar(
            select(func.count(RuntimeLease.id)).where(RuntimeLease.released_at.is_(None))
        ) == 0

def test_b_synced_six_of_eight_enqueues_weekly_job_and_applies_verified_claim(tmp_path):
    db, scheduler, _, leases, account_id, runtime_id, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=6,
        weekly_target=8,
        daily_status=DailyStatus.DONE.value,
    )

    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent is not None and intent.job_type == JobType.WEEKLY_FARM
    job = _job(db, account_id)
    assert job.kind == JobKind.LONG_SESSION
    assert job.payload["account_schedule_intent"] == "WEEKLY_FARM"
    leases.claim_slot(runtime_id, job.id)
    assert scheduler.complete(
        account_id,
        intent.idempotency_key,
        VerifiedOutcome(weekly_claim_id="backend-claim-unique-1"),
        now=NOW,
    )

    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        claim_count = session.scalar(
            select(func.count(GameplayTask.id)).where(
                GameplayTask.kind == "INWORLD_WEEKLY_CLAIM"
            )
        )
        assert state.weekly_collected == 7
        assert state.phase == "FINAL_COLLECTION"
        assert state.active_job_key is None
        assert claim_count == 1
        assert session.get(Job, job.id).status == JobStatus.SUCCEEDED
        assert session.scalar(
            select(func.count(RuntimeLease.id)).where(RuntimeLease.released_at.is_(None))
        ) == 0

    second = scheduler.schedule_next(account_id, NOW + timedelta(minutes=1), SLOT)
    assert second is not None and second.job_type == JobType.FINAL_COLLECTION
    second_job = _job(db, account_id)
    leases.claim_slot(runtime_id, second_job.id)
    assert scheduler.complete(
        account_id,
        second.idempotency_key,
        VerifiedOutcome(weekly_claim_id="backend-claim-unique-1"),
        now=NOW + timedelta(minutes=1),
    )
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.weekly_collected == 7
        assert session.scalar(
            select(func.count(GameplayTask.id)).where(
                GameplayTask.kind == "INWORLD_WEEKLY_CLAIM"
            )
        ) == 1


def test_c_pending_gift_precedes_final_collection(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=7,
        weekly_target=8,
        phase="FINAL_COLLECTION",
        daily_status=DailyStatus.DONE.value,
        pending_gift=True,
        next_weekly_eligible_at=NOW - timedelta(minutes=1),
    )

    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent is not None and intent.job_type == JobType.CLAIM_PENDING_GIFT
    assert _job(db, account_id).payload["account_schedule_intent"] == "CLAIM_PENDING_GIFT"


def test_f_fresh_actionable_heartbeat_enqueues_claim_before_farming(tmp_path):
    db, scheduler, _, _, account_id, runtime_id, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=6,
        weekly_target=8,
        phase="FARMING",
        daily_status=DailyStatus.DONE.value,
        next_weekly_eligible_at=NOW - timedelta(minutes=1),
    )
    weekly_intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert weekly_intent is not None and weekly_intent.job_type == JobType.WEEKLY_FARM
    with db.transaction() as session:
        persist_worker_inworld_gift_confirmation(
            session,
            account_id=account_id,
            runtime_id=runtime_id,
            worker_run=None,
            report={
                "last_observation_at": NOW.isoformat(),
                "telemetry": {"inworld_gift_state": "IN_WORLD_GIFT_ACTIONABLE"},
            },
            now=NOW,
            sqlite=True,
        )

    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent is not None and intent.job_type == JobType.CLAIM_PENDING_GIFT
    with db.session() as session:
        jobs = list(session.scalars(select(Job).order_by(Job.id)))
        assert len(jobs) == 2
        assert jobs[0].status == JobStatus.CANCELLED
        assert jobs[1].payload["account_schedule_intent"] == "CLAIM_PENDING_GIFT"


def test_d_weekly_done_with_daily_pending_queues_only_daily_maintenance(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=8,
        weekly_target=8,
        phase="DONE",
        daily_status=DailyStatus.PENDING.value,
        pending_gift=False,
    )

    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent is not None and intent.job_type == JobType.DAILY_MAINTENANCE
    job = _job(db, account_id)
    assert job.kind == JobKind.DAILY_GIFT_CLAIM
    assert job.payload["account_schedule_intent"] == "DAILY_MAINTENANCE"
    assert scheduler.complete(
        account_id,
        intent.idempotency_key,
        VerifiedOutcome(daily_unavailable=True),
        now=NOW,
    )
    with db.session() as session:
        assert session.get(AccountScheduleState, account_id).daily_status == DailyStatus.UNKNOWN.value
    assert scheduler.schedule_next(account_id, NOW, SLOT) is None


def test_e_weekly_and_daily_done_produce_no_job(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=8,
        weekly_target=8,
        phase="DONE",
        daily_status=DailyStatus.DONE.value,
        pending_gift=False,
    )

    assert scheduler.schedule_next(account_id, NOW, SLOT) is None
    assert _job(db, account_id) is None


def test_future_weekly_eligibility_waits_without_reserving_a_job(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=6,
        weekly_target=8,
        daily_status=DailyStatus.DONE.value,
        next_weekly_eligible_at=NOW + timedelta(minutes=1),
    )
    assert scheduler.schedule_next(account_id, NOW, SLOT) is None
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.active_job_key is None
        assert session.scalar(select(func.count(Job.id))) == 0


def test_unavailable_slot_preserves_state_and_creates_no_duplicate_job(tmp_path):
    db, scheduler, _, _, account_id, _, node_id = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=6,
        weekly_target=8,
        daily_status=DailyStatus.DONE.value,
        next_weekly_eligible_at=NOW - timedelta(minutes=1),
    )
    with db.transaction() as session:
        other_account = Account(
            label="occupying-runtime",
            steam_username="occupying-runtime",
            status=AccountState.RUNNING,
        )
        session.add(other_account)
        session.flush()
        session.add(
            RuntimeInstance(
                account_id=other_account.id,
                node_id=node_id,
                provider="mock",
                external_id="runtime-occupier",
                image_version="test-image",
                state=RuntimeState.RUNNING,
                active=True,
            )
        )

    assert scheduler.schedule_next(account_id, NOW, SLOT) is None
    assert scheduler.schedule_next(account_id, NOW, SLOT) is None
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.weekly_collected == 6
        assert state.active_job_key is None
        assert session.scalar(select(func.count(Job.id))) == 0


def test_restart_reloads_reservation_and_does_not_duplicate_job(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=6,
        weekly_target=8,
        daily_status=DailyStatus.DONE.value,
        next_weekly_eligible_at=NOW - timedelta(minutes=1),
    )
    first = scheduler.schedule_next(account_id, NOW, SLOT)
    assert first is not None

    reloaded_db = Database(db.url)
    reloaded_jobs = JobQueue(
        reloaded_db, lease_seconds=60, retry_base_seconds=1, max_attempts=2
    )
    reloaded = AccountScheduler(reloaded_db, reloaded_jobs)
    again = reloaded.schedule_next(account_id, NOW + timedelta(days=1), ResourceContext())
    assert again is not None
    assert again.idempotency_key == first.idempotency_key
    assert again.job_type == first.job_type
    with db.session() as session:
        assert session.scalar(select(func.count(Job.id))) == 1
    reloaded_db.dispose()


def test_confirmed_boundary_syncs_then_daily_completion_is_durable(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(db, account_id, confirmed_claims_current_observation=1)

    assert scheduler.sync_weekly_boundary(account_id, target=8)
    _update_state(db, account_id, daily_status=DailyStatus.PENDING.value)
    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent is not None and intent.job_type == JobType.DAILY_MAINTENANCE
    assert scheduler.complete(
        account_id,
        intent.idempotency_key,
        VerifiedOutcome(daily_claimed=True),
        now=NOW,
    )
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.weekly_state == WeeklyState.SYNCED.value
        assert state.weekly_collected == 0
        assert state.confirmed_claims_current_observation == 0
        assert state.daily_status == DailyStatus.DONE.value
    next_intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert next_intent is not None and next_intent.job_type == JobType.WEEKLY_FARM


def _inworld_report(runtime_id, action_id="action-1", received_frame_id="r1-w1-f2"):
    return {
        "telemetry": {
            "inworld_gift_confirmation": {
                "semantic": "IN_WORLD_GIFT_CONFIRMED",
                "action_id": action_id,
                "received_frame_id": received_frame_id,
                "evidence_frame_id": "r1-w2-f2",
                "evidence_sequence": 2,
                "observed_at": NOW.isoformat(),
                "runtime_id": runtime_id,
                "verification": "RECORDED_CANONICAL_CLOSE_FRESH_WORLD",
                "item_id": 123456,
                "backend": {
                    "operation": "SetItemOpened_Complete",
                    "http_status": 200,
                    "error": False,
                    "ack_sha256": "a" * 64,
                },
            }
        }
    }


def test_heartbeat_receipt_increments_synced_count_once(tmp_path):
    db, _, _, _, account_id, runtime_id, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.SYNCED.value,
        weekly_collected=6,
        weekly_target=8,
    )
    report = _inworld_report(runtime_id)
    for _ in range(2):
        with db.transaction() as session:
            persist_worker_inworld_gift_confirmation(
                session,
                account_id=account_id,
                runtime_id=runtime_id,
                worker_run=None,
                report=report,
                now=NOW,
                sqlite=True,
            )

    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.weekly_collected == 7
        assert state.phase == "FINAL_COLLECTION"
        assert session.scalar(
            select(func.count(GameplayTask.id)).where(
                GameplayTask.kind == "INWORLD_WEEKLY_CLAIM"
            )
        ) == 1


def test_unsynced_heartbeat_receipt_only_increases_lower_bound(tmp_path):
    db, _, _, _, account_id, runtime_id, _ = _database(tmp_path)
    _update_state(
        db,
        account_id,
        weekly_state=WeeklyState.UNSYNCED_CURRENT_CYCLE.value,
        weekly_collected=None,
        confirmed_claims_current_observation=1,
    )
    with db.transaction() as session:
        persist_worker_inworld_gift_confirmation(
            session,
            account_id=account_id,
            runtime_id=runtime_id,
            worker_run=None,
            report=_inworld_report(runtime_id),
            now=NOW,
            sqlite=True,
        )
    with db.session() as session:
        state = session.get(AccountScheduleState, account_id)
        assert state.weekly_collected is None
        assert state.confirmed_claims_current_observation == 2


def test_pause_cancels_queued_automatic_job_without_stopping_runtime(tmp_path):
    db, scheduler, _, _, account_id, runtime_id, _ = _database(tmp_path)
    _update_state(db, account_id, weekly_state="SYNCED", weekly_collected=6, daily_status="DONE")
    intent = scheduler.schedule_next(account_id, NOW, SLOT)
    assert intent is not None
    assert scheduler.pause(account_id) == {"account_id": account_id, "paused": True, "active_jobs": []}
    assert _job(db, account_id).status == JobStatus.CANCELLED
    assert scheduler.schedule_next(account_id, NOW + timedelta(days=1), SLOT) is None
    with db.session() as session:
        assert session.get(Account, account_id).enabled
        assert session.get(RuntimeInstance, runtime_id).active


def test_pause_requires_existing_executor_to_drain_before_manual_ownership(tmp_path):
    db, scheduler, _, _, account_id, _, _ = _database(tmp_path)
    _update_state(db, account_id, weekly_state="SYNCED", weekly_collected=6, daily_status="DONE")
    assert scheduler.schedule_next(account_id, NOW, SLOT)
    job = _job(db, account_id)
    with db.transaction() as session:
        session.get(Job, job.id).status = JobStatus.RUNNING
    assert scheduler.pause(account_id)["active_jobs"] == [job.id]
    assert scheduler.schedule_next(account_id, NOW, SLOT) is None
    assert _job(db, account_id).status == JobStatus.RUNNING
