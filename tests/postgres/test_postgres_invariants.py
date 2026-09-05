import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from alembic import command
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import alembic_config
from app.main import create_app
from app.models import Account, AccountState, RuntimeInstance, RuntimeState
from tests.helpers import create_account

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL is required for PostgreSQL integration",
)


def test_postgres_skip_locked_and_slot_invariant(settings):
    url = settings.database_url
    config = alembic_config(url)
    command.upgrade(config, "head")
    settings = Settings(
        environment="test",
        database_url=url,
        runtime_provider="mock",
        secret_key=Fernet.generate_key().decode(),
        admin_password="test-admin-password",
        default_node_name="postgres-node",
        default_node_max_active_slots=4,
        background_workers=False,
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "test-admin-password"},
            )
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            accounts = []
            for index in range(20):
                account = create_account(client, f"postgres-race-{index}")
                assert app.state.executor.execute_next()
                with app.state.db.session() as session:
                    session.get(Account, account["id"]).status = AccountState.READY
                    session.get(RuntimeInstance, account["runtime_id"]).state = RuntimeState.READY
                    from app.models import utcnow
                    session.get(RuntimeInstance, account["runtime_id"]).verified_at = utcnow()
                accounts.append(account)

            with ThreadPoolExecutor(max_workers=20) as pool:
                list(
                    pool.map(
                        lambda account: app.state.accounts.command(
                            account["id"],
                            "START_RUNTIME",
                            actor="postgres-test",
                            request_id=f"pg-{account['id']}",
                        ),
                        accounts,
                    )
                )
            with ThreadPoolExecutor(max_workers=20) as pool:
                list(pool.map(lambda _: app.state.executor.execute_next(), range(20)))
            with app.state.db.session() as session:
                active = (
                    session.query(RuntimeInstance)
                    .filter(RuntimeInstance.state.in_(["STARTING", "RUNNING"]))
                    .count()
                )
            assert active == 4
            assert app.state.leases.active_count(app.state.node_id) == 4
            assert list(app.state.provider.states.values()).count("RUNNING") == 4
    finally:
        app.state.db.dispose()
