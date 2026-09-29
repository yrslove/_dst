from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from app.db import Database, migrate
from app.models import (
    Account,
    GameplayTask,
    GameplayTaskStatus,
    Node,
    RuntimeInstance,
    RuntimeState,
    WorkerMode,
    WorkerRun,
    utcnow,
)
from app.schemas import RuntimeHeartbeatRequest
from app.services.agents import AgentService
from app.services.security import hash_token


def _database(tmp_path) -> Database:
    url = f"sqlite:///{(tmp_path / 'gameplay.sqlite3').as_posix()}"
    migrate(url)
    return Database(url)


def _runtime(db: Database, *, suffix: str = "one") -> tuple[int, int, int]:
    with db.transaction(immediate=True) as session:
        account = Account(label=f"account-{suffix}", steam_username=f"steam-{suffix}")
        node = Node(name=f"node-{suffix}", provider="mock")
        session.add_all([account, node])
        session.flush()
        runtime = RuntimeInstance(
            account_id=account.id,
            node_id=node.id,
            provider="mock",
            external_id=f"runtime-{suffix}",
            state=RuntimeState.RUNNING,
            image_version="test-image",
            active=True,
        )
        session.add(runtime)
        session.flush()
        run = WorkerRun(
            runtime_id=runtime.id,
            account_id=account.id,
            plugin="dst",
            mode=WorkerMode.ACTIVE,
            started_at=utcnow() - timedelta(minutes=1),
        )
        session.add(run)
        session.flush()
        return account.id, runtime.id, run.id


def _report(
    *,
    gift_state: str,
    confirmation: dict | None = None,
    worker_state: str = "WAITING",
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "plugin": "dst",
        "mode": "ACTIVE",
        "state": worker_state,
        "last_observation_at": now,
        "error_code": "WORKER_INTERVENTION_REQUIRED"
        if worker_state == "ERROR"
        else None,
        "telemetry": {
            "daily_gift_state": gift_state,
            "daily_gift_confirmation": confirmation,
            "worker_active_seconds": 8,
            "pause_seconds": 0,
            "actions_count": 1,
            "recoveries": 0,
        },
    }


def _confirmation(
    action_id: str = "runtime-1:worker-4:action-7", frame_id: str = "r1-w4-f20"
):
    return {
        "semantic": "DAILY_GIFT_CONFIRMED",
        "action_id": action_id,
        "evidence_frame_id": frame_id,
        "evidence_sequence": 20,
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }


def _update(db: Database, run_ids: tuple[int, int, int], report: dict) -> None:
    _, runtime_id, _ = run_ids
    service = AgentService(db, leases=None, settings=None)
    with db.transaction(immediate=True) as session:
        runtime = session.get(RuntimeInstance, runtime_id)
        service._update_worker_run(session, runtime, report, "ACTIVE", utcnow())


def test_confirmation_persists_atomically_and_reload_retains_claim(tmp_path):
    db = _database(tmp_path)
    run_ids = _runtime(db)
    confirmed = _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=_confirmation())
    _update(db, run_ids, confirmed)

    with db.session() as session:
        task = session.scalar(select(GameplayTask))
        assert task is not None
        assert task.account_id == run_ids[0]
        assert task.runtime_id == run_ids[1]
        assert task.worker_run_id == run_ids[2]
        assert task.kind == "DAILY_GIFT_CLAIM"
        assert task.status == GameplayTaskStatus.SUCCEEDED
        assert task.claim_confirmed_at is not None
        assert task.claim_persisted_at is not None
        assert task.result_json["semantic"] == "DAILY_GIFT_CONFIRMED"

    database_url = db.url
    db.dispose()
    reloaded = Database(database_url)
    with reloaded.session() as session:
        task = session.scalar(select(GameplayTask))
        assert task is not None and task.status == GameplayTaskStatus.SUCCEEDED
        assert task.result_json["evidence_frame_id"] == "r1-w4-f20"
    reloaded.dispose()


