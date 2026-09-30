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
