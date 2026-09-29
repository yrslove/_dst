import pytest

from app.models import GameplayTask, Job
from tests.helpers import make_ready


def test_session_creation_idempotency_and_terminal_preservation(client, app):
    account = make_ready(client, app, "idle-session")
    url = f"/api/v1/accounts/{account['id']}/gameplay/session"
    headers = {"Idempotency-Key": "session-one"}
    first = client.post(url, json={"requested_duration": 600}, headers=headers)
    assert first.status_code == 202, first.text
    task_id = first.json()["gameplay_task_id"]
    again = client.post(url, json={"requested_duration": 600}, headers=headers)
    assert again.json()["gameplay_task_id"] == task_id
    with app.state.db.transaction(immediate=True) as s:
        task = s.get(GameplayTask, task_id)
        assert task.kind == "LONG_SESSION" and task.status == "PENDING"
        from app.models import utcnow

        task.completed_at = utcnow()
        task.status = "COMPLETED"
        task.result_json = {"terminal_reason": "TIME_BUDGET_REACHED"}
        job = s.get(Job, first.json()["job"]["id"])
        job.status = "SUCCEEDED"
    assert (
        client.post(url, json={"requested_duration": 600}, headers=headers).json()[
            "gameplay_task_id"
        ]
        == task_id
    )
    assert client.post(url, json={"requested_duration": 599}).status_code == 422


