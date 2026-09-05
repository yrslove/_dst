from __future__ import annotations

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    base_url = os.getenv("TEST_DATABASE_URL")
    url = f"sqlite:///{(tmp_path / 'test.sqlite3').as_posix()}"
    engine = None
    schema = None
    if base_url:
        if make_url(base_url).get_backend_name() != "postgresql":
            raise ValueError("TEST_DATABASE_URL must be PostgreSQL")
        # Every test owns a freshly generated schema. Never downgrade an existing DB.
        schema = "dst_test_" + uuid.uuid4().hex
        engine = create_engine(base_url)
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = make_url(base_url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    configured = Settings(
        environment="test",
        database_url=url,
        runtime_provider="mock",
        secret_key=Fernet.generate_key().decode(),
        admin_username="admin",
        admin_password="test-admin-password",
        default_node_name="test-node",
        default_node_max_active_slots=4,
        scheduler_interval_seconds=999,
        watchdog_interval_seconds=999,
        background_workers=False,
    )
    try:
        yield configured
    finally:
        if engine is not None:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            engine.dispose()


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
def raw_client(app):
    with TestClient(app) as value:
        yield value


@pytest.fixture
def client(raw_client):
    response = raw_client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert response.status_code == 200, response.text
    raw_client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return raw_client
