from datetime import timedelta

from sqlalchemy import select

from app.models import (
    Account,
    AccountState,
    Job,
    JobStatus,
    RuntimeInstance,
    RuntimeLease,
    RuntimeState,
    utcnow,
)
from tests.helpers import create_account, make_ready


def test_job_is_durable_before_executor_runs(client, app):
    account = create_account(client, "durable")
    jobs = client.get("/api/v1/jobs").json()
    assert jobs[0]["status"] == "PENDING"
    assert account["state"] == "NEW"
    assert app.state.executor.execute_next()
    assert client.get(f"/api/v1/jobs/{jobs[0]['id']}").json()["status"] == "SUCCEEDED"


def test_duplicate_start_returns_one_job(client, app):
    account = make_ready(client, app, "idempotent")
    first = client.post(f"/api/v1/accounts/{account['id']}/start")
    second = client.post(f"/api/v1/accounts/{account['id']}/start")
    assert first.status_code == second.status_code == 202
    assert first.json()["job"]["id"] == second.json()["job"]["id"]


def test_start_stop_restart_runtime(client, app):
    account = make_ready(client, app, "lifecycle")
    assert client.post(f"/api/v1/accounts/{account['id']}/start").status_code == 202
    assert app.state.executor.execute_next()
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["state"] == "RUNNING"
    assert client.post(f"/api/v1/accounts/{account['id']}/restart").status_code == 202
    first_restart_job = client.get("/api/v1/jobs").json()[0]["id"]
    assert app.state.executor.execute_next()
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["state"] == "RUNNING"
    second_restart = client.post(f"/api/v1/accounts/{account['id']}/restart")
    assert second_restart.status_code == 202
    assert second_restart.json()["job"]["id"] != first_restart_job
    assert app.state.executor.execute_next()
    assert client.post(f"/api/v1/accounts/{account['id']}/stop").status_code == 202
    assert app.state.executor.execute_next()
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["state"] == "STOPPED"
    runs = client.get(f"/api/v1/accounts/{account['id']}/runs").json()
    assert len(runs) >= 1
    assert runs[0]["result"] == "STOPPED"
    assert runs[0]["duration_seconds"] is not None


def test_provider_timeout_retries_without_duplicate_provision(client, app):
    from app.providers.base import InstanceTimeout

    account = make_ready(client, app, "timeout")
    app.state.provider.inject("start", InstanceTimeout("response lost"), times=1)
    queued = client.post(f"/api/v1/accounts/{account['id']}/start")
    assert queued.status_code == 202
    assert app.state.executor.execute_next()
    failed_once = client.get(f"/api/v1/jobs/{queued.json()['job']['id']}").json()
    assert failed_once["status"] == "RETRY"
    with app.state.db.session() as session:
        stored = session.get(Job, failed_once["id"])
        stored.run_after = utcnow()
    assert app.state.executor.execute_next()
    completed = client.get(f"/api/v1/jobs/{failed_once['id']}").json()
    assert completed["status"] == "SUCCEEDED", (
        completed["last_error_code"],
        completed["last_error"],
    )
    assert app.state.provider.calls["ensure"] == 1


def test_bounded_provider_failure_becomes_failed_job(client, app):
    from app.providers.base import ProviderUnavailable

    account = make_ready(client, app, "bounded-failure")
    app.state.provider.inject("start", ProviderUnavailable("node down"), times=3)
    queued = client.post(f"/api/v1/accounts/{account['id']}/start")
    job_id = queued.json()["job"]["id"]
    for _ in range(3):
        assert app.state.executor.execute_next(), client.get(
            f"/api/v1/jobs/{job_id}"
        ).json()
        with app.state.db.session() as session:
            stored = session.get(Job, job_id)
            if stored.status == JobStatus.RETRY:
                stored.run_after = utcnow()
    completed = client.get(f"/api/v1/jobs/{job_id}").json()
    assert completed["status"] == "FAILED"
    assert completed["attempt_count"] == 3


def test_scheduler_skips_disabled_account(client, app):
    account = create_account(client, "disabled-scheduler")
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        stored = session.get(Account, account["id"])
        stored.enabled = False
        stored.status = AccountState.DISABLED
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = RuntimeState.READY
        runtime.desired_state = "RUNNING"
    before = len(client.get("/api/v1/jobs").json())
    assert app.state.scheduler.tick() == 0
    after = len(client.get("/api/v1/jobs").json())
    assert after == before


def test_rebuild_preserves_account_and_increments_generation(client, app):
    account = make_ready(client, app, "rebuild")
    queued = client.post(
        f"/api/v1/accounts/{account['id']}/rebuild",
        json={"image_version": "dst-base-v2"},
    )
    assert queued.status_code == 202
    assert app.state.executor.execute_next()
    current = client.get(f"/api/v1/accounts/{account['id']}").json()
    history = client.get(f"/api/v1/accounts/{account['id']}/runtime-history").json()
    assert current["id"] == account["id"]
    assert current["runtime_generation"] == 2
    assert current["image_version"] == "dst-base-v2"
    assert current["state"] == "NEEDS_LOGIN"
    assert len(history) == 2
    assert sum(1 for item in history if item["active"]) == 1


def test_expired_job_lease_is_reclaimed(client, app):
    create_account(client, "lease-job")
    job = app.state.jobs.claim("dead-worker")
    assert job is not None
    with app.state.db.session() as session:
        stored = session.get(Job, job.id)
        stored.status = JobStatus.RUNNING
        stored.leased_until = utcnow() - timedelta(seconds=1)
    recovered = app.state.jobs.claim("new-worker")
    assert recovered is not None
    assert recovered.id == job.id
    assert recovered.attempt_count == 2


def test_expired_slot_lease_is_recovered(client, app):
    account = make_ready(client, app, "slot-recovery")
    start = client.post(f"/api/v1/accounts/{account['id']}/start")
    assert start.status_code == 202
    assert app.state.executor.execute_next()
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = "STALE"
        lease = session.scalar(
            select(RuntimeLease).where(
                RuntimeLease.runtime_id == account["runtime_id"],
                RuntimeLease.released_at.is_(None),
            )
        )
        lease.expires_at = utcnow() - timedelta(seconds=1)
    assert app.state.leases.recover_expired() == 0
    assert app.state.leases.active_count(app.state.node_id) == 1
    app.state.provider.states[account["external_id"]] = "STOPPED"
    app.state.reconciler.tick()
    assert app.state.leases.active_count(app.state.node_id) == 0
