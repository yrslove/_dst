from datetime import timedelta

from app.domain.state import transition_account, transition_runtime
from app.models import (
    Account,
    AccountState,
    DesiredState,
    ErrorCode,
    Node,
    NodeStatus,
    Run,
    RuntimeInstance,
    RuntimeState,
    WorkerStatus,
    utcnow,
)
from tests.helpers import create_account


def heartbeat_payload(runtime_id: int) -> dict:
    return {
        "runtime_id": runtime_id,
        "phase": "GAME_READY",
        "steam_running": True,
        "dst_running": True,
        "healthy": True,
        "automation_state": "NOOP",
        "details": {},
        "agent_version": "test-1",
        "protocol_version": 1,
    }


def stale_verified_runtime(client, app, label: str) -> tuple[dict, str]:
    account = create_account(client, label)
    assert app.state.executor.execute_next()
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
    assert app.state.executor.execute_next()
    with app.state.db.transaction(immediate=True) as session:
        stored = session.get(Account, account["id"])
        stored.status = AccountState.NEEDS_ATTENTION
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = RuntimeState.STALE
        runtime.desired_state = DesiredState.RUNNING
        runtime.verified_at = utcnow()
        runtime.last_error_code = ErrorCode.AGENT_STALE
        runtime.last_error_message = "Detected AGENT_STALE; capacity retained"
        node = session.get(Node, runtime.node_id)
        node.status = NodeStatus.ONLINE
    return account, token


def send_heartbeat(client, runtime_id: int, token: str, payload: dict | None = None):
    return client.post(
        "/api/v1/runtime-agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json=payload or heartbeat_payload(runtime_id),
    )


def test_stale_runtime_recovers_from_healthy_authenticated_heartbeat(client, app):
    account, token = stale_verified_runtime(client, app, "stale-heartbeat-recovers")

    response = send_heartbeat(
        client, account["runtime_id"], token, heartbeat_payload(account["runtime_id"])
    )

    assert response.status_code == 200
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert runtime.state == RuntimeState.RUNNING
        assert runtime.last_error_code is None
        assert runtime.last_error_message is None
        assert session.get(Account, account["id"]).status == AccountState.RUNNING


def test_stale_runtime_does_not_recover_without_steam_or_dst_readiness(client, app):
    for label, changes in (
        ("stale-no-steam", {"steam_running": False}),
        ("stale-no-dst", {"dst_running": False}),
    ):
        account, token = stale_verified_runtime(client, app, label)
        payload = heartbeat_payload(account["runtime_id"])
        payload.update(changes)
        assert send_heartbeat(client, account["runtime_id"], token, payload).status_code == 200
        with app.state.db.session() as session:
            assert session.get(RuntimeInstance, account["runtime_id"]).state == RuntimeState.STALE
            assert session.get(Account, account["id"]).status == AccountState.NEEDS_ATTENTION


def test_stale_runtime_does_not_recover_when_desired_stopped(client, app):
    account, token = stale_verified_runtime(client, app, "stale-desired-stopped")
    with app.state.db.transaction(immediate=True) as session:
        session.get(RuntimeInstance, account["runtime_id"]).desired_state = DesiredState.STOPPED

    assert send_heartbeat(
        client, account["runtime_id"], token, heartbeat_payload(account["runtime_id"])
    ).status_code == 200
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).state == RuntimeState.STALE
        assert session.get(Account, account["id"]).status == AccountState.NEEDS_ATTENTION


def test_stale_runtime_does_not_recover_from_inactive_generation(client, app):
    account, token = stale_verified_runtime(client, app, "stale-old-generation")
    with app.state.db.transaction(immediate=True) as session:
        session.get(RuntimeInstance, account["runtime_id"]).active = False

    response = send_heartbeat(
        client, account["runtime_id"], token, heartbeat_payload(account["runtime_id"])
    )
    assert response.status_code == 401
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).state == RuntimeState.STALE


def test_stale_runtime_does_not_clear_nonrecoverable_error(client, app):
    account, token = stale_verified_runtime(client, app, "stale-fatal-error")
    with app.state.db.transaction(immediate=True) as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.last_error_code = ErrorCode.RUNTIME_NOT_FOUND
        runtime.last_error_message = "Provider confirms missing runtime"

    assert send_heartbeat(
        client, account["runtime_id"], token, heartbeat_payload(account["runtime_id"])
    ).status_code == 200
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert runtime.state == RuntimeState.STALE
        assert runtime.last_error_code == ErrorCode.RUNTIME_NOT_FOUND
        assert runtime.last_error_message == "Provider confirms missing runtime"
        assert session.get(Account, account["id"]).status == AccountState.NEEDS_ATTENTION


