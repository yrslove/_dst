import pytest

from app.domain.state import InvalidStateTransition, transition_runtime
from app.models import RuntimeInstance, RuntimeState


def runtime(state: RuntimeState) -> RuntimeInstance:
    return RuntimeInstance(
        account_id=1,
        node_id=1,
        provider="mock",
        external_id="runtime",
        state=state,
        image_version="dst-base-v1",
    )


def test_valid_runtime_transition():
    value = runtime(RuntimeState.READY)
    assert transition_runtime(value, RuntimeState.STARTING)
    assert value.state == RuntimeState.STARTING


def test_invalid_destroyed_to_running():
    value = runtime(RuntimeState.DESTROYED)
    with pytest.raises(InvalidStateTransition):
        transition_runtime(value, RuntimeState.RUNNING)

