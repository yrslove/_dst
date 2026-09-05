from app.models import AccountSecret
from tests.helpers import create_account


def test_unauthenticated_write_denied(raw_client):
    response = raw_client.post(
        "/api/v1/accounts",
        json={"label": "a", "steam_username": "a"},
    )
    assert response.status_code == 401


def test_csrf_required(raw_client):
    login = raw_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert login.status_code == 200
    response = raw_client.post(
        "/api/v1/accounts",
        json={"label": "a", "steam_username": "a"},
    )
    assert response.status_code == 403


def test_admin_session_cookie_and_logout(client):
    cookie = client.cookies.get("dst_admin_session")
    assert cookie
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 200
    assert client.get("/api/v1/accounts").status_code == 401


def test_create_duplicate_and_secret_not_exposed(client, app):
    account = create_account(client, "same")
    assert account["state"] == "NEW"
    duplicate = client.post(
        "/api/v1/accounts",
        json={"label": "other", "steam_username": "same"},
    )
    assert duplicate.status_code == 409
    body = client.get("/api/v1/accounts").text
    assert "secret-value" not in body
    assert "steam_password" not in body
    with app.state.db.session() as session:
        stored = session.get(AccountSecret, account["id"])
        assert stored.steam_password_enc
        assert stored.steam_password_enc != "secret-value"


def test_disable_requires_stopped_runtime(client, app):
    from tests.helpers import make_ready

    account = make_ready(client, app, "disable-me")
    start = client.post(f"/api/v1/accounts/{account['id']}/start")
    assert start.status_code == 202
    assert app.state.executor.execute_next()
    assert client.post(f"/api/v1/accounts/{account['id']}/disable").status_code == 409
    assert client.post(f"/api/v1/accounts/{account['id']}/stop").status_code == 202
    assert app.state.executor.execute_next()
    assert client.post(f"/api/v1/accounts/{account['id']}/disable").status_code == 200


def test_request_id_is_returned(client):
    response = client.get("/api/v1/accounts", headers={"X-Request-ID": "known-request"})
    assert response.headers["X-Request-ID"] == "known-request"


def test_invalid_request_id_is_replaced_and_api_is_not_cacheable(client):
    response = client.get("/api/v1/accounts", headers={"X-Request-ID": "bad\nvalue"})
    assert response.headers["X-Request-ID"] != "bad\nvalue"
    assert response.headers["Cache-Control"] == "no-store"
