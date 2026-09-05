from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.config import ConfigurationError, Settings


class SecretDecryptionError(RuntimeError):
    pass


class SecretsService:
    def __init__(self, settings: Settings):
        if settings.secret_key:
            key = settings.secret_key.encode("ascii")
        elif settings.environment == "development":
            path = Path(".data/dev_master.key")
            if path.exists():
                key = path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(key)
        elif settings.environment == "test":
            key = Fernet.generate_key()
        else:
            raise ConfigurationError(
                "DST_FARM_SECRET_KEY is required outside development/test"
            )
        try:
            self._fernet = Fernet(key)
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                "DST_FARM_SECRET_KEY is not a valid Fernet key"
            ) from exc

    def encrypt(self, value: str | None) -> str | None:
        return (
            self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
            if value
            else None
        )

    def decrypt(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretDecryptionError(
                "encrypted value cannot be decrypted with the configured key"
            ) from exc