def test_storage_guard_preserves_runtime_and_records_reason(client, app, monkeypatch):
    account = make_ready(client, app, "idle-storage")
    from types import SimpleNamespace

    import app.services.session as policy

    monkeypatch.setattr(policy.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    requested = client.post(
        f"/api/v1/accounts/{account['id']}/gameplay/session",
        json={"requested_duration": 600},
    )
    assert requested.status_code == 202, requested.text
    app.state.executor.execute_next()
    with app.state.db.session() as s:
        task = s.get(GameplayTask, requested.json()["gameplay_task_id"])
        assert task.status == "NEEDS_ATTENTION"
        assert task.error_code == "STORAGE_GUARD_TRIGGERED"
        assert task.result_json["terminal_reason"] == "STORAGE_GUARD_TRIGGERED"


@pytest.mark.parametrize(
    "failure", [None, "DEAD", "UNKNOWN", "STALE", "RESUME", "PROFILE"]
)
def test_bounded_monitor_completion_failure_resume_and_cleanup(
    client, app, monkeypatch, failure
):
    from dataclasses import replace
    from datetime import timedelta
    from types import SimpleNamespace

    import app.services.session as policy
    from app.models import RuntimeState, WorkerRun, utcnow
    from app.runtime.world_profile import desired_profile_hash

    account = make_ready(client, app, "monitor-" + str(failure))
    ex = app.state.executor
    ex.settings = replace(ex.settings, runtime_safe_idle_world=True)
    requested = client.post(
        f"/api/v1/accounts/{account['id']}/gameplay/session",
        json={"requested_duration": 600},
    ).json()
    now = [utcnow()]
    initial = now[0]
    mode = ["DISABLED"]
    events = []
    with app.state.db.transaction(immediate=True) as s:
        job = s.get(Job, requested["job"]["id"])
        job.payload = {**job.payload, "requested_duration": 1}
        queued = s.get(GameplayTask, requested["gameplay_task_id"])
        queued.result_json = {**queued.result_json, "requested_duration": 1}
        if failure == "RESUME":
            job.payload = {**job.payload, "owns_runtime": True}
            task = s.get(GameplayTask, requested["gameplay_task_id"])
            task.status = "RUNNING"
            run = WorkerRun(
                runtime_id=account["runtime_id"],
                account_id=account["id"],
                plugin="DSTGameWorker",
                mode="ACTIVE",
            )
            s.add(run)
            s.flush()
            task.worker_run_id = run.id
            task.result_json = {
                "requested_duration": 1,
                "state": "IN_WORLD",
                "monitor_started_at": now[0].isoformat(),
                "deadline": (now[0] + timedelta(seconds=1)).isoformat(),
                "checkpoints": 1,
                "recoveries": 0,
                "gift_transitions": [],
                "confirmations": [],
            }
            mode[0] = "ACTIVE"
    obs = {
        "screen": "IN_WORLD_IDLE",
        "validity": "VALID",
        "calibration_verified": True,
        "assets_verified": True,
        "screen_confidence": 0.99,
    }

    def snapshot(_):
        elapsed = (now[0] - initial).total_seconds()
        screen = (
            ("DEAD" if failure == "DEAD" else "UNKNOWN")
            if elapsed >= 2 and failure in ["DEAD", "UNKNOWN"]
            else "IN_WORLD_IDLE"
        )
        stamp = (
            now[0] - timedelta(seconds=41)
            if failure == "STALE" and mode[0] == "ACTIVE"
            else now[0]
        )
        profile_evidence = {
            "enabled": True,
            "status": "WORLD_PROFILE_VERIFIED",
            "verification_scope": "PERSISTED_WORLD_SETTINGS_AND_CURRENT_PROCESS",
            "world_profile_verified": True,
            "loaded_world_verified": True,
            "profile_version": 1,
            "world_session_id": "EA6E12E4296C650B",
            "fixture_manifest_sha256": "a" * 64,
            "settings_fingerprint": desired_profile_hash(),
            "profile_layer": "worldgenoverride.lua",
            "configuration_verified": True,
            "account_id": account["id"],
            "runtime_id": account["runtime_id"],
            "runtime_generation": 1,
            "world_path": "/home/dst/.klei/DoNotStarveTogether/123/Cluster_1/Master/worldgenoverride.lua",
            "desired_profile_hash": desired_profile_hash(),
            "applied_profile_hash": desired_profile_hash(),
            "process_id": 314,
            "process_start_ticks": 880,
            "process_generation": f"r{account['runtime_id']}-g1-p314-t880",
            "process_started_at": (initial - timedelta(seconds=1)).isoformat(),
            "verified_at": now[0].isoformat(),
        }
        if failure == "PROFILE":
            profile_evidence["status"] = "UNVERIFIED"
        return {
            "mode": mode[0],
            "state": "WAITING",
            "healthy": True,
            "dst_running": True,
            "held_inputs": False,
            "updated_at": now[0],
            "last_observation_at": stamp,
            "observation": {**obs, "screen": screen, "source_frame_id": str(elapsed)},
            "telemetry": {"daily_gift_state": "NO_REWARD_AVAILABLE"},
            "world_profile": profile_evidence,
        }

    monkeypatch.setattr(policy, "utcnow", lambda: now[0])
    monkeypatch.setattr(
        policy.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + timedelta(seconds=seconds)),
    )
    monkeypatch.setattr(ex, "_worker_snapshot", snapshot)
    monkeypatch.setattr(ex, "_start", lambda job: events.append("start"))
    monkeypatch.setattr(ex, "_wait_for_game_ready", lambda *a, **k: True)
    monkeypatch.setattr(ex, "_wait_for_fresh_observation", lambda *a, **k: obs)

    def request(*a):
        mode[0] = a[-1]
        return 1

    monkeypatch.setattr(ex, "_request_worker_mode", request)
    monkeypatch.setattr(ex, "_disable_worker", lambda *a, **k: events.append("disable"))
    monkeypatch.setattr(ex, "_managed_task_stop", lambda job: events.append("stop"))
    if failure == "RESUME":
        monkeypatch.setattr(
            ex.provider,
            "inspect",
            lambda *a, **k: SimpleNamespace(state=RuntimeState.RUNNING),
        )
        monkeypatch.setattr(ex, "_latest_active_mode_command", lambda _: 1)
    assert ex.execute_next()
    with app.state.db.session() as s:
        task = s.get(GameplayTask, requested["gameplay_task_id"])
        expected = {
            None: "TIME_BUDGET_REACHED",
            "RESUME": "TIME_BUDGET_REACHED",
            "DEAD": "GAMEPLAY_DEAD",
            "UNKNOWN": "PERCEPTION_UNKNOWN",
            "STALE": "PERCEPTION_STALE",
            "PROFILE": "SAFE_WORLD_PROFILE_UNVERIFIED",
        }[failure]
        assert task.result_json["terminal_reason"] == expected, task.result_json
        assert task.status == (
            "COMPLETED" if failure in [None, "RESUME"] else "NEEDS_ATTENTION"
        )
        assert task.confirmation_key is None
    if failure == "PROFILE":
        assert events == ["start", "stop"]
        assert mode[0] == "DISABLED"
    else:
        assert events[-2:] == ["disable", "stop"]
    if failure == "RESUME":
        assert "start" not in events
