from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ADMIN_PASSWORDS = {"admin", "password", "change-me", "changeme"}


class ConfigurationError(RuntimeError):
    """Raised when configuration would make the control plane unsafe."""


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class Settings:
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    database_url: str = "sqlite:///.data/dst_orchestrator.sqlite3"
    runtime_provider: str = "mock"
    secret_key: str | None = None
    admin_username: str = "admin"
    admin_password: str = "change-me"
    session_cookie_name: str = "dst_admin_session"
    session_ttl_seconds: int = 8 * 60 * 60
    debug: bool = False
    auto_migrate: bool = True
    incus_remote: str = "local"
    incus_base_instance: str = "dst-base-v1"
    incus_image: str = "images:ubuntu/24.04"
    incus_profile: str = "default"
    incus_command_timeout_seconds: int = 90
    incus_max_attempts: int = 3
    current_image_version: str = "dst-base-v1"
    current_image_verified: bool = False
    default_node_name: str = "local-node"
    default_node_max_active_slots: int = 2
    default_node_secret: str | None = None
    scheduler_interval_seconds: float = 5
    job_poll_interval_seconds: float = 1
    watchdog_interval_seconds: float = 10
    watchdog_stale_seconds: int = 60
    node_stale_seconds: int = 45
    job_lease_seconds: int = 30
    slot_lease_seconds: int = 120
    retry_max_attempts: int = 3
    retry_base_seconds: int = 2
    resource_sample_seconds: int = 60
    orchestrator_public_url: str = "http://127.0.0.1:8080"
    agent_protocol_version: int = 1
    app_version: str = "1.0.0"
    metrics_enabled: bool = True
    background_workers: bool = True
    runtime_view_provider: str = "disabled"
    view_session_ttl_seconds: int = 300
    runtime_display: str = ":99"
    runtime_worker_plugin: str = "dst"
    scheduler_leader_lease_seconds: int = 15

    @classmethod
    def from_env(cls) -> Settings:
        legacy_db = os.getenv("DST_FARM_DB")
        database_url = os.getenv("DATABASE_URL")
        if not database_url and legacy_db:
            database_url = f"sqlite:///{Path(legacy_db).as_posix()}"
        return cls(
            environment=os.getenv("ENVIRONMENT", "development").lower(),
            host=os.getenv("APP_HOST", "127.0.0.1"),
            port=int(os.getenv("APP_PORT", "8080")),
            database_url=database_url or "sqlite:///.data/dst_orchestrator.sqlite3",
            runtime_provider=os.getenv("RUNTIME_PROVIDER", "mock").lower(),
            secret_key=os.getenv("DST_FARM_SECRET_KEY") or None,
            admin_username=os.getenv("ADMIN_USERNAME", "admin"),
            admin_password=os.getenv("ADMIN_PASSWORD", "change-me"),
            session_cookie_name=os.getenv("SESSION_COOKIE_NAME", "dst_admin_session"),
            session_ttl_seconds=int(os.getenv("SESSION_TTL_SECONDS", str(8 * 60 * 60))),
            debug=_bool("DEBUG"),
            auto_migrate=_bool("AUTO_MIGRATE", True),
            incus_remote=os.getenv("INCUS_REMOTE", "local"),
            incus_base_instance=os.getenv("INCUS_BASE_INSTANCE", "dst-base-v1"),
            incus_image=os.getenv("INCUS_IMAGE", "images:ubuntu/24.04"),
            incus_profile=os.getenv("INCUS_PROFILE", "default"),
            incus_command_timeout_seconds=int(
                os.getenv("INCUS_COMMAND_TIMEOUT_SECONDS", "90")
            ),
            incus_max_attempts=int(os.getenv("INCUS_MAX_ATTEMPTS", "3")),
            current_image_version=os.getenv("CURRENT_IMAGE_VERSION", "dst-base-v1"),
            current_image_verified=_bool("CURRENT_IMAGE_VERIFIED", False),
            default_node_name=os.getenv("DEFAULT_NODE_NAME", "local-node"),
            default_node_max_active_slots=int(
                os.getenv("DEFAULT_NODE_MAX_ACTIVE_SLOTS", "2")
            ),
            default_node_secret=os.getenv("DEFAULT_NODE_SECRET") or None,
            scheduler_interval_seconds=float(
                os.getenv("SCHEDULER_INTERVAL_SECONDS", "5")
            ),
            job_poll_interval_seconds=float(
                os.getenv("JOB_POLL_INTERVAL_SECONDS", "1")
            ),
            watchdog_interval_seconds=float(
                os.getenv("WATCHDOG_INTERVAL_SECONDS", "10")
            ),
            watchdog_stale_seconds=int(os.getenv("WATCHDOG_STALE_SECONDS", "60")),
            node_stale_seconds=int(os.getenv("NODE_STALE_SECONDS", "45")),
            job_lease_seconds=int(os.getenv("JOB_LEASE_SECONDS", "30")),
            slot_lease_seconds=int(os.getenv("SLOT_LEASE_SECONDS", "120")),
            retry_max_attempts=int(os.getenv("RETRY_MAX_ATTEMPTS", "3")),
            retry_base_seconds=int(os.getenv("RETRY_BASE_SECONDS", "2")),
            resource_sample_seconds=int(os.getenv("RESOURCE_SAMPLE_SECONDS", "60")),
            orchestrator_public_url=os.getenv(
                "ORCHESTRATOR_PUBLIC_URL", "http://127.0.0.1:8080"
            ),
            agent_protocol_version=int(os.getenv("AGENT_PROTOCOL_VERSION", "1")),
            app_version=os.getenv("APP_VERSION", "1.0.0"),
            metrics_enabled=_bool("METRICS_ENABLED", True),
            background_workers=_bool("BACKGROUND_WORKERS", True),
            runtime_view_provider=os.getenv(
                "RUNTIME_VIEW_PROVIDER", "disabled"
            ).lower(),
            view_session_ttl_seconds=int(os.getenv("VIEW_SESSION_TTL_SECONDS", "300")),
            runtime_display=os.getenv("RUNTIME_DISPLAY", os.getenv("DISPLAY", ":99")),
            runtime_worker_plugin=os.getenv("RUNTIME_WORKER_PLUGIN", "dst").lower(),
            scheduler_leader_lease_seconds=int(
                os.getenv("SCHEDULER_LEADER_LEASE_SECONDS", "15")
            ),
        )

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def validate(self) -> None:
        if self.environment not in {"development", "test", "production"}:
            raise ConfigurationError(
                "ENVIRONMENT must be development, test, or production"
            )
        if self.runtime_provider not in {"mock", "incus"}:
            raise ConfigurationError("RUNTIME_PROVIDER must be mock or incus")
        if self.runtime_view_provider not in {"disabled", "mock", "xpra"}:
            raise ConfigurationError(
                "RUNTIME_VIEW_PROVIDER must be disabled, mock, or xpra"
            )
        if self.runtime_worker_plugin not in {"noop", "dst"}:
            raise ConfigurationError("RUNTIME_WORKER_PLUGIN must be noop or dst")
        if self.default_node_max_active_slots < 1:
            raise ConfigurationError("DEFAULT_NODE_MAX_ACTIVE_SLOTS must be positive")
        if self.retry_max_attempts < 1 or self.incus_max_attempts < 1:
            raise ConfigurationError("retry limits must be positive")
        for name in (
            "session_ttl_seconds",
            "incus_command_timeout_seconds",
            "scheduler_interval_seconds",
            "job_poll_interval_seconds",
            "watchdog_interval_seconds",
            "watchdog_stale_seconds",
            "node_stale_seconds",
            "job_lease_seconds",
            "slot_lease_seconds",
            "retry_base_seconds",
            "resource_sample_seconds",
            "view_session_ttl_seconds",
            "scheduler_leader_lease_seconds",
        ):
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"{name} must be positive")
        if self.is_production:
            failures: list[str] = []
            if not self.database_url.startswith(
                ("postgresql://", "postgresql+psycopg://")
            ):
                failures.append("production requires PostgreSQL")
            if not self.secret_key:
                failures.append("DST_FARM_SECRET_KEY is required")
            if self.runtime_provider == "mock":
                failures.append("mock provider is forbidden")
            if self.runtime_view_provider == "mock":
                failures.append("mock VIEW provider is forbidden")
            if self.debug:
                failures.append("DEBUG must be false")
            if (
                self.admin_password.lower() in DEFAULT_ADMIN_PASSWORDS
                or len(self.admin_password) < 12
            ):
                failures.append(
                    "a non-default ADMIN_PASSWORD of at least 12 characters is required"
                )
            if not self.orchestrator_public_url.lower().startswith("https://"):
                failures.append("ORCHESTRATOR_PUBLIC_URL must use HTTPS")
            if failures:
                raise ConfigurationError(
                    "Unsafe production configuration: " + "; ".join(failures)
                )

    def ensure_local_dirs(self) -> None:
        if not self.is_sqlite:
            return
        path = self.database_url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
