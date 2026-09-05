from __future__ import annotations

import uuid

from app.providers.base import RuntimeDescriptor
from app.providers.view.base import (
    RuntimeViewProvider,
    ViewBackendUnavailable,
    ViewStatus,
)
from app.runtime.display import DisplayEnvironment


class DisabledRuntimeViewProvider(RuntimeViewProvider):
    name = "disabled"

    def prepare_runtime(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> None:
        raise ViewBackendUnavailable("runtime VIEW provider is not configured")

    def status(self, backend_session_id: str) -> ViewStatus:
        return ViewStatus(
            "ERROR",
            backend_session_id,
            "REMOTE_VIEW_BACKEND_UNAVAILABLE",
            "runtime VIEW provider is not configured",
        )

    def create_session(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> ViewStatus:
        self.prepare_runtime(runtime, display)
        raise AssertionError("unreachable")

    def close_session(self, backend_session_id: str) -> None:
        return None

    def cleanup_expired_sessions(self, backend_session_ids: list[str]) -> None:
        return None


class MockRuntimeViewProvider(RuntimeViewProvider):
    name = "mock"

    def __init__(self):
        self.sessions: set[str] = set()

    def prepare_runtime(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> None:
        return None

    def status(self, backend_session_id: str) -> ViewStatus:
        return ViewStatus(
            "ACTIVE" if backend_session_id in self.sessions else "CLOSED",
            backend_session_id,
        )

    def create_session(
        self, runtime: RuntimeDescriptor, display: DisplayEnvironment
    ) -> ViewStatus:
        session_id = f"mock-view-{uuid.uuid4().hex}"
        self.sessions.add(session_id)
        return ViewStatus("ACTIVE", session_id)

    def close_session(self, backend_session_id: str) -> None:
        self.sessions.discard(backend_session_id)

    def cleanup_expired_sessions(self, backend_session_ids: list[str]) -> None:
        for value in backend_session_ids:
            self.close_session(value)
