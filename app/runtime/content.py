"""Idempotent private Steam bootstrap from a credential-free, read-only node cache.

This stdlib-only file is also injected into guests before the agent is started.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import shutil
from pathlib import Path

ASSETS = Path("/opt/dst-runtime-assets")
GAME = "Don't Starve Together"
STEAM_EXECUTABLES = (
    "steam.sh",
    "ubuntu12_32/steam",
    "ubuntu12_32/steam-runtime/run.sh",
    "ubuntu12_32/steam-runtime/setup.sh",
)


def executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def prerequisites(home: Path, *, require_dst: bool = True) -> bool:
    steam = home / ".steam/steam"
    return all(executable(steam / p) for p in STEAM_EXECUTABLES) and (
        not require_dst
        or (
            executable(steam / "steamapps/common" / GAME / "bin64/dontstarve_steam_x64")
            and (steam / "steamapps/appmanifest_322330.acf").is_file()
            and (steam / "steamapps/libraryfolders.vdf").is_file()
        )
    )


def _link(link: Path, target: Path) -> None:
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        raise RuntimeError(f"conflicting runtime link: {link}")
    if link.exists():
        if link.resolve() == target.resolve():
            return
        raise RuntimeError(f"existing runtime path must be preserved: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)


def _write_missing(path: Path, content: str) -> None:
    # Never replace account-owned Steam metadata on subsequent reconcile.
    if path.exists():
        return
    temporary = path.with_name(path.name + ".bootstrap-tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def prepare(home: Path, assets: Path, stage: str, *, uid: int, gid: int) -> None:
    manifest = json.loads((assets / "manifest.json").read_text())
    if manifest.get("schema") != 1 or not str(manifest.get("buildid", "")).isdecimal():
        raise RuntimeError("invalid credential-free runtime asset manifest")
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o750)
    control = home / ".steam"
    control.mkdir(exist_ok=True)
    steam_link = control / "steam"
    # Existing authenticated layouts are preserved; fresh runtimes use Debian's path.
    steam = (
        steam_link.resolve() if steam_link.exists() else control / "debian-installation"
    )
    steam.mkdir(parents=True, exist_ok=True)
    if stage == "steam":
        seed = assets / "steam-client"
        if not all(executable(seed / p) for p in STEAM_EXECUTABLES):
            raise RuntimeError("Steam client cache is incomplete")
        marker = steam / ".dst-client-seeded"
        if not all(executable(steam / p) for p in STEAM_EXECUTABLES):
            # Seed contains only program files, never account home/config/userdata.
            shutil.copytree(seed, steam, dirs_exist_ok=True, symlinks=True)
            for root, dirs, files in os.walk(steam, followlinks=False):
                os.chown(root, uid, gid)
                for name in dirs + files:
                    os.chown(Path(root) / name, uid, gid, follow_symlinks=False)
        for name in ("config", "userdata", "logs", "steamapps", "deb-installer"):
            (steam / name).mkdir(exist_ok=True)
        _write_missing(
            steam / "deb-installer/version", manifest["installer_version"] + "\n"
        )
        _link(steam_link, steam)
        _link(control / "root", steam)
        _link(home / ".local/share/Steam", steam)
        if not prerequisites(home, require_dst=False):
            raise RuntimeError("Steam runtime prerequisites are missing")
        _write_missing(marker, "1\n")
    elif stage == "dst":
        if not prerequisites(home, require_dst=False):
            raise RuntimeError("Steam runtime must be prepared before DST content")
        game = assets / "common" / GAME
        if (
            not executable(game / "bin64/dontstarve_steam_x64")
            or not (game / "data").is_dir()
        ):
            raise RuntimeError("DST executable/content cache is incomplete")
        apps = steam / "steamapps"
        (apps / "common").mkdir(parents=True, exist_ok=True)
        _link(apps / "common" / GAME, game)
        _write_missing(
            apps / "appmanifest_322330.acf",
            (assets / "appmanifest_322330.acf").read_text(),
        )
        _write_missing(
            apps / "libraryfolders.vdf",
            '"libraryfolders"\n{\n "0"\n {\n "path" "'
            + str(steam)
            + '"\n "label" ""\n "apps" { "322330" "'
            + str(manifest["size_on_disk"])
            + '" }\n }\n}\n',
        )
        if not prerequisites(home):
            raise RuntimeError("DST library prerequisites are missing")
    else:
        raise ValueError("unknown provisioning stage")
    # Repair only bootstrap-owned directory ownership; preserve Klei/world/evidence.
    for path in (home, control, steam, home / ".local", home / ".local/share"):
        if path.exists():
            os.chown(path, uid, gid)
    for path in steam.iterdir():
        os.chown(path, uid, gid, follow_symlinks=False)
    for path in (steam / "steamapps").iterdir():
        os.chown(path, uid, gid, follow_symlinks=False)
    for path in (
        steam_link,
        control / "root",
        home / ".local/share/Steam",
        steam / "steamapps/common" / GAME,
    ):
        if path.is_symlink():
            os.chown(path, uid, gid, follow_symlinks=False)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("steam", "dst"))
    args = parser.parse_args()
    user = pwd.getpwnam("dst")
    if user.pw_dir != "/home/dst" or user.pw_uid == 0:
        raise RuntimeError("invalid runtime user/home contract")
    prepare(Path(user.pw_dir), ASSETS, args.stage, uid=user.pw_uid, gid=user.pw_gid)
    print(args.stage.upper() + "_RUNTIME_PREPARED")


if __name__ == "__main__":
    main()
