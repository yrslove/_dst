from fastapi.testclient import TestClient

from app.main import create_app
from app.models import JobStatus


def test_control_plane_restart_keeps_persisted_job(settings):
    first = create_app(settings)
    with TestClient(first) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        created = client.post(
            "/api/v1/accounts",
            json={"label": "restart", "steam_username": "restart"},
        )
        job_id = created.json()["job"]["id"]
        assert created.json()["job"]["status"] == JobStatus.PENDING

    second = create_app(settings)
    with TestClient(second) as client:
        assert second.state.executor.execute_next()
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert client.get(f"/api/v1/jobs/{job_id}").json()["status"] == JobStatus.SUCCEEDED
