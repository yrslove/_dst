from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    id: int
    account_id: int
    node_id: int
    external_id: str
    provider: str
    incus_remote: str | None
    image_version: str
    runtime_generation: int
    network_profile: str | None = None


@dataclass(slots=True)
class ProviderStatus:
    state: str
    raw: dict[str, Any]


class ProviderError(RuntimeError):
    retryable = False
    code = "PROVIDER_FAILED"


class ProviderUnavailable(ProviderError):
    retryable = True
    code = "INCUS_UNAVAILABLE"


class InstanceNotFound(ProviderError):
    code = "RUNTIME_NOT_FOUND"


class InstanceAlreadyRunning(ProviderError):
    code = "INSTANCE_ALREADY_RUNNING"


class InstanceTimeout(ProviderError):
    retryable = True
    code = "RUNTIME_TIMEOUT"


class ProvisionFailed(ProviderError):
    retryable = True
    code = "PROVISION_FAILED"


class GPUUnavailable(ProviderError):
    code = "GPU_UNAVAILABLE"


class RuntimeProvider(ABC):
    @abstractmethod
    def ensure(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def inspect(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def start(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def stop(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def restart(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def destroy(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def rebuild(
        self, runtime: RuntimeDescriptor, *, correlation_id: str | None = None
    ) -> ProviderStatus:
        raise NotImplementedError

    @abstractmethod
    def healthcheck(self, *, correlation_id: str | None = None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def execute(
        self,
        runtime: RuntimeDescriptor,
        command: tuple[str, ...],
        *,
        timeout: int = 60,
        correlation_id: str | None = None,
    ) -> ProviderStatus:
        """Run an argv-only command inside a runtime; implementations must bound output."""
        raise NotImplementedError

    @abstractmethod
    def put_file(
        self,
        runtime: RuntimeDescriptor,
        path: str,
        content: bytes,
        *,
        mode: int = 0o600,
        correlation_id: str | None = None,
    ) -> None:
        """Inject a bounded configuration file without exposing content to logs."""
        raise NotImplementedError
