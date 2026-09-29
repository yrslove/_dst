from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.models import (
    GameplayTask,
    GameplayTaskStatus,
    Job,
    RuntimeInstance,
    RuntimeState,
    WorkerCommand,
    WorkerStatus,
)
from app.schemas import RuntimeHeartbeatRequest
from tests.helpers import make_ready


def _agent_report(
    app,
    runtime_id,
    token,
    *,
    mode,
    state,
    command_id=None,
    gift_state="UNKNOWN",
    confirmation=None,
):
    observed_at = datetime.now(UTC).isoformat()
    observation = {
        "source_frame_id": f"synthetic-{observed_at}-{mode}",
        "source_sequence": 1,
        "screen": "MAIN_MENU",
        "screen_confidence": 0.99,
        "validity": "VALID",
        "calibration_verified": True,
        "assets_verified": True,
    }
    availability_evidence = None
    if gift_state == "NO_REWARD_AVAILABLE":
        observation["screen"] = "IN_WORLD_IDLE"
        availability_evidence = {
            "semantic": gift_state,
            "availability": gift_state,
            "icon_present": True,
            "icon_state": "INACTIVE",
            "identity_confidence": 0.99,
            "evidence_frame_id": observation["source_frame_id"],
        }
    worker = {
        "plugin": "DSTGameWorker",
        "version": "test",
        "config_version": 1,
        "mode": mode,
        "state": state,
        "healthy": True,
        "last_tick_at": observed_at,
        "last_observation_at": observed_at,
        "error_code": None,
        "telemetry": {
            "daily_gift_state": gift_state,
            "daily_gift_confirmation": confirmation,
            "gift_availability_evidence": availability_evidence,
        },
        "details": {
            "observation": observation,
            "diagnostics": {"input_safety": {"held_inputs": False}},
        },
    }
    payload = RuntimeHeartbeatRequest.model_validate(
        {
            "runtime_id": runtime_id,
            "phase": "GAME_READY",
            "steam_running": True,
            "dst_running": True,
            "healthy": True,
            "automation_state": state,
            "worker_plugin": "DSTGameWorker",
            "worker_state": state,
            "worker_last_tick_at": observed_at,
            "worker_last_observation_at": observed_at,
            "worker": worker,
            "worker_command_results": (
                [{"id": command_id, "result": "OK"}] if command_id else []
            ),
            "details": {"worker": worker},
            "agent_version": "synthetic-stage3",
            "protocol_version": 1,
        }
    )
    app.state.agents.runtime_heartbeat(payload, token)
    return observation


def _confirmation():
    observed_at = datetime.now(UTC).isoformat()
    return {
        "semantic": "DAILY_GIFT_CONFIRMED",
        "action_id": "synthetic-action-1",
        "evidence_frame_id": "synthetic-confirmation-frame",
        "evidence_sequence": 2,
        "observed_at": observed_at,
    }


def _task_request(client, account_id):
    response = client.post(f"/api/v1/accounts/{account_id}/gameplay/daily-gift")
    assert response.status_code == 202, response.text
    return response.json()


def _short_bounds(app):
    app.state.settings.gameplay_readiness_timeout_seconds = 0.01
    app.state.settings.gameplay_observation_timeout_seconds = 0.01
    app.state.settings.gameplay_execution_timeout_seconds = 0.01
    app.state.settings.gameplay_poll_interval_seconds = 0.001


