import pytest

from app.config import Settings
from app.providers.base import (
    InstanceNotFound,
    InstanceTimeout,
    ProviderUnavailable,
    RuntimeDescriptor,
)
from app.providers.incus_cli import IncusCLIProvider
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


def test_incus_provisions_from_verified_image_source_ref(monkeypatch):
    provider = IncusCLIProvider(
        Settings(environment="test", runtime_provider="incus", incus_remote="local")
    )
    runtime = RuntimeDescriptor(
        id=1,
        account_id=1,
        node_id=1,
        external_id="dst-test-g1",
        provider="incus",
        incus_remote=None,
        image_version="logical-v2",
        image_source_ref="golden-base-v2",
        runtime_generation=1,
    )
    calls = []
    monkeypatch.setattr(
        provider,
        "_exists",
        lambda _runtime, name=None: name == "golden-base-v2",
    )
    monkeypatch.setattr(
        provider,
        "_run",
        lambda *args, **_kwargs: calls.append(args),
    )

    provider.ensure(runtime)

    assert calls == [("copy", "golden-base-v2", "dst-test-g1")]
