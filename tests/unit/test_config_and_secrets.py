import pytest
from cryptography.fernet import Fernet

from app.config import ConfigurationError, Settings
from app.services.secrets import SecretsService


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"database_url": "postgresql+psycopg://localhost/db"},
        {
            "database_url": "postgresql+psycopg://localhost/db",
            "secret_key": Fernet.generate_key().decode(),
        },
        {
            "database_url": "postgresql+psycopg://localhost/db",
            "secret_key": Fernet.generate_key().decode(),
            "runtime_provider": "incus",
        },
    ],
)
def test_unsafe_production_configuration_fails(overrides):
    values = {
        "environment": "production",
        "database_url": "sqlite:///bad.sqlite3",
        "runtime_provider": "mock",
        "secret_key": None,
        "admin_password": "change-me",
        "orchestrator_public_url": "http://example.test",
    }
    values.update(overrides)
    with pytest.raises(ConfigurationError):
        Settings(**values).validate()


def test_secret_roundtrip(settings):
    service = SecretsService(settings)
    encrypted = service.encrypt("pw")
    assert encrypted != "pw"
    assert service.decrypt(encrypted) == "pw"


def test_test_environment_can_use_ephemeral_key(settings):
    settings.secret_key = None
    assert SecretsService(settings).decrypt(None) is None
