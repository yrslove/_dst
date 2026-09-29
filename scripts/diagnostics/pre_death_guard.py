"""Require a matching prepared-world Incus snapshot before a destructive death test."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


def incus_json(*args: str) -> object:
    return json.loads(subprocess.check_output(["incus", *args, "--format", "json"]))


def prepared_save(rootfs: Path) -> tuple[Path, bytes, tuple[float, float]]:
    clusters = tuple(
        rootfs.glob("home/dst/.klei/DoNotStarveTogether/*/Cluster_1/Master")
    )
    if len(clusters) != 1:
        raise ValueError(f"expected one Farm 01 Master, found {len(clusters)}")
    master = clusters[0]
    if not re.fullmatch(rb"\s*return\s*\{\s*\}\s*", (master / "modoverrides.lua").read_bytes()):
        raise ValueError("temporary mod remains enabled")
    saves = [
        path
        for path in (master / "save/session").glob("*/[0-9]*")
        if path.is_file() and path.suffix != ".meta"
    ]
    if not saves:
        raise ValueError("no Master save")
    save = max(saves, key=lambda path: (path.stat().st_mtime_ns, path.name))
    payload = save.read_bytes()
    record = re.search(
        rb'tablefunctions\["ents_researchlab_fn"\] = function\(\)\s*'
        rb'return \{\{x=([-\d.]+),z=([-\d.]+)\}\}\s*end',
        payload,
    )
    if record is None or payload.count(b'tablefunctions["ents_researchlab_fn"] = function()') != 1:
        raise ValueError(f"{save}: expected exactly one Science Machine record")
    return save, payload, (float(record[1]), float(record[2]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="dst-000001-g1")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--enable-controlled-death", action="store_true")
    parser.add_argument(
        "--storage-root", type=Path, default=Path("/var/lib/incus/storage-pools/default")
    )
    args = parser.parse_args()
    instances = incus_json("list")
    instance = next((item for item in instances if item["name"] == args.instance), None)
    if instance is None or instance["status"] != "Stopped":
        raise SystemExit("runtime must be stopped through the managed lifecycle")
    snapshots = incus_json("snapshot", "list", args.instance)
    if args.snapshot not in {item["name"] for item in snapshots}:
        raise SystemExit(f"missing Incus snapshot: {args.snapshot}")
    live_root = args.storage_root / "containers" / args.instance / "rootfs"
    snapshot_root = (
        args.storage_root / "containers-snapshots" / args.instance / args.snapshot / "rootfs"
    )
    try:
        current_save, current_bytes, current_position = prepared_save(live_root)
        snapshot_save, snapshot_bytes, snapshot_position = prepared_save(snapshot_root)
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    if current_bytes != snapshot_bytes or current_position != snapshot_position:
        raise SystemExit("current prepared save differs from the pre-death snapshot")
    if args.enable_controlled_death:
        mod = (
            live_root
            / "home/dst/.steam/debian-installation/steamapps/common"
            / "Don't Starve Together/mods/controlled_death_fixture/modmain.lua"
        )
        if not mod.is_file():
            raise SystemExit("controlled death fixture is not installed")
        (current_save.parents[3] / "modoverrides.lua").write_text(
            'return { ["controlled_death_fixture"] = '
            '{ enabled = true, configuration_options = {} } }\n'
        )
    print(
        json.dumps(
            {
                "ready": True,
                "instance": args.instance,
                "snapshot": args.snapshot,
                "save": current_save.name,
                "snapshot_save": snapshot_save.name,
                "science_machine": current_position,
                "save_sha256": hashlib.sha256(current_bytes).hexdigest(),
                "controlled_death_enabled": args.enable_controlled_death,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
