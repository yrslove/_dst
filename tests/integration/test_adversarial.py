import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import select

from app.models import (
    Account,
    Job,
    JobStatus,
    RuntimeInstance,
    RuntimeLease,
    WorkerStatus,
    utcnow,
)
from app.providers.base import ProviderUnavailable
from tests.helpers import create_account, make_ready


def retry_now(app, job_id):
    with app.state.db.session() as session:
        session.get(Job, job_id).run_after = utcnow()


def test_conflicting_commands_and_disable_are_rejected(client, app):
    account = make_ready(client, app, "conflict")
    assert client.post(f"/api/v1/accounts/{account['id']}/start").status_code == 202
    assert client.post(f"/api/v1/accounts/{account['id']}/stop").status_code == 409
    assert client.post(f"/api/v1/accounts/{account['id']}/disable").status_code == 409


def test_replayed_start_key_does_not_change_stopped_desire(client, app):
    account = make_ready(client, app, "replay")
    path = f"/api/v1/accounts/{account['id']}"
    headers = {"Idempotency-Key": "one-start"}
    first = client.post(path + "/start", headers=headers).json()
    app.state.executor.execute_next()
    client.post(path + "/stop")
    app.state.executor.execute_next()
    replay = client.post(path + "/start", headers=headers).json()
    assert replay["job"]["id"] == first["job"]["id"]
    assert client.get(path).json()["desired_state"] == "STOPPED"


def test_old_attempt_cannot_complete_new_attempt(app, client):
    create_account(client, "fenced-attempt")
    old = app.state.jobs.claim("worker")
    with app.state.db.session() as session:
        session.get(Job, old.id).leased_until = utcnow() - timedelta(seconds=1)
    new = app.state.jobs.claim("worker")
    assert new.id == old.id and new.lease_owner != old.lease_owner
    app.state.jobs.succeed(old.id, old.lease_owner)
    assert app.state.jobs.get(old.id).status == JobStatus.LEASED
    app.state.executor.execute(old)
    assert app.state.provider.calls["ensure"] == 0
    app.state.executor.execute(new)
    assert app.state.jobs.get(old.id).status == JobStatus.SUCCEEDED


def test_long_provider_call_renews_job_lease(client, app, monkeypatch):
    account = make_ready(client, app, "long-start")
    app.state.jobs.lease_seconds = 1
    entered = threading.Event()
    original = app.state.provider.start

    def slow(*args, **kwargs):
        entered.set()
        time.sleep(1.6)
        return original(*args, **kwargs)

    monkeypatch.setattr(app.state.provider, "start", slow)
    job_id = client.post(f"/api/v1/accounts/{account['id']}/start").json()["job"]["id"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(app.state.executor.execute_next)
        assert entered.wait(5)
        time.sleep(1.1)
        assert app.state.jobs.claim("competitor") is None
        assert running.result(timeout=10)
    assert app.state.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert app.state.jobs.get(job_id).attempt_count == 1


def test_rebuild_retry_resumes_same_generation(client, app):
    account = make_ready(client, app, "rebuild-retry")
    client.post(f"/api/v1/accounts/{account['id']}/start")
    app.state.executor.execute_next()
    app.state.provider.inject("ensure", ProviderUnavailable("temporary outage"))
    job_id = client.post(f"/api/v1/accounts/{account['id']}/rebuild", json={}).json()["job"]["id"]
    app.state.executor.execute_next()
    assert app.state.jobs.get(job_id).status == JobStatus.RETRY
    first_id = client.get(f"/api/v1/accounts/{account['id']}").json()["runtime_id"]
    retry_now(app, job_id)
    app.state.executor.execute_next()
    assert app.state.jobs.get(job_id).status == JobStatus.SUCCEEDED
    current = client.get(f"/api/v1/accounts/{account['id']}").json()
    assert current["runtime_id"] == first_id and current["runtime_generation"] == 2
    assert current["verified_at"] is None
    assert len(client.get(f"/api/v1/accounts/{account['id']}/runtime-history").json()) == 2
    assert app.state.provider.states[account["external_id"]] == "STOPPED"


def test_scheduler_does_not_reset_exhausted_retry_budget(client, app):
    account = make_ready(client, app, "exhausted")
    app.state.provider.inject("start", ProviderUnavailable("offline"), times=3)
    job_id = client.post(f"/api/v1/accounts/{account['id']}/start").json()["job"]["id"]
    for _ in range(3):
        app.state.executor.execute_next()
        retry_now(app, job_id)
    assert app.state.jobs.get(job_id).status == JobStatus.FAILED
    with app.state.db.session() as session:
        session.get(RuntimeInstance, account["runtime_id"]).state = "STOPPED"
        session.get(Account, account["id"]).status = "READY"
    for _ in range(5):
        assert app.state.scheduler.tick() == 0


def test_stale_slot_holds_capacity_until_provider_confirms_stop(client, app):
    account = make_ready(client, app, "quarantined")
    client.post(f"/api/v1/accounts/{account['id']}/start")
    app.state.executor.execute_next()
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, account["runtime_id"])
        runtime.state = "STALE"
        lease = session.scalar(select(RuntimeLease).where(RuntimeLease.runtime_id == runtime.id, RuntimeLease.released_at.is_(None)))
        lease.expires_at = utcnow() - timedelta(seconds=1)
    assert app.state.leases.recover_expired() == 0
    app.state.reconciler.tick()
    assert app.state.leases.active_count(app.state.node_id) == 1
    assert client.post(f"/api/v1/nodes/{app.state.node_id}/maintenance").status_code == 409
    assert client.get(f"/api/v1/accounts/{account['id']}").json()["state"] == "STALE"


def test_setup_without_fresh_game_readiness_cannot_verify(client, app):
    account = create_account(client, "fresh-verification")
    app.state.executor.execute_next()
    path = f"/api/v1/accounts/{account['id']}"
    assert client.post(path + "/setup").status_code == 202
    app.state.executor.execute_next()
    with app.state.db.session() as session:
        session.add(WorkerStatus(runtime_id=account["runtime_id"], phase="GAME_READY", healthy=True,
                                 steam_running=True, dst_running=True,
                                 updated_at=utcnow() - timedelta(hours=1)))
    job = client.post(path + "/verify").json()["job"]
    app.state.executor.execute_next()
    assert app.state.jobs.get(job["id"]).status == JobStatus.FAILED
    assert client.get(path).json()["verified_at"] is None
    client.post(path + "/stop")
    app.state.executor.execute_next()
    assert client.post(path + "/start").status_code == 409
