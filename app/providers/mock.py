from __future__ import annotations

import threading
import time
from collections import Counter

from app.providers.base import (
    InstanceNotFound,
    InstanceTimeout,
    ProviderStatus,
    ProviderUnavailable,
    ProvisionFailed,
    RuntimeDescriptor,
    RuntimeProvider,
)


class MockProvider(RuntimeProvider):
    """Thread-safe provider with deterministic failure injection for tests."""

    def __init__(self):
        self.states: dict[str, str] = {}
        self.failures: dict[str, list[Exception]] = {}
        self.calls: Counter[str] = Counter()
        self.delay_seconds = 0.0
        self.available = True
        self._lock = threading.RLock()

    def inject(self, operation: str, exception: Exception, *, times: int = 1) -> None:
        with self._lock:
            self.failures.setdefault(operation, []).extend([exception] * times)

    def _before(self, operation: str) -> None:
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        with self._lock:
            self.calls[operation] += 1
            if not self.available:
                raise ProviderUnavailable("mock node unavailable")
            failures = self.failures.get(operation, [])
            if failures:
                raise failures.pop(0)

    def ensure(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self._before("ensure")
        with self._lock:
            self.states.setdefault(runtime.external_id, "STOPPED")
        return ProviderStatus(
            "NEEDS_LOGIN", {"mock": True, "generation": runtime.runtime_generation}
        )

    def inspect(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self._before("inspect")
        with self._lock:
            if runtime.external_id not in self.states:
                raise InstanceNotFound(runtime.external_id)
            state = self.states[runtime.external_id]
        return ProviderStatus(state, {"mock": True})

    def start(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self._before("start")
        with self._lock:
            if runtime.external_id not in self.states:
                raise InstanceNotFound(runtime.external_id)
            self.states[runtime.external_id] = "RUNNING"
        return ProviderStatus("RUNNING", {"mock": True})

    def stop(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self._before("stop")
        with self._lock:
            if runtime.external_id not in self.states:
                raise InstanceNotFound(runtime.external_id)
            self.states[runtime.external_id] = "STOPPED"
        return ProviderStatus("STOPPED", {"mock": True})

    def restart(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self._before("restart")
        with self._lock:
            if runtime.external_id not in self.states:
                raise InstanceNotFound(runtime.external_id)
            self.states[runtime.external_id] = "RUNNING"
        return ProviderStatus("RUNNING", {"mock": True})

    def destroy(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> None:
        self._before("destroy")
        with self._lock:
            self.states.pop(runtime.external_id, None)

    def rebuild(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        self._before("rebuild")
        with self._lock:
            self.states[runtime.external_id] = "STOPPED"
        return ProviderStatus("NEEDS_LOGIN", {"mock": True})

    def healthcheck(self, *, correlation_id: str | None = None) -> bool:
        self._before("healthcheck")
        return self.available

    def execute(self, runtime, command, *, timeout=60, correlation_id=None):
        self._before("execute")
        if runtime.external_id not in self.states:
            raise InstanceNotFound(runtime.external_id)
        return ProviderStatus("EXITED", {"mock": True, "exit_code": 0, "stdout": ""})

    def put_file(self, runtime, path, content, *, mode=0o600, correlation_id=None):
        self._before("put_file")
        if runtime.external_id not in self.states:
            raise InstanceNotFound(runtime.external_id)


__all__ = [
    "InstanceTimeout",
    "MockProvider",
    "ProviderUnavailable",
    "ProvisionFailed",
]
