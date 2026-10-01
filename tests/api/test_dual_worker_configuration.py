from app.models import RuntimeInstance, RuntimeState, WorkerCommand, utcnow
from tests.helpers import create_account


def test_profile_uses_existing_command_ack_and_cannot_read_other_account_command(client, app):
    a = create_account(client, "dual-a")
    b = create_account(client, "dual-b")
    with app.state.db.transaction(immediate=True) as session:
        for account in (a, b):
            runtime = session.get(RuntimeInstance, account["runtime_id"])
            runtime.state = RuntimeState.RUNNING
            runtime.verified_at = utcnow()
    response = client.post(f"/api/v1/accounts/{a['id']}/worker/mode", json={
        "mode": "ACTIVE", "locomotion_profile": "CONTROL",
        "experiment_session_id": "a-only", "experiment_seconds": 120,
    })
    assert response.status_code == 202
    command = response.json()
    own = f"/api/v1/accounts/{a['id']}/worker/commands/{command['id']}"
    other = f"/api/v1/accounts/{b['id']}/worker/commands/{command['id']}"
    assert client.get(other).status_code == 404
    assert client.get(own).json()["status"] == "PENDING"
    with app.state.db.transaction(immediate=True) as session:
        stored = session.get(WorkerCommand, command["id"])
        assert stored.payload["locomotion_profile"] == "CONTROL"
        assert stored.payload["experiment_session_id"] == "a-only"
        stored.status, stored.result = "COMPLETED", "ACTIVE_GATE_CLOSED"
    assert client.get(own).json()["result"] == "ACTIVE_GATE_CLOSED"


def test_experiment_requires_session_identity_and_rejects_unsafe_path(client):
    account = create_account(client, "dual-invalid")
    response = client.post(f"/api/v1/accounts/{account['id']}/worker/mode", json={
        "mode": "ACTIVE", "locomotion_profile": "CONTROL",
        "experiment_session_id": "../../other-account",
    })
    assert response.status_code == 422


def test_schedule_pause_preserves_enabled_runtime_and_is_durable(client, app):
    from app.models import Account, AccountScheduleState
    account = create_account(client, 'dual-schedule-pause')
    assert app.state.executor.execute_next()
    result = client.post(f"/api/v1/accounts/{account['id']}/schedule/pause", json={})
    assert result.status_code == 200
    assert result.json()['active_jobs'] == []
    with app.state.db.session() as session:
        assert session.get(AccountScheduleState, account['id']).paused
        assert session.get(Account, account['id']).enabled
        assert session.get(RuntimeInstance, account['runtime_id']).active
