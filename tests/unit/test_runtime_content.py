from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.providers.base import ProviderStatus, ProvisionFailed, RuntimeDescriptor
from app.providers.incus_cli import IncusCLIProvider
from app.runtime.bootstrap import RuntimeBootstrapService
from app.runtime.bootstrap_models import BootstrapPhase, RuntimeAgentConfig
from app.runtime.content import GAME, STEAM_EXECUTABLES, prepare, prerequisites
from runtime_agent.process_supervisor import ProcessSupervisor
from runtime_agent.processes.steam import SteamProcess, SteamState
from scripts.build_runtime_assets import build
from tests.unit.test_runtime_agent import FakeProcess


def cache(tmp_path):
    assets = tmp_path / "assets"
    for name in STEAM_EXECUTABLES:
        path = assets / "steam-client" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("binary")
        path.chmod(0o755)
    binary = assets / "common" / GAME / "bin64/dontstarve_steam_x64"
    binary.parent.mkdir(parents=True)
    binary.write_text("game")
    binary.chmod(0o755)
    (binary.parent.parent / "data").mkdir()
    (assets / "manifest.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "buildid": "123",
                "installer_version": "1.0.0.79",
                "size_on_disk": "10",
            }
        )
    )
    (assets / "appmanifest_322330.acf").write_text('"AppState" { "appid" "322330" }')
    return assets


def provision(home, assets, stage):
    prepare(home, assets, stage, uid=os.getuid(), gid=os.getgid())


def test_missing_home_and_content_are_prepared_without_authentication(tmp_path):
    assets = cache(tmp_path)
    home = tmp_path / "new-account"
    assert not prerequisites(home)
    provision(home, assets, "steam")
    assert prerequisites(home, require_dst=False)
    assert not prerequisites(home)
    provision(home, assets, "dst")
    assert prerequisites(home)
    assert (home / ".local/share/Steam").resolve() == (home / ".steam/steam").resolve()
    assert (
        home / ".steam/steam/steamapps/common" / GAME
    ).resolve() == assets / "common" / GAME
    assert not any((home / ".steam/steam/config").iterdir())
    assert not any((home / ".steam/steam/userdata").iterdir())


def test_repeated_bootstrap_preserves_private_session_and_library(tmp_path):
    assets = cache(tmp_path)
    home = tmp_path / "account"
    provision(home, assets, "steam")
    provision(home, assets, "dst")
    session = home / ".steam/steam/config/loginusers.vdf"
    session.write_text("private login")
    library = home / ".steam/steam/steamapps/libraryfolders.vdf"
    before = library.stat().st_mtime_ns
    for _ in range(2):
        provision(home, assets, "steam")
        provision(home, assets, "dst")
    assert session.read_text() == "private login"
    assert library.stat().st_mtime_ns == before
    assert len(list((home / ".steam").iterdir())) == 3