def test_daily_gift_task_runs_managed_synthetic_lifecycle(client, app, monkeypatch):
    account = make_ready(client, app, "daily-gift-e2e")
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    _short_bounds(app)

    requested = _task_request(client, account["id"])
    task_id = requested["gameplay_task_id"]
    with app.state.db.session() as session:
        task = session.get(GameplayTask, task_id)
        assert task.status == GameplayTaskStatus.PENDING
        assert task.account_id == account["id"]
        assert task.runtime_id == account["runtime_id"]

    def ready(runtime_id, *, after, timeout):
        with app.state.db.transaction(immediate=True) as session:
            runtime = session.get(RuntimeInstance, runtime_id)
            runtime.state = RuntimeState.RUNNING
        _agent_report(app, runtime_id, token, mode="DISABLED", state="DISABLED")
        return True

    def fresh_observation(runtime_id, *, after, timeout, mode, command_id):
        if mode == "OBSERVE":
            return _agent_report(
                app,
                runtime_id,
                token,
                mode=mode,
                state="OBSERVING",
                command_id=command_id,
            )
        return _agent_report(
            app,
            runtime_id,
            token,
            mode=mode,
            state="OBSERVING",
            command_id=command_id,
            gift_state="DAILY_GIFT_CONFIRMED",
            confirmation=_confirmation(),
        )

    def disable(control, job, *, timeout):
        command_id = app.state.executor._request_worker_mode(control, job, "DISABLED")
        _agent_report(
            app,
            job.runtime_id,
            token,
            mode="DISABLED",
            state="DISABLED",
            command_id=command_id,
        )

    monkeypatch.setattr(app.state.executor, "_wait_for_game_ready", ready)
    monkeypatch.setattr(
        app.state.executor, "_wait_for_fresh_observation", fresh_observation
    )
    monkeypatch.setattr(app.state.executor, "_disable_worker", disable)

    assert app.state.executor.execute_next()
    tasks = client.get(f"/api/v1/accounts/{account['id']}/gameplay/tasks").json()
    result = next(item for item in tasks if item["id"] == task_id)
    assert result["status"] == GameplayTaskStatus.SUCCEEDED
    assert result["result"]["semantic"] == "DAILY_GIFT_CONFIRMED"
    next_request = _task_request(client, account["id"])
    assert next_request["gameplay_task_id"] != task_id
    with app.state.db.session() as session:
        assert (
            session.scalar(
                select(GameplayTask).where(GameplayTask.id == task_id)
            ).claim_persisted_at
            is not None
        )
        assert (
            session.scalar(
                select(WorkerStatus).where(
                    WorkerStatus.runtime_id == account["runtime_id"]
                )
            ).worker_mode
            == "DISABLED"
        )
        assert (
            session.scalar(
                select(RuntimeInstance).where(
                    RuntimeInstance.id == account["runtime_id"]
                )
            ).state
            == RuntimeState.STOPPED
        )
    with app.state.db.session() as session:
        job = session.scalar(select(Job).where(Job.id == requested["job"]["id"]))
        assert job is not None and job.payload["owns_runtime"] is True
        before = session.scalar(
            select(func.count())
            .select_from(WorkerCommand)
            .where(WorkerCommand.runtime_id == account["runtime_id"])
        )
        session.expunge(job)
    app.state.executor._daily_gift_claim(job)
    with app.state.db.session() as session:
        after = session.scalar(
            select(func.count())
            .select_from(WorkerCommand)
            .where(WorkerCommand.runtime_id == account["runtime_id"])
        )
        assert after == before


def test_daily_gift_task_is_created_before_execution_and_account_owned(client, app):
    account = make_ready(client, app, "daily-gift-durable")
    requested = client.post(f"/api/v1/accounts/{account['id']}/gameplay/daily-gift")
    assert requested.status_code == 202
    task_id = requested.json()["gameplay_task_id"]
    duplicate = client.post(f"/api/v1/accounts/{account['id']}/gameplay/daily-gift")
    assert duplicate.status_code == 202
    assert duplicate.json()["gameplay_task_id"] == task_id
    with app.state.db.session() as session:
        task = session.get(GameplayTask, task_id)
        job = session.get(Job, requested.json()["job"]["id"])
        assert task.status == GameplayTaskStatus.PENDING
        assert job.kind == "DAILY_GIFT_CLAIM"
        assert job.payload["gameplay_task_id"] == task.id


