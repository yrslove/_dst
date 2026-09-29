from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.deploy_runtime import (
    GUEST_INSTALLER,
    GUEST_PROBE,
    DeployError,
    assert_clean_tree,
    make_metadata,
    parse_metadata,
    require_revision,
    runtime_files,
)


def test_runtime_manifest_excludes_non_runtime_content() -> None:
    repo = Path(__file__).resolve().parents[2]

    paths = {path.relative_to(repo).as_posix() for path in runtime_files(repo)}

    assert "runtime_agent/main.py" in paths
    assert "runtime_agent/gameworker/dst/assets/manifest.json" in paths
    assert "runtime_agent/gameworker/dst/assets/main_menu_host_game.png" in paths
    assert "app/runtime/display.py" in paths
    assert "app/runtime/world_profile.py" in paths
    assert "app/subprocess_env.py" in paths
    assert "app/runtime/bootstrap.py" not in paths
    assert not any(
        forbidden in path
        for path in paths
        for forbidden in (
            ".git/",
            ".venv/",
            "tests/",
            "assets/samples/",
            "IO_REPORT.md",
            "recordings/",
            "world_saves/",
            "screenshots/",
        )
    )


def test_dirty_tree_guard_only_allows_known_untracked_io_report() -> None:
    assert_clean_tree(["?? IO_REPORT.md"])

    with pytest.raises(DeployError, match="dirty checkout"):
        assert_clean_tree([" M runtime_agent/main.py"])


def test_deployment_metadata_round_trips_commit_identity() -> None:
    revision = "a" * 40
    metadata = make_metadata(revision, [], Path("."))

    parsed = parse_metadata(json.dumps(metadata))

    assert parsed["commit"] == revision
    assert parsed["dirty"] is False
    assert isinstance(parsed["deployed_at_utc"], str)


def test_revision_verification_fails_closed_on_mismatch() -> None:
    with pytest.raises(DeployError, match="revision mismatch"):
        require_revision("a" * 40, "b" * 40)


def test_guest_metadata_without_a_commit_is_rejected() -> None:
    with pytest.raises(DeployError, match="no valid commit"):
        parse_metadata('{"dirty": false}')


def test_guest_helpers_are_valid_python() -> None:
    compile(GUEST_INSTALLER, "<guest-installer>", "exec")
    compile(GUEST_PROBE, "<guest-probe>", "exec")
    assert '"app/runtime/world_profile.py"' in GUEST_INSTALLER
