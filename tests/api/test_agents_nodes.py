from datetime import timedelta

from app.models import (
    Account,
    AccountState,
    Node,
    NodeStatus,
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