def test_new_attempt_preserves_previous_attention_task(client, app):
    account = make_ready(client, app, "daily-gift-after-attention")
    previous = _task_request(client, account["id"])
    previous_id = previous["gameplay_task_id"]
    with app.state.db.transaction(immediate=True) as session:
        task = session.get(GameplayTask, previous_id)
        task.status = GameplayTaskStatus.NEEDS_ATTENTION
        task.error_code = "WORKER_OBSERVATION_UNSAFE"
        task.completed_at = datetime.now(UTC)
        session.get(Job, previous["job"]["id"]).status = "SUCCEEDED"
    before = client.get(f"/api/v1/accounts/{account['id']}/gameplay/tasks").json()
    new = _task_request(client, account["id"])
    assert new["gameplay_task_id"] != previous_id
    with app.state.db.session() as session:
        old = session.get(GameplayTask, previous_id)
        current = session.get(GameplayTask, new["gameplay_task_id"])
        assert old.status == GameplayTaskStatus.NEEDS_ATTENTION
        assert old.error_code == "WORKER_OBSERVATION_UNSAFE"
        assert current.status == GameplayTaskStatus.PENDING
    assert any(row["id"] == previous_id for row in before)


def test_live_report_shape_is_safe_and_character_selection_is_rejected(client, app):
    account = make_ready(client, app, "daily-gift-real-report")
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    with app.state.db.transaction(immediate=True) as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = RuntimeState.RUNNING
    _agent_report(
        app,
        account["runtime_id"],
        token,
        mode="OBSERVE",
        state="OBSERVING",
    )
    snapshot = app.state.executor._worker_snapshot(account["runtime_id"])
    assert snapshot["observation"]["validity"] == "VALID"
    assert snapshot["held_inputs"] is False
    assert app.state.executor._safe_observation(snapshot["observation"])
    with app.state.db.transaction(immediate=True) as session:
        command = WorkerCommand(
            runtime_id=account["runtime_id"],
            command="SET_MODE",
            payload={"mode": "OBSERVE"},
            status="COMPLETED",
            result="OK",
            created_by="test",
            created_at=datetime.now(UTC),
        )
        session.add(command)
        session.flush()
        command_id = command.id
    fresh = app.state.executor._wait_for_fresh_observation(
        account["runtime_id"],
        after=datetime.now(UTC) - timedelta(seconds=5),
        timeout=0.01,
        mode="OBSERVE",
        command_id=command_id,
    )
    assert fresh["source_frame_id"] == snapshot["observation"]["source_frame_id"]
    assert not app.state.executor._safe_observation(
        {**snapshot["observation"], "screen": "CHARACTER_SELECTION"}
    )


def test_character_selection_executor_gate_requires_verified_structural_anchors(app):
    observation = {
        "screen": "CHARACTER_SELECTION",
        "screen_confidence": 0.98,
        "production_ready": True,
        "detections": [
            {
                "kind": name,
                "detected": True,
                "verified": True,
                "confidence": 0.98,
                "bounds": {"left": 0.1, "top": 0.1, "right": 0.2, "bottom": 0.2},
            }
            for name in (
                "character_select_title",
                "character_select_players",
                "character_select_wilson_icon",
            )
        ],
    }
    assert app.state.executor._safe_observation(observation)
    ambiguous = {
        **observation,
        "detections": observation["detections"][:-1],
    }
    assert not app.state.executor._safe_observation(ambiguous)


def test_game_ready_timestamp_from_sqlite_is_compared_as_utc(client, app):
    account = make_ready(client, app, "daily-gift-sqlite-time")
    stored_at = datetime.now(UTC)
    after = stored_at.replace(microsecond=max(0, stored_at.microsecond - 1))
    with app.state.db.transaction(immediate=True) as session:
        worker = session.get(WorkerStatus, account["runtime_id"])
        worker.phase = "GAME_READY"
        worker.steam_running = True
        worker.dst_running = True
        worker.healthy = True
        # SQLite returns DateTime values without tzinfo even when stored UTC.
        worker.updated_at = stored_at.replace(tzinfo=None)
    assert app.state.executor._wait_for_game_ready(
        account["runtime_id"], after=after, timeout=0.01
    )


