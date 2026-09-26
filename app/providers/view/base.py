from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.providers.base import RuntimeDescriptor
from app.runtime.display import DisplayEnvironment


class ViewUnavailable(RuntimeError):
    code = "REMOTE_VIEW_UNAVAILABLE"

    def __init__(
        self, message: str, *, backend_session_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.backend_session_id = backend_session_id


class ViewBackendUnavailable(ViewUnavailable):
    code = "REMOTE_VIEW_BACKEND_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class ViewStatus:
    status: str
    backend_session_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    upstream: tuple[str, int] | None = None

    @property
    def provider_session_id(self) -> str | None:
        return self.backend_session_id


class RuntimeViewProvider(ABC):
    name = "unknown"

    def reserve_session(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> str | None:
        """Return a side-effect-free durable ID when setup needs crash cleanup."""
        return None

    @abstractmethod
    def prepare_runtime(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> None: ...

    @abstractmethod
    def create_session(
        self,
        runtime: RuntimeDescriptor,
        display: DisplayEnvironment,
        *,
        backend_session_id: str | None = None,
    ) -> ViewStatus: ...

    @abstractmethod
    def status(self, backend_session_id: str) -> ViewStatus: ...

    def get_view_status(self, backend_session_id: str) -> ViewStatus:
        return self.status(backend_session_id)

    @abstractmethod
    def close_session(self, backend_session_id: str) -> None: ...

    @abstractmethod
    def cleanup_expired_sessions(self, backend_session_ids: list[str]) -> None: ...

    def resolve_upstream(self, backend_session_id: str) -> tuple[str, int]:
        status = self.status(backend_session_id)
        if status.status != "ACTIVE" or status.upstream is None:
            raise ViewUnavailable("remote view upstream is not active")
        return status.upstream
