"""Readiness-aware Linux launchers for the existing process supervisors.

The runtime agent owns these wrapper processes and their process groups. Markers
are scoped to each supervisor start by SteamProcess/DSTProcess before_start.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _mark_ready(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write("ready\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _steam_running() -> bool:
    uid = os.getuid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.joinpath("comm").read_text().strip() != "steam":
                continue
            if entry.stat().st_uid == uid:
                return True
        except (OSError, UnicodeError):
            continue
    return False


def _game_pids(binary: Path) -> set[int]:
    result: set[int] = set()
    uid = os.getuid()
    binary = binary.resolve()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid == uid and entry.joinpath("exe").resolve() == binary:
                result.add(int(entry.name))
        except (OSError, RuntimeError):
            continue
    return result


def _new_logon(log: Path, offset: int) -> tuple[bool, int]:
    try:
        with log.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            if size < offset:
                offset = 0  # Steam rotated its log.
            stream.seek(offset)
            data = stream.read(1024 * 1024)
            offset = stream.tell()
    except OSError:
        return False, offset
    return b"RecvMsgClientLogOnResponse() : processing complete" in data, offset


def steam() -> int:
    log = Path.home() / ".steam/steam/logs/connection_log.txt"
    try:
        offset = log.stat().st_size
    except OSError:
        offset = 0
    subprocess.Popen(["/usr/games/steam", "-silent"])
    marker = Path(os.getenv("STEAM_READY_FILE", "/run/dst-runtime/steam.ready"))
    ready = False
    deadline = time.monotonic() + 90
    while True:
        logged_on, offset = _new_logon(log, offset)
        if logged_on and _steam_running():
            _mark_ready(marker)
            ready = True
        if ready and not _steam_running():
            marker.unlink(missing_ok=True)
            return 1
        if not ready and time.monotonic() > deadline:
            return 1
        time.sleep(1)


def dst() -> int:
    game_dir = Path(
        os.getenv(
            "DST_GAME_DIR",
            str(Path.home() / ".steam/steam/steamapps/common/Don't Starve Together"),
        )
    )
    binary = game_dir / "bin64/dontstarve_steam_x64"
    if not binary.is_file():
        return 1
    adopted_pid_text = os.getenv("DST_ADOPT_GAME_PID")
    adopted_pid = None
    if adopted_pid_text:
        try:
            adopted_pid = int(adopted_pid_text)
        except ValueError:
            return 1
        if adopted_pid <= 1 or adopted_pid not in _game_pids(binary):
            return 1
    else:
        # Let the authenticated Steam client supply its runtime and API context.
        subprocess.run(
            ["/usr/games/steam", "steam://run/322330"],
            check=True,
            timeout=20,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    marker = Path(os.getenv("DST_READY_FILE", "/run/dst-runtime/dst.ready"))
    visible_since: float | None = None
    game_pid: int | None = adopted_pid
    deadline = time.monotonic() + 120

    def stop_game(*_args: object) -> None:
        marker.unlink(missing_ok=True)
        if game_pid is not None:
            try:
                os.kill(game_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_game)
    while True:
        pids = _game_pids(binary)
        if game_pid is None:
            if pids:
                game_pid = min(pids)
            elif time.monotonic() > deadline:
                return 1
            else:
                time.sleep(1)
                continue
        if game_pid not in pids:
            marker.unlink(missing_ok=True)
            return 1
        visible = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--pid", str(game_pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
        ).returncode == 0
        if visible:
            visible_since = visible_since or time.monotonic()
            if time.monotonic() - visible_since >= 5 and not marker.exists():
                _mark_ready(marker)
        else:
            visible_since = None
            marker.unlink(missing_ok=True)
        time.sleep(1)


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"steam", "dst"}:
        raise SystemExit("usage: python -m runtime_agent.launchers {steam|dst}")
    raise SystemExit(steam() if sys.argv[1] == "steam" else dst())