def test_accounts_share_binaries_but_never_auth_userdata_or_klei(tmp_path):
    assets = cache(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    for home in (a, b):
        provision(home, assets, "steam")
        provision(home, assets, "dst")
    for rel in ("config/loginusers.vdf", "userdata/123/session", "ssfn123"):
        path = a / ".steam/steam" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("private")
        assert not (b / ".steam/steam" / rel).exists()
    (a / ".klei").mkdir()
    assert not (b / ".klei").exists()
    assert (a / ".steam/steam").resolve() != (b / ".steam/steam").resolve()
    assert (a / ".steam/steam/steamapps/common" / GAME).resolve() == (
        b / ".steam/steam/steamapps/common" / GAME
    ).resolve()


def test_missing_content_never_consumes_supervisor_crash_budget_then_needs_login(
    tmp_path,
):
    assets = cache(tmp_path)
    home = tmp_path / "new-account"
    supervisor = ProcessSupervisor(
        "steam", ("steam",), popen=lambda *_a, **_k: FakeProcess()
    )
    login = tmp_path / "steam.needs-login"
    steam = SteamProcess(
        supervisor,
        tmp_path / "steam.ready",
        needs_login_file=login,
        prerequisites_ready=lambda: prerequisites(home),
    )
    for _ in range(5):
        assert steam.start() == SteamState.STOPPED
        supervisor.tick()
    assert supervisor.status.pid is None
    assert supervisor.status.restart_count == 0
    assert not supervisor.status.exhausted
    provision(home, assets, "steam")
    assert steam.start() == SteamState.STOPPED
    provision(home, assets, "dst")
    steam.start()
    login.touch()  # Model launcher evidence of the real Steam login prompt.
    assert steam.status() == SteamState.NEEDS_LOGIN
    assert not supervisor.status.exhausted


def test_missing_cache_fails_before_agent_start_and_retains_bootstrap_phase(tmp_path):
    service = RuntimeBootstrapService(
        None,
        SimpleNamespace(
            execute=lambda *_a, **_k: (_ for _ in ()).throw(
                ProvisionFailed("cache missing")
            )
        ),
    )
    with pytest.raises(ProvisionFailed):
        service._apply(
            BootstrapPhase.DST_RUNTIME_PREPARED,
            SimpleNamespace(id=3),
            SimpleNamespace(steam_enabled=True),
            None,
        )
    with pytest.raises(FileNotFoundError):
        provision(tmp_path / "home", tmp_path / "missing-cache", "steam")
    assert not (tmp_path / "home").exists()


def test_cache_builder_copies_only_program_assets(tmp_path):
    assets = cache(tmp_path)
    source = assets / "steam-client"
    (source / "steamapps/common").mkdir(parents=True)
    (source / "steamapps/common" / GAME).symlink_to(
        assets / "common" / GAME, target_is_directory=True
    )
    (source / "steamapps/appmanifest_322330.acf").write_text(
        '"AppState" { "buildid" "123" "SizeOnDisk" "10" "LastOwner" "secret" }'
    )
    (source / "deb-installer").mkdir()
    (source / "deb-installer/version").write_text("1.0.0.79")
    (source / "package").mkdir()
    (source / "package/steam_client_metrics.bin").write_text("private metrics")
    for name in ("config", "userdata", "logs", "appcache"):
        (source / name).mkdir()
        (source / name / "private").write_text("secret")
    (source / "ssfn123").write_text("secret")
    # A content source must be a real installation directory.
    import shutil

    (source / "steamapps/common" / GAME).unlink()
    shutil.copytree(assets / "common" / GAME, source / "steamapps/common" / GAME)
    destination = tmp_path / "published"
    build(source, destination)
    assert all(
        executable.exists()
        for executable in [destination / "steam-client" / x for x in STEAM_EXECUTABLES]
    )
    assert not any(
        "secret" in p.read_text() for p in destination.rglob("*") if p.is_file()
    )
    assert not (destination / "steam-client/package/steam_client_metrics.bin").exists()
    with pytest.raises(ValueError, match="immutable"):
        build(source, destination)


def test_asset_mount_is_read_only_and_idempotent():
    provider = IncusCLIProvider(Settings())
    runtime = RuntimeDescriptor(
        3, 3, 1, "dst-000003-g1", "incus", "local", "dst-base-v1", 1
    )
    expected = {
        "type": "disk",
        "source": provider.settings.incus_runtime_assets,
        "path": "/opt/dst-runtime-assets",
        "readonly": "true",
    }
    calls = []
    provider.inspect = lambda *_a, **_k: ProviderStatus(
        "RUNNING", {"expanded_devices": {"runtime-assets": expected}}
    )
    provider._run = lambda *args, **_kwargs: calls.append(args)
    provider.prepare_assets(runtime)
    provider.prepare_assets(runtime)
    assert calls == []
    provider.inspect = lambda *_a, **_k: ProviderStatus(
        "RUNNING", {"expanded_devices": {}}
    )
    provider.prepare_assets(runtime)
    assert calls[0][-1] == "readonly=true"
    expected["readonly"] = "false"
    provider.inspect = lambda *_a, **_k: ProviderStatus(
        "RUNNING", {"expanded_devices": {"runtime-assets": expected}}
    )
    with pytest.raises(ProvisionFailed, match="conflicts"):
        provider.prepare_assets(runtime)


def test_bootstrap_content_failure_is_resumable_and_service_starts_last(client, app):
    from app.models import RuntimeInstance
    from app.runtime.bootstrap_errors import BootstrapFailed
    from tests.helpers import create_account

    account = create_account(client, "fresh-content-bootstrap")
    assert app.state.executor.execute_next()
    descriptor = app.state.executor._descriptor(account["runtime_id"])
    config = RuntimeAgentConfig(
        runtime_id=descriptor.id,
        account_id=descriptor.account_id,
        node_id=descriptor.node_id,
        runtime_token="test-token",
        orchestrator_url="http://localhost",
        protocol_version=1,
        heartbeat_interval=5,
        steam_enabled=True,
        dst_enabled=True,
    )
    commands = []
    original = app.state.provider.execute
    fail = True

    def execute(runtime, command, **kwargs):
        commands.append(command)
        if command[-1] == "dst" and fail:
            raise ProvisionFailed("missing content cache")
        return original(runtime, command, **kwargs)

    app.state.provider.execute = execute
    service = RuntimeBootstrapService(app.state.db, app.state.provider, version=9)
    with pytest.raises(BootstrapFailed):
        service.bootstrap(descriptor, config)
    assert not any("reload-or-restart" in c for c in commands)
    with app.state.db.session() as session:
        runtime = session.get(RuntimeInstance, descriptor.id)
        assert runtime.bootstrap_phase == BootstrapPhase.STEAM_RUNTIME_PREPARED
    fail = False
    commands.clear()
    assert service.bootstrap(descriptor, config) == BootstrapPhase.BOOTSTRAP_COMPLETE
    assert [c[-1] for c in commands[:2]] == ["steam", "dst"]
    assert "reload-or-restart" in commands[-1]
    commands.clear()
    service.bootstrap(descriptor, config)
    assert [c[-1] for c in commands] == ["steam", "dst"]
    assert not any("reload-or-restart" in c for c in commands)


def test_reconcile_repairs_deleted_client_binary_after_completed_seed(tmp_path):
    assets = cache(tmp_path)
    home = tmp_path / "account"
    provision(home, assets, "steam")
    provision(home, assets, "dst")
    session = home / ".steam/steam/config/loginusers.vdf"
    session.write_text("private")
    (home / ".steam/steam/ubuntu12_32/steam").unlink()
    assert not prerequisites(home)
    provision(home, assets, "steam")
    assert prerequisites(home)
    assert session.read_text() == "private"
