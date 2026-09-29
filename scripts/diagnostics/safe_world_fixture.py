"""Attest/back up or restore the existing prepared Master fixture while stopped.

Provision through normal DST world load with canonical Master/worldgenoverride.lua,
then save through DST. This tool never generates a world or changes save contents.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

from app.runtime.world_profile import attest_fixture, verified_fixture
from scripts.diagnostics.pre_death_guard import incus_json, prepared_save


def backup(master: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Never replace a rollback archive. Includes the existing shard index,
    # player snapshots, world snapshots, settings, and safe fixture manifest.
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            archive.add(master, arcname="Master")


def restore(master: Path, archive_path: Path, *, rollback_path: Path) -> dict:
    with tempfile.TemporaryDirectory(
        dir=master.parent, prefix=".safe-restore-"
    ) as temp:
        staging = Path(temp)
        with tarfile.open(archive_path) as archive:
            members = archive.getmembers()
            if sum(m.size for m in members) > 512 * 1024 * 1024:
                raise ValueError("fixture restore exceeds bounded size")
            if any(
                not (m.isfile() or m.isdir())
                or not m.name.startswith("Master/")
                and m.name != "Master"
                for m in members
            ):
                raise ValueError("fixture archive contains unexpected member")
            archive.extractall(staging, filter="data")
        verified_fixture(staging / "Master")
        backup(master, rollback_path)
        # Guarded restore replaces only this prepared shard, never Steam/auth data.
        old = staging / "previous-Master"
        master.rename(old)
        try:
            (staging / "Master").rename(master)
            for p in [master, *master.rglob("*")]:
                owner = old.stat()
                os.chown(p, owner.st_uid, owner.st_gid)
            verified_fixture(master)
        except Exception:
            if master.exists():
                shutil.rmtree(master)
            old.rename(master)
            raise
        return verified_fixture(master)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="dst-000001-g1")
    parser.add_argument(
        "--storage-root",
        type=Path,
        default=Path("/var/lib/incus/storage-pools/default"),
    )
    parser.add_argument("--application-log", type=Path)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--restore", action="store_true")
    parser.add_argument("--rollback", type=Path)
    args = parser.parse_args()
    instance = next((i for i in incus_json("list") if i["name"] == args.instance), None)
    if not instance or instance["status"] != "Stopped":
        raise SystemExit("runtime must be stopped through managed lifecycle")
    root = args.storage_root / "containers" / args.instance / "rootfs"
    save, _, _ = prepared_save(root)
    master = save.parents[3]
    if args.restore:
        if not args.rollback:
            parser.error("--restore requires --rollback")
        result = restore(master, args.archive, rollback_path=args.rollback)
    else:
        if not args.application_log:
            parser.error(
                "attestation requires --application-log from the provisioning run"
            )
        if args.archive.exists():
            raise SystemExit("archive already exists; refusing to replace a fixture")
        result = attest_fixture(
            master, application_log=args.application_log.read_text()
        )
        backup(master, args.archive)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