def test_runtime_heartbeat_persists_worker_pipeline_result(tmp_path):
    db = _database(tmp_path)
    token = "runtime-heartbeat-token"
    with db.transaction(immediate=True) as session:
        account = Account(label="heartbeat-account", steam_username="heartbeat-steam")
        node = Node(name="heartbeat-node", provider="mock")
        session.add_all([account, node])
        session.flush()
        runtime = RuntimeInstance(
            account_id=account.id,
            node_id=node.id,
            provider="mock",
            external_id="heartbeat-runtime",
            state=RuntimeState.RUNNING,
            image_version="test-image",
            active=True,
            verified_at=utcnow(),
            token_hash=hash_token(token),
        )
        session.add(runtime)
        session.flush()
        runtime_id = runtime.id

    class LeaseSink:
        def renew_slot(self, _runtime_id):
            return None

    service = AgentService(
        db,
        leases=LeaseSink(),
        settings=type(
            "Settings", (), {"agent_protocol_version": 1, "resource_sample_seconds": 60}
        )(),
    )
    worker = {
        **_report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=_confirmation()),
        "version": "test",
        "config_version": 1,
        "healthy": True,
        "telemetry": {
            **_report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=_confirmation())[
                "telemetry"
            ],
            "daily_gift_confirmation": _confirmation(),
        },
    }
    payload = RuntimeHeartbeatRequest(
        runtime_id=runtime_id,
        phase="GAME_READY",
        steam_running=True,
        dst_running=True,
        healthy=True,
        worker_plugin="dst",
        worker_state="WAITING",
        worker=worker,
        agent_version="test-agent",
    )
    service.runtime_heartbeat(payload, token)
    service.runtime_heartbeat(payload, token)

    with db.session() as session:
        tasks = list(session.scalars(select(GameplayTask)))
        runs = list(session.scalars(select(WorkerRun)))
        assert len(tasks) == 1
        assert tasks[0].status == GameplayTaskStatus.SUCCEEDED
        assert tasks[0].runtime_id == runtime_id
        assert tasks[0].worker_run_id == runs[0].id
    db.dispose()


def test_duplicate_delivery_and_worker_ticks_create_one_claim(tmp_path):
    db = _database(tmp_path)
    run_ids = _runtime(db)
    confirmation = _confirmation()
    report = _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=confirmation)

    _update(db, run_ids, report)
    _update(db, run_ids, report)
    _update(db, run_ids, report)
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(GameplayTask)) == 1
        task = session.scalar(select(GameplayTask))
        assert task.claim_persisted_at is not None
    db.dispose()


def test_database_uniqueness_rejects_same_claim_identity(tmp_path):
    db = _database(tmp_path)
    run_ids = _runtime(db)
    _update(
        db,
        run_ids,
        _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=_confirmation()),
    )
    with pytest.raises(IntegrityError), db.transaction(immediate=True) as session:
        task = session.scalar(select(GameplayTask))
        session.add(
            GameplayTask(
                account_id=task.account_id,
                runtime_id=task.runtime_id,
                worker_run_id=task.worker_run_id,
                kind=task.kind,
                status=GameplayTaskStatus.SUCCEEDED,
                started_at=task.started_at,
                updated_at=utcnow(),
                completed_at=utcnow(),
                confirmation_key=task.confirmation_key,
                claim_confirmed_at=task.claim_confirmed_at,
                claim_persisted_at=utcnow(),
                result_json=task.result_json,
            )
        )
        session.flush()
    db.dispose()


def test_incomplete_and_failed_tasks_are_not_successful_claims(tmp_path):
    db = _database(tmp_path)
    run_ids = _runtime(db)
    _update(db, run_ids, _report(gift_state="GIFT_AVAILABLE"))
    with db.session() as session:
        task = session.scalar(select(GameplayTask))
        assert task.status == GameplayTaskStatus.RUNNING
        assert task.confirmation_key is None
        assert task.claim_confirmed_at is None

    _update(db, run_ids, _report(gift_state="GIFT_UI_OPEN"))
    with db.session() as session:
        task = session.scalar(select(GameplayTask))
        assert task.status == GameplayTaskStatus.RUNNING
        assert task.confirmation_key is None
        assert task.claim_confirmed_at is None

    _update(db, run_ids, _report(gift_state="GIFT_UI_CLOSED"))
    with db.session() as session:
        task = session.scalar(select(GameplayTask))
        assert task.status == GameplayTaskStatus.NEEDS_ATTENTION
        assert task.status != GameplayTaskStatus.SUCCEEDED

    second = _runtime(db, suffix="failed")
    _update(
        db,
        second,
        _report(gift_state="GIFT_AVAILABLE", worker_state="ERROR"),
    )
    with db.session() as session:
        failed = session.scalar(
            select(GameplayTask).where(GameplayTask.account_id == second[0])
        )
        assert failed.status == GameplayTaskStatus.FAILED
        assert failed.error_code == "WORKER_INTERVENTION_REQUIRED"
        assert failed.claim_confirmed_at is None
    db.dispose()


