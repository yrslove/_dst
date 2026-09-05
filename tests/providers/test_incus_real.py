import os
import platform
import uuid

import pytest

from app.config import Settings
from app.providers.base import RuntimeDescriptor
from app.providers.incus_cli import IncusCLIProvider

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_INCUS_TESTS") != "1" or platform.system() != "Linux",
    reason="real Incus integration is opt-in with RUN_INCUS_TESTS=1 on Linux",
)


def test_real_incus_lifecycle():
    settings = Settings(
        environment="test",
        runtime_provider="incus",
        incus_base_instance="",
        incus_image=os.getenv("INCUS_TEST_IMAGE", "images:alpine/3.20"),
        incus_max_attempts=1,
    )
    provider = IncusCLIProvider(settings)
    runtime = RuntimeDescriptor(
        id=1,
        account_id=1,
        node_id=1,
        external_id=f"dst-integration-{uuid.uuid4().hex[:12]}",
        provider="incus",
        incus_remote=os.getenv("INCUS_TEST_REMOTE", "local"),
        image_version="",
        runtime_generation=1,
    )
    try:
        provider.ensure(runtime)
        assert provider.inspect(runtime).state == "STOPPED"
        assert provider.start(runtime).state == "RUNNING"
        assert provider.restart(runtime).state == "RUNNING"
        assert provider.stop(runtime).state == "STOPPED"
    finally:
        provider.destroy(runtime)