def test_healthy_heartbeat_on_running_runtime_does_not_change_state(client, app):
    account = create_account(client, "running-heartbeat-idempotent")
    assert app.state.executor.execute_next()
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
    assert app.state.executor.execute_next()
    with app.state.db.transaction(immediate=True) as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.verified_at = utcnow()
    assert send_heartbeat(
        client, account["runtime_id"], token, heartbeat_payload(account["runtime_id"])
    ).status_code == 200
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).state == RuntimeState.RUNNING


def test_healthy_heartbeat_for_wrong_runtime_identity_does_not_recover(client, app):
    account, token = stale_verified_runtime(client, app, "stale-wrong-identity")
    payload = heartbeat_payload(account["runtime_id"] + 1000)

    assert send_heartbeat(client, account["runtime_id"], token, payload).status_code == 404
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).state == RuntimeState.STALE


def test_bad_runtime_token_does_not_change_heartbeat(client, app):
    account = create_account(client, "bad-token")
    assert app.state.executor.execute_next()
    response = client.post(
        "/api/v1/runtime-agent/heartbeat",
        headers={"Authorization": "Bearer wrong"},
        json=heartbeat_payload(account["runtime_id"]),
    )
    assert response.status_code == 401
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).last_heartbeat_at is None


def test_runtime_token_isolation(client, app):
    first = create_account(client, "token-a")
    second = create_account(client, "token-b")
    assert app.state.executor.execute_next()
    assert app.state.executor.execute_next()
    token = client.post(f"/api/v1/runtimes/{first['runtime_id']}/token/rotate").json()["token"]
    response = client.post(
        "/api/v1/runtime-agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json=heartbeat_payload(second["runtime_id"]),
    )
    assert response.status_code == 401
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, second["runtime_id"]).last_heartbeat_at is None


def test_protocol_mismatch_rejected_before_mutation(client, app):
    account = create_account(client, "old-agent")
    assert app.state.executor.execute_next()
    token = client.post(f"/api/v1/runtimes/{account['runtime_id']}/token/rotate").json()["token"]
    payload = heartbeat_payload(account["runtime_id"])
    payload["protocol_version"] = 999
    response = client.post(
        "/api/v1/runtime-agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
    )
    assert response.status_code == 409
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).last_heartbeat_at is None


def test_agent_details_are_bounded_and_secrets_redacted(client, app):
    account = create_account(client, "safe-agent-details")
    assert app.state.executor.execute_next()
    token = client.post(f"/api/v1/runtimes/{account['runtime_id']}/token/rotate").json()["token"]
    assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
    assert app.state.executor.execute_next()
    payload = heartbeat_payload(account["runtime_id"])
    payload["details"] = {"token": "must-not-store", "nested": {"password_hint": "nope"}, "noise": "x" * 1000}
    assert client.post("/api/v1/runtime-agent/heartbeat", headers={"Authorization": f"Bearer {token}"}, json=payload).status_code == 200
    with app.state.db.session() as session:
        details = session.get(WorkerStatus, account["runtime_id"]).details
        diagnostics = details["diagnostics"]
        assert diagnostics["token"] == "[REDACTED]"
        assert diagnostics["nested"]["password_hint"] == "[REDACTED]"
        assert len(diagnostics["noise"]) == 500


def test_steam_ready_ends_login_state_without_claiming_runtime_verified(client, app):
    account = create_account(client, "steam-ready-transition")
    assert app.state.executor.execute_next()
    token = client.post(
        f"/api/v1/runtimes/{account['runtime_id']}/token/rotate"
    ).json()["token"]
    assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
    assert app.state.executor.execute_next()
    payload = heartbeat_payload(account["runtime_id"])
    payload.update(phase="STEAM_READY", dst_running=False)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        "/api/v1/runtime-agent/heartbeat", headers=headers, json=payload
    )
    assert response.status_code == 200
    assert response.json()["runtime_verified"] is False
    with app.state.db.session() as session:
        assert session.get(Account, account["id"]).status == AccountState.VERIFYING
        assert session.get(RuntimeInstance, account["runtime_id"]).verified_at is None
    # Repeated authenticated heartbeats must be idempotent.
    assert client.post(
        "/api/v1/runtime-agent/heartbeat", headers=headers, json=payload
    ).status_code == 200
    with app.state.db.session() as session:
        assert session.get(Account, account["id"]).status == AccountState.VERIFYING
    # A failed verification job can put the database record in ERROR while the
    # same authenticated runtime remains live. Fresh heartbeats and VERIFY must
    # recover that record without restarting its container.
    with app.state.db.transaction(immediate=True) as session:
        transition_runtime(
            session.get(RuntimeInstance, account["runtime_id"]), RuntimeState.ERROR
        )
        transition_account(session.get(Account, account["id"]), AccountState.ERROR)
    payload.update(phase="GAME_READY", dst_running=True)
    assert client.post(
        "/api/v1/runtime-agent/heartbeat", headers=headers, json=payload
    ).status_code == 200
    assert client.post(f"/api/v1/accounts/{account['id']}/verify").status_code == 202
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        assert session.get(Account, account["id"]).status == AccountState.RUNNING
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert runtime.state == RuntimeState.RUNNING
        assert runtime.verified_at is not None


