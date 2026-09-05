from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import create_app
from tests.helpers import create_account


def test_view_provider_disabled_fails_closed(client, app):
    account = create_account(client, "view-disabled")
    response = client.post(f"/api/v1/runtimes/{account['runtime_id']}/view-sessions")
    assert response.status_code == 503
    assert "access_token" not in response.text


def test_view_session_is_short_lived_and_token_protected(settings):
    app = create_app(replace(settings, runtime_view_provider="mock"))
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        account = create_account(client, "view-mock")
        created = client.post(
            f"/api/v1/runtimes/{account['runtime_id']}/view-sessions"
        )
        assert created.status_code == 201
        body = created.json()
        assert body["access_token"]
        client.headers.pop("X-CSRF-Token")
        assert client.get(f"/api/v1/view-sessions/{body['id']}").status_code == 404
        authorized = client.get(
            f"/api/v1/view-sessions/{body['id']}",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert authorized.status_code == 200
        assert "url" not in authorized.json()

