from dataclasses import replace
from threading import Thread

from fastapi.testclient import TestClient
from sqlalchemy import select
from websockets.sync.server import serve

from app.main import create_app
from app.models import RuntimeViewSession
from app.providers.view import MockRuntimeViewProvider, ViewBackendUnavailable
from tests.helpers import create_account


class PendingCleanupViewProvider(MockRuntimeViewProvider):
    def reserve_session(self, runtime, display):
        return "mock-view-pending-cleanup"

    def create_session(self, runtime, display, *, backend_session_id=None):
        assert backend_session_id == "mock-view-pending-cleanup"
        raise ViewBackendUnavailable("setup outcome requires cleanup")


def test_view_provider_disabled_fails_closed(client, app):
    account = create_account(client, "view-disabled")
    response = client.post(f"/api/v1/runtimes/{account['runtime_id']}/view-sessions")
    assert response.status_code == 503
    assert "access_token" not in response.text


def test_view_websocket_negotiates_binary_on_both_legs(client, app, monkeypatch):
    upstream_subprotocols = []

    def echo(connection):
        upstream_subprotocols.append(connection.subprotocol)
        connection.send(connection.recv())

    with serve(echo, "127.0.0.1", 0, subprotocols=["binary"]) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.socket.getsockname()[:2]
        monkeypatch.setattr(
            app.state.views,
            "resolve_upstream",
            lambda _session_id, _token: (host, port, 10.0),
        )
        client.cookies.set("dst_view_access", "test-view-token")

        with client.websocket_connect(
            "/api/v1/view-sessions/1/transport/", subprotocols=["binary"]
        ) as websocket:
            assert websocket.accepted_subprotocol == "binary"
            websocket.send_bytes(b"frame-probe")
            assert websocket.receive_bytes() == b"frame-probe"

        server.shutdown()
        thread.join(timeout=2)
    assert upstream_subprotocols == ["binary"]


def test_view_session_is_short_lived_and_token_protected(settings):
    app = create_app(replace(settings, runtime_view_provider="mock"))
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        account = create_account(client, "view-mock")
        assert app.state.executor.execute_next()
        assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
        assert app.state.executor.execute_next()
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


def test_failed_view_setup_retains_backend_identity_until_cleanup(settings):
    app = create_app(replace(settings, runtime_view_provider="mock"))
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        account = create_account(client, "view-cleanup")
        assert app.state.executor.execute_next()
        assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
        assert app.state.executor.execute_next()
        app.state.views.provider = PendingCleanupViewProvider()

        response = client.post(
            f"/api/v1/runtimes/{account['runtime_id']}/view-sessions"
        )

        assert response.status_code == 503
        with app.state.db.session() as session:
            record = session.scalar(
                select(RuntimeViewSession).order_by(RuntimeViewSession.id.desc())
            )
            assert record.status == "CLOSING"
            assert record.backend_session_id == "mock-view-pending-cleanup"
        assert app.state.views.cleanup_expired_sessions() == 1
        with app.state.db.session() as session:
            record = session.scalar(
                select(RuntimeViewSession).order_by(RuntimeViewSession.id.desc())
            )
            assert record.status == "CLOSED"
            assert record.closed_at is not None


def test_pending_view_cleanup_precedes_replacement_and_old_close_is_idempotent(
    settings,
):
    app = create_app(replace(settings, runtime_view_provider="mock"))
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        account = create_account(client, "view-replacement")
        assert app.state.executor.execute_next()
        assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
        assert app.state.executor.execute_next()

        first = client.post(
            f"/api/v1/runtimes/{account['runtime_id']}/view-sessions"
        ).json()
        with app.state.db.transaction(immediate=True) as session:
            record = session.get(RuntimeViewSession, first["id"])
            record.status = "CLOSING"

        second_response = client.post(
            f"/api/v1/runtimes/{account['runtime_id']}/view-sessions"
        )
        assert second_response.status_code == 201
        second = second_response.json()
        with app.state.db.session() as session:
            old_record = session.get(RuntimeViewSession, first["id"])
            new_record = session.get(RuntimeViewSession, second["id"])
            assert old_record.status == "CLOSED"
            assert old_record.closed_at is not None
            new_backend_id = new_record.backend_session_id
        assert app.state.views.provider.sessions == {new_backend_id}

        assert client.delete(f"/api/v1/view-sessions/{first['id']}").status_code == 200
        assert app.state.views.provider.sessions == {new_backend_id}


def test_view_is_revoked_and_cleaned_when_runtime_stops(settings):
    app = create_app(replace(settings, runtime_view_provider="mock"))
    with TestClient(app) as client:
        login = client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "test-admin-password"},
        )
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        account = create_account(client, "view-runtime-stop")
        assert app.state.executor.execute_next()
        assert client.post(f"/api/v1/accounts/{account['id']}/setup").status_code == 202
        assert app.state.executor.execute_next()
        created = client.post(
            f"/api/v1/runtimes/{account['runtime_id']}/view-sessions"
        ).json()

        assert client.post(f"/api/v1/accounts/{account['id']}/stop").status_code == 202
        assert app.state.executor.execute_next()
        client.headers.pop("X-CSRF-Token")
        response = client.get(
            f"/api/v1/view-sessions/{created['id']}",
            headers={"Authorization": f"Bearer {created['access_token']}"},
        )

        assert response.status_code == 404
        assert not app.state.views.provider.sessions
        with app.state.db.session() as session:
            record = session.get(RuntimeViewSession, created["id"])
            assert record.status == "CLOSED"
            assert record.closed_at is not None