def test_readiness_timeout_fails_task_and_stops_owned_runtime(client, app, monkeypatch):
    account = make_ready(client, app, "daily-gift-ready-timeout")
    _short_bounds(app)
    requested = _task_request(client, account["id"])
    monkeypatch.setattr(
        app.state.executor, "_wait_for_game_ready", lambda *args, **kwargs: False
    )

    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        task = session.get(GameplayTask, requested["gameplay_task_id"])
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        job = session.get(Job, requested["job"]["id"])
        assert task.status == GameplayTaskStatus.FAILED
        assert task.error_code == "TimeoutError"
        assert runtime.state == RuntimeState.STOPPED
        assert job.status == "FAILED"


def test_explicit_no_reward_is_terminal_without_claim_success(client, app, monkeypatch):
    account = make_ready(client, app, "daily-gift-unavailable")
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    _short_bounds(app)
    requested = _task_request(client, account["id"])

    def ready(runtime_id, *, after, timeout):
        with app.state.db.transaction(immediate=True) as session:
            session.get(RuntimeInstance, runtime_id).state = RuntimeState.RUNNING
        _agent_report(app, runtime_id, token, mode="DISABLED", state="DISABLED")
        return True

    def fresh_observation(runtime_id, *, after, timeout, mode, command_id):
        observation = _agent_report(
            app,
            runtime_id,
            token,
            mode=mode,
            state="OBSERVING",
            command_id=command_id,
            gift_state="NO_REWARD_AVAILABLE" if mode == "ACTIVE" else "UNKNOWN",
        )
        if mode == "ACTIVE":
            assert (
                app.state.executor._worker_snapshot(runtime_id)["telemetry"][
                    "daily_gift_state"
                ]
                == "NO_REWARD_AVAILABLE"
            )
        return observation

    def disable(control, job, *, timeout):
        command_id = app.state.executor._request_worker_mode(control, job, "DISABLED")
        _agent_report(
            app,
            job.runtime_id,
            token,
            mode="DISABLED",
            state="DISABLED",
            command_id=command_id,
        )

    monkeypatch.setattr(app.state.executor, "_wait_for_game_ready", ready)
    monkeypatch.setattr(
        app.state.executor, "_wait_for_fresh_observation", fresh_observation
    )
    monkeypatch.setattr(app.state.executor, "_disable_worker", disable)
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        task = session.get(GameplayTask, requested["gameplay_task_id"])
        assert task.status == GameplayTaskStatus.NO_REWARD_AVAILABLE, (
            task.status,
            task.error_code,
            task.error_message,
        )
        assert task.result_json["semantic"] == "NO_REWARD_AVAILABLE"
        assert task.result_json["gift_icon_evidence"]["icon_present"] is True
        assert task.claim_confirmed_at is None


def test_no_reward_requires_current_fresh_icon_proof(app):
    now = datetime.now(UTC)
    task = GameplayTask(started_at=now - timedelta(seconds=1))
    snapshot = {
        "last_observation_at": now,
        "telemetry": {"daily_gift_state": "NO_REWARD_AVAILABLE"},
        "observation": {
            "screen": "IN_WORLD_IDLE",
            "screen_confidence": 0.99,
            "production_ready": True,
            "source_frame_id": "fresh-icon",
        },
    }
    proof = {
        "semantic": "NO_REWARD_AVAILABLE",
        "availability": "NO_REWARD_AVAILABLE",
        "icon_state": "INACTIVE",
        "icon_present": True,
        "identity_confidence": 0.99,
        "evidence_frame_id": "fresh-icon",
    }
    assert app.state.executor._verified_no_reward(snapshot, task, proof)
    assert not app.state.executor._verified_no_reward(snapshot, task, None)
    assert not app.state.executor._verified_no_reward(
        snapshot, task, {**proof, "evidence_frame_id": "old"}
    )
    assert not app.state.executor._verified_no_reward(
        snapshot, task, {**proof, "icon_state": "UNKNOWN"}
    )
    snapshot["last_observation_at"] = now - timedelta(seconds=20)
    assert not app.state.executor._verified_no_reward(snapshot, task, proof)