def test_restart_retry_reconciles_committed_claim_and_new_cycles_are_distinct(tmp_path):
    db = _database(tmp_path)
    run_ids = _runtime(db)
    first = _confirmation("action-1", "r1-w4-f20")
    _update(db, run_ids, _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=first))
    database_url = db.url
    db.dispose()

    restarted = Database(database_url)
    # A lost heartbeat response can redeliver the already committed result.
    _update(
        restarted,
        run_ids,
        _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=first),
    )
    _update(restarted, run_ids, _report(gift_state="GIFT_AVAILABLE"))
    second = _confirmation("action-2", "r1-w4-f48")
    _update(
        restarted,
        run_ids,
        _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=second),
    )
    with restarted.session() as session:
        rows = list(
            session.scalars(
                select(GameplayTask)
                .where(GameplayTask.account_id == run_ids[0])
                .order_by(GameplayTask.id)
            )
        )
        assert len(rows) == 2
        assert all(row.status == GameplayTaskStatus.SUCCEEDED for row in rows)
        assert rows[0].confirmation_key != rows[1].confirmation_key
    restarted.dispose()


def test_failed_transaction_leaves_no_success_and_redelivery_can_commit(tmp_path):
    db = _database(tmp_path)
    run_ids = _runtime(db)
    report = _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=_confirmation())
    service = AgentService(db, leases=None, settings=None)
    with (
        pytest.raises(RuntimeError, match="simulated transaction failure"),
        db.transaction(immediate=True) as session,
    ):
        service._update_worker_run(
            session,
            session.get(RuntimeInstance, run_ids[1]),
            report,
            "ACTIVE",
            utcnow(),
        )
        raise RuntimeError("simulated transaction failure")
    with db.session() as session:
        assert session.scalar(select(func.count()).select_from(GameplayTask)) == 0

    _update(db, run_ids, report)
    with db.session() as session:
        task = session.scalar(select(GameplayTask))
        assert task.status == GameplayTaskStatus.SUCCEEDED
    db.dispose()


def test_confirmation_is_scoped_to_account_and_worker_run(tmp_path):
    db = _database(tmp_path)
    first = _runtime(db, suffix="account-a")
    second = _runtime(db, suffix="account-b")
    identical = _confirmation("shared-action", "shared-frame")
    _update(
        db, first, _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=identical)
    )
    _update(
        db, second, _report(gift_state="DAILY_GIFT_CONFIRMED", confirmation=identical)
    )
    with db.session() as session:
        rows = list(
            session.scalars(select(GameplayTask).order_by(GameplayTask.account_id))
        )
        assert len(rows) == 2
        assert rows[0].account_id == first[0]
        assert rows[0].worker_run_id == first[2]
        assert rows[1].account_id == second[0]
        assert rows[1].worker_run_id == second[2]
    db.dispose()


def test_success_check_and_indexes_exist_on_sqlite_migration(tmp_path):
    db = _database(tmp_path)
    inspector = inspect(db.engine)
    assert "gameplay_tasks" in inspector.get_table_names()
    assert "uq_gameplay_task_claim_identity" in {
        item.get("name") for item in inspector.get_unique_constraints("gameplay_tasks")
    }
    assert "uq_gameplay_task_active_account_kind" in {
        item.get("name") for item in inspector.get_indexes("gameplay_tasks")
    }
    db.dispose()
