import pytest

from app.providers.base import (
    InstanceNotFound,
    InstanceTimeout,
    ProviderUnavailable,
    RuntimeDescriptor,
)
from app.providers.mock import MockProvider


@pytest.fixture
def runtime():
    return RuntimeDescriptor(
        id=1,
        account_id=1,
        node_id=1,
        external_id="dst-test-g1",
        provider="mock",
        incus_remote=None,
        image_version="dst-base-v1",
        runtime_generation=1,
    )


def test_mock_simulates_timeout_and_unavailable(runtime):
    provider = MockProvider()
    provider.ensure(runtime)
    provider.inject("start", InstanceTimeout("timeout"))
    with pytest.raises(InstanceTimeout):
        provider.start(runtime)
    provider.available = False
    with pytest.raises(ProviderUnavailable):
        provider.inspect(runtime)


def test_mock_reports_missing_container(runtime):
    with pytest.raises(InstanceNotFound):
        MockProvider().start(runtime)

