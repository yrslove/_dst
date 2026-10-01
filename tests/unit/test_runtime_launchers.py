from __future__ import annotations

from types import SimpleNamespace

from runtime_agent import launchers


def test_dst_launcher_attaches_to_existing_game_without_launching_duplicate(
    monkeypatch, tmp_path
):
    game_dir = tmp_path / "game"
    binary = game_dir / "bin64" / "dontstarve_steam_x64"
    binary.parent.mkdir(parents=True)
    binary.touch()
    ready = tmp_path / "dst.ready"
    pids = iter(({1237}, {1237}, set()))
    monotonic = iter((0.0, 100.0, 106.0))
    launched = []

    monkeypatch.setenv("DST_GAME_DIR", str(game_dir))
    monkeypatch.setenv("DST_READY_FILE", str(ready))
    monkeypatch.setenv("DST_ADOPT_GAME_PID", "1237")
    monkeypatch.setattr(launchers, "_game_pids", lambda _binary: next(pids))
    monkeypatch.setattr(launchers.time, "monotonic", lambda: next(monotonic))
    monkeypatch.setattr(launchers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launchers.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        launchers.subprocess,
        "run",
        lambda command, **_kwargs: launched.append(command)
        or SimpleNamespace(returncode=0),
    )

    assert launchers.dst() == 1
    assert launched == [["xdotool", "search", "--onlyvisible", "--pid", "1237"]]
    assert not ready.exists()


def test_dst_launcher_rejects_missing_adopted_game_without_launching(monkeypatch, tmp_path):
    game_dir = tmp_path / "game"
    binary = game_dir / "bin64" / "dontstarve_steam_x64"
    binary.parent.mkdir(parents=True)
    binary.touch()
    launched = []
    monkeypatch.setenv("DST_GAME_DIR", str(game_dir))
    monkeypatch.setenv("DST_ADOPT_GAME_PID", "1237")
    monkeypatch.setattr(launchers, "_game_pids", lambda _binary: set())
    monkeypatch.setattr(
        launchers.subprocess,
        "run",
        lambda *args, **kwargs: launched.append((args, kwargs)),
    )

    assert launchers.dst() == 1
    assert launched == []


def test_steam_preserves_client_awaiting_login_then_clears_marker(monkeypatch, tmp_path):
    ready = tmp_path / "steam.ready"
    login = tmp_path / "steam.needs-login"
    monkeypatch.setenv("STEAM_READY_FILE", str(ready))
    monkeypatch.setenv("STEAM_NEEDS_LOGIN_FILE", str(login))
    monkeypatch.setattr(launchers.subprocess, "Popen", lambda *_args: None)
    monkeypatch.setattr(launchers.time, "monotonic", lambda: 1000.0)
    logins = iter((False, False, True, False))
    monkeypatch.setattr(launchers, "_new_logon", lambda *_args: (next(logins), 0))
    alive = iter((True, True, True, True, False))
    monkeypatch.setattr(launchers, "_steam_running", lambda: next(alive))
    checkpoints = []
    monkeypatch.setattr(launchers.time, "sleep", lambda _seconds: checkpoints.append((login.exists(), ready.exists())))
    # Expire startup after the first clock read.
    clock = iter((0.0, 1000.0, 1001.0))
    monkeypatch.setattr(launchers.time, "monotonic", lambda: next(clock))
    assert launchers.steam() == 1
    assert checkpoints == [(True, False), (True, False), (False, True)]
    assert not login.exists()
    assert not ready.exists()
