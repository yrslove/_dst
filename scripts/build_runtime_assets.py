"""Publish a versioned, credential-free binary cache from an offline installation.

Reads only an explicit allow-list of program assets. Never copies a Steam home.
The source runtime must stay stopped while publishing its installed binaries.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.runtime.content import GAME, STEAM_EXECUTABLES, executable

CLIENT_ASSETS = (
    "steam.sh",
    "steam",
    "steam_msg.sh",
    "steamdeps.txt",
    "bootstrap.tar.xz",
    "ubuntu12_32",
    "ubuntu12_64",
    "steamrt32",
    "steamrt64",
    "bin",
    "linux32",
    "linux64",
    "clientui",
    "steamui",
    "controller_base",
    "friends",
    "graphics",
    "legacycompat",
    "public",
    "resource",
    "tenfoot",
    "fontconfig",
    "steamclient.dll",
    "steamclient64.dll",
    "GameOverlayRenderer64.dll",
    "steam_subscriber_agreement.txt",
)


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        n
        for n in names
        if n.startswith(".")
        or n
        in {
            "pinned_libs_32",
            "pinned_libs_64",
            "libcurl_compat_32",
            "libcurl_compat_64",
            "var",
        }
    }


def _copy(source: Path, target: Path) -> None:
    if source.is_symlink():
        raise ValueError("top-level asset cannot be a symlink")
    if source.is_dir():
        shutil.copytree(source, target, symlinks=True, ignore=_ignore)
    else:
        shutil.copy2(source, target)


def _validate_links(root: Path) -> None:
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() and not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("cache contains a link outside published assets")


def build(source: Path, destination: Path) -> None:
    if destination.exists():
        raise ValueError("published versions are immutable; select a new destination")
    if not all(executable(source / p) for p in STEAM_EXECUTABLES):
        raise ValueError("source Steam client is incomplete")
    game = source / "steamapps/common" / GAME
    if (
        not executable(game / "bin64/dontstarve_steam_x64")
        or not (game / "data").is_dir()
    ):
        raise ValueError("source DST content is incomplete")
    text = (source / "steamapps/appmanifest_322330.acf").read_text()

    def field(name: str) -> str:
        match = re.search(r'"' + name + r'"\s+"([0-9]+)"', text)
        if not match:
            raise ValueError("missing content version metadata")
        return match[1]

    version = (source / "deb-installer/version").read_text().strip()
    if not re.fullmatch(r"[0-9.]+", version):
        raise ValueError("invalid Debian installer version")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=".runtime-assets-", dir=destination.parent)
    )
    try:
        client = temporary / "steam-client"
        client.mkdir()
        for name in CLIENT_ASSETS:
            asset = source / name
            if asset.exists():
                _copy(asset, client / name)
        # Package manifests/downloads are common client content; omit client metrics.
        (client / "package").mkdir()
        for asset in (source / "package").iterdir():
            if asset.name.endswith((".manifest", ".installed")) or ".zip" in asset.name:
                _copy(asset, client / "package" / asset.name)
        (temporary / "common").mkdir()
        _copy(game, temporary / "common" / GAME)
        _validate_links(temporary)
        # Construct fresh game metadata. No LastOwner, UserConfig, launch/session data.
        manifest = {
            "schema": 1,
            "appid": "322330",
            "buildid": field("buildid"),
            "size_on_disk": field("SizeOnDisk"),
            "installer_version": version,
        }
        (temporary / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        acf = '"AppState"\n{\n'
        for key, value in [
            ("appid", "322330"),
            ("Universe", "1"),
            ("name", GAME),
            ("StateFlags", "4"),
            ("installdir", GAME),
            ("buildid", manifest["buildid"]),
            ("SizeOnDisk", manifest["size_on_disk"]),
        ]:
            acf += f' "{key}" "{value}"\n'
        # Installed depot IDs and manifest IDs describe common binaries only.
        depots = re.search(
            r'"InstalledDepots"\s*\{(.*?)\n\s*\}\s*\n\s*\}', text, re.DOTALL
        )
        if depots:
            entries = re.findall(
                r'"([0-9]+)"\s*\{\s*"manifest"\s*"([0-9]+)"\s*"size"\s*"([0-9]+)"\s*\}',
                depots.group(0),
            )
            acf += ' "InstalledDepots"\n {\n'
            for depot, depot_manifest, size in entries:
                acf += (
                    f' "{depot}" {{ "manifest" "{depot_manifest}" "size" "{size}" }}\n'
                )
            acf += " }\n"
        (temporary / "appmanifest_322330.acf").write_text(acf + "}\n")
        # Unprivileged container IDs must be able to read host-owned mounted assets.
        for directory, dirs, files in os.walk(temporary):
            Path(directory).chmod(0o755)
            for name in files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steam-install", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    build(args.steam_install, args.destination)
    print("Credential-free runtime assets published")


if __name__ == "__main__":
    main()