def test_stopped_runtime_rejects_even_valid_heartbeat(client, app):
    account = create_account(client, "stopped-agent")
    assert app.state.executor.execute_next()
    token = client.post(f"/api/v1/runtimes/{account['runtime_id']}/token/rotate").json()["token"]
    response = client.post(
        "/api/v1/runtime-agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json=heartbeat_payload(account["runtime_id"]),
    )
    assert response.status_code == 401
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).last_heartbeat_at is None


def test_node_heartbeat_records_resources(client, app):
    token = client.post(f"/api/v1/nodes/{app.state.node_id}/token/rotate").json()["token"]
    response = client.post(
        "/api/v1/node-agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "node_id": app.state.node_id,
            "agent_version": "node-test-1",
            "protocol_version": 1,
            "incus_available": True,
            "active_runtime_count": 0,
            "resources": {
                "cpu_percent": 12.5,
                "ram_used_bytes": 100,
                "ram_total_bytes": 1000,
                "gpu_present": False,
            },
        },
    )
    assert response.status_code == 200, response.text
    node = client.get("/api/v1/nodes").json()[0]
    assert node["status"] == "ONLINE"
    assert node["resources"]["cpu_percent"] == 12.5


def test_drain_and_maintenance_block_new_starts(client, app):
    account = create_account(client, "drained")
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        stored = session.get(Account, account["id"])
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        stored.status = AccountState.READY
        runtime.state = RuntimeState.READY
    drained = client.post(f"/api/v1/nodes/{app.state.node_id}/drain")
    assert drained.status_code == 200
    assert drained.json()["draining"] is True
    assert client.post(f"/api/v1/accounts/{account['id']}/start").status_code == 409
    maintenance = client.post(f"/api/v1/nodes/{app.state.node_id}/maintenance")
    assert maintenance.status_code == 200
    assert maintenance.json()["maintenance"] is True


def test_dead_node_marks_runtime_stale_without_destroying(client, app, settings):
    account = create_account(client, "dead-node")
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        node = session.get(Node, app.state.node_id)
        node.provider = "incus"
        node.status = NodeStatus.ONLINE
        node.last_heartbeat_at = utcnow() - timedelta(seconds=settings.node_stale_seconds + 1)
        stored = session.get(Account, account["id"])
        stored.status = AccountState.RUNNING
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = RuntimeState.RUNNING
        runtime.updated_at = utcnow()
    result = app.state.watchdog.tick()
    assert result["nodes_offline"] == 1
    with app.state.db.session() as session:
        node = session.get(Node, app.state.node_id)
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert node.status == NodeStatus.OFFLINE
        assert runtime.state == RuntimeState.STALE
        assert runtime.active is True


def test_stale_runtime_agent_marks_needs_attention(client, app, settings):
    account = create_account(client, "stale-agent")
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        stored = session.get(Account, account["id"])
        stored.status = AccountState.RUNNING
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = RuntimeState.RUNNING
        runtime.last_heartbeat_at = utcnow() - timedelta(
            seconds=settings.watchdog_stale_seconds + 1
        )
    result = app.state.watchdog.tick()
    assert result["runtimes_stale"] == 1
    with app.state.db.session() as session:
        stored = session.get(Account, account["id"])
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert stored.status == AccountState.NEEDS_ATTENTION
        assert runtime.state == RuntimeState.STALE
        assert runtime.last_error_code == "AGENT_STALE"


def test_new_run_gets_first_heartbeat_window_after_old_heartbeat(client, app, settings):
    account = create_account(client, "restart-heartbeat-window")
    assert app.state.executor.execute_next()
    with app.state.db.transaction(immediate=True) as session:
        stored = session.get(Account, account["id"])
        stored.status = AccountState.RUNNING
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = RuntimeState.RUNNING
        runtime.last_heartbeat_at = utcnow() - timedelta(
            seconds=settings.watchdog_stale_seconds + 1
        )
        session.add(
            Run(
                account_id=stored.id,
                runtime_id=runtime.id,
                node_id=runtime.node_id,
                started_at=utcnow(),
                start_reason="setup",
            )
        )
    result = app.state.watchdog.tick()
    assert result["runtimes_stale"] == 0
    with app.state.db.session() as session:
        assert session.get(RuntimeInstance, account["runtime_id"]).state == RuntimeState.RUNNING
