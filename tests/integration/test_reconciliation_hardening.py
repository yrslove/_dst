from app.models import Account, AccountState, JobKind, RuntimeInstance, RuntimeState
from tests.helpers import create_account, make_ready


def test_unexpected_provider_running_state_is_quarantined(client, app):
    account = create_account(client, "unexpected-running")
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        external_id = runtime.external_id
        assert runtime.state == RuntimeState.NEEDS_LOGIN
    app.state.provider.states[external_id] = RuntimeState.RUNNING

    assert app.state.reconciler.tick() == 1

    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        stored_account = session.get(Account, account["id"])
        assert runtime.state == RuntimeState.STALE
        assert runtime.last_error_code == "UNKNOWN"
        assert stored_account.status == AccountState.NEEDS_ATTENTION


def test_token_rotation_defers_bootstrap_until_stopped_runtime_starts(client, app):
    account = make_ready(client, app, "rotate-rebootstrap")
    runtime_id = account["runtime_id"]
    execute_calls = app.state.provider.calls["execute"]

    rotated = client.post(f"/api/v1/runtimes/{runtime_id}/token/rotate")

    assert rotated.status_code == 200
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, runtime_id)
        assert runtime.bootstrap_phase == "AGENT_FILES_INSTALLED"
        assert runtime.bootstrap_completed_at is None
    assert app.state.executor.execute_next() is False

    assert client.post(f"/api/v1/accounts/{account['id']}/start").status_code == 202
    assert app.state.executor.execute_next()

    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, runtime_id)
        assert runtime.state == RuntimeState.RUNNING
        assert runtime.bootstrap_phase == "BOOTSTRAP_COMPLETE"
        assert runtime.bootstrap_completed_at is not None
    assert app.state.provider.calls["execute"] > execute_calls


def test_legacy_bootstrap_job_does_not_exec_into_stopped_runtime(client, app):
    account = make_ready(client, app, "legacy-bootstrap")
    execute_calls = app.state.provider.calls["execute"]
    job = app.state.jobs.enqueue(
        kind=JobKind.BOOTSTRAP_RUNTIME,
        account_id=account["id"],
        runtime_id=account["runtime_id"],
        node_id=account["node_id"],
        request_id="legacy-bootstrap",
        idempotency_key=f"legacy-bootstrap:{account['runtime_id']}",
    )

    assert app.state.executor.execute_next()

    assert app.state.jobs.get(job.id).status == "SUCCEEDED"
    assert app.state.provider.calls["execute"] == execute_calls
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        assert runtime.state == "STOPPED"
