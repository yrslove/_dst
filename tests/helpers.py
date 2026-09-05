from __future__ import annotations


def create_account(client, username: str) -> dict:
    response = client.post(
        "/api/v1/accounts",
        json={
            "label": username,
            "steam_username": username,
            "steam_password": "secret-value",
        },
    )
    assert response.status_code == 202, response.text
    return response.json()["account"]


def drain_jobs(app, limit: int = 100) -> int:
    count = 0
    while count < limit and app.state.executor.execute_next():
        count += 1
    return count


def make_ready(client, app, username: str) -> dict:
    account = create_account(client, username)
    assert app.state.executor.execute_next()
    token_response = client.post(f"/api/v1/runtimes/{account['runtime_id']}/token/rotate")
    assert token_response.status_code == 200, token_response.text
    token = token_response.json()["token"]
    setup = client.post(f"/api/v1/accounts/{account['id']}/setup")
    assert setup.status_code == 202, setup.text
    assert app.state.executor.execute_next()
    heartbeat = client.post(
        "/api/v1/runtime-agent/heartbeat",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "runtime_id": account["runtime_id"],
            "phase": "GAME_READY",
            "steam_running": True,
            "dst_running": True,
            "healthy": True,
            "automation_state": "NOOP",
            "details": {},
            "agent_version": "test-1",
            "protocol_version": 1,
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    queued = client.post(f"/api/v1/accounts/{account['id']}/verify")
    assert queued.status_code == 202, queued.text
    assert app.state.executor.execute_next()
    result = client.get(f"/api/v1/accounts/{account['id']}").json()
    assert result["verified_at"] is not None, result
    stopped = client.post(f"/api/v1/accounts/{account['id']}/stop")
    assert stopped.status_code == 202, stopped.text
    assert app.state.executor.execute_next()
    result = client.get(f"/api/v1/accounts/{account['id']}").json()
    assert result["state"] == "STOPPED", result
    return result
