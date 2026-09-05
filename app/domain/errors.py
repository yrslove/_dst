from __future__ import annotations

from app.models import ErrorCode


class ControlPlaneError(RuntimeError):
    code = ErrorCode.UNKNOWN
    retryable = False
    status_code = 409

    def __init__(self, message: str = ""):
        super().__init__(message or self.code.value)


class NotFound(ControlPlaneError):
    status_code = 404


class AccountNotFound(NotFound):
    code = ErrorCode.RUNTIME_NOT_FOUND


class RuntimeNotFound(NotFound):
    code = ErrorCode.RUNTIME_NOT_FOUND


class NoCapacity(ControlPlaneError):
    code = ErrorCode.NO_CAPACITY
    retryable = True


class NodeOffline(ControlPlaneError):
    code = ErrorCode.NODE_OFFLINE
    retryable = True


class AuthenticationRequired(ControlPlaneError):
    code = ErrorCode.AUTH_REQUIRED
    status_code = 401


class ProtocolMismatch(ControlPlaneError):
    code = ErrorCode.AGENT_PROTOCOL_MISMATCH
    status_code = 409


class NodeProtocolMismatch(ProtocolMismatch):
    code = ErrorCode.NODE_PROTOCOL_MISMATCH