def test_stale_observation_stops_without_activating_worker(client, app, monkeypatch):
    account = make_ready(client, app, "daily-gift-stale-observation")
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    _short_bounds(app)
    requested = _task_request(client, account["id"])

    def ready(runtime_id, *, after, timeout):
        with app.state.db.transaction(immediate=True) as session:
            session.get(RuntimeInstance, runtime_id).state = RuntimeState.RUNNING
        _agent_report(app, runtime_id, token, mode="DISABLED", state="DISABLED")
        return True

    def stale_observation(runtime_id, *, after, timeout, mode, command_id):
        observation = _agent_report(
            app,
            runtime_id,
            token,
            mode=mode,
            state="OBSERVING",
            command_id=command_id,
        )
        observation["production_ready"] = False
        observation["screen"] = "UNKNOWN"
        return observation

    def disable(control, job, *, timeout):
        command_id = app.state.executor._request_worker_mode(control, job, "DISABLED")
        _agent_report(
            app,
            job.runtime_id,
            token,
            mode="DISABLED",
            state="DISABLED",
            command_id=command_id,
        )

    monkeypatch.setattr(app.state.executor, "_wait_for_game_ready", ready)
    monkeypatch.setattr(
        app.state.executor, "_wait_for_fresh_observation", stale_observation
    )
    monkeypatch.setattr(app.state.executor, "_disable_worker", disable)
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        task = session.get(GameplayTask, requested["gameplay_task_id"])
        commands = list(
            session.scalars(
                select(WorkerCommand).where(
                    WorkerCommand.runtime_id == account["runtime_id"],
                    WorkerCommand.command == "SET_MODE",
                )
            )
        )
        assert task.status == GameplayTaskStatus.NEEDS_ATTENTION
        assert task.error_code == "WORKER_OBSERVATION_UNSAFE"
        assert [command.payload["mode"] for command in commands] == [
            "OBSERVE",
            "DISABLED",
        ]


def test_restart_before_success_does_not_reactivate_worker(client, app, monkeypatch):
    account = make_ready(client, app, "daily-gift-restart-before-success")
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    _short_bounds(app)
    requested = _task_request(client, account["id"])
    with app.state.db.transaction(immediate=True) as session:
        task = session.get(GameplayTask, requested["gameplay_task_id"])
        task.status = GameplayTaskStatus.RUNNING

    def ready(runtime_id, *, after, timeout):
        with app.state.db.transaction(immediate=True) as session:
            session.get(RuntimeInstance, runtime_id).state = RuntimeState.RUNNING
        _agent_report(app, runtime_id, token, mode="DISABLED", state="DISABLED")
        return True

    monkeypatch.setattr(app.state.executor, "_wait_for_game_ready", ready)
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        task = session.get(GameplayTask, requested["gameplay_task_id"])
        commands = list(
            session.scalars(
                select(WorkerCommand).where(
                    WorkerCommand.runtime_id == account["runtime_id"],
                    WorkerCommand.command == "SET_MODE",
                )
            )
        )
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert task.status == GameplayTaskStatus.NEEDS_ATTENTION
        assert task.error_code == "ORCHESTRATION_RESTARTED"
        assert all(command.payload["mode"] != "ACTIVE" for command in commands)
        assert runtime.state == RuntimeState.STOPPED
