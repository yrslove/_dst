from app.domain.errors import ControlPlaneError
from app.models import ErrorCode


class BootstrapIncomplete(ControlPlaneError):
    code = ErrorCode.RUNTIME_BOOTSTRAP_INCOMPLETE
    retryable = True


class BootstrapFailed(ControlPlaneError):
    code = ErrorCode.RUNTIME_BOOTSTRAP_FAILED
    retryable = True
