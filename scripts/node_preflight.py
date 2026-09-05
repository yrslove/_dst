#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def command_check(name: str, command: list[str], *, timeout: int = 10) -> Check:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return Check(name, "FAIL", f"{command[0]} not installed")
    except subprocess.TimeoutExpired:
        return Check(name, "FAIL", "command timed out")
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return Check(
        name,
        "PASS" if result.returncode == 0 else "FAIL",
        detail[0][:100] if detail else "ok",
    )


def json_list_check(name: str, command: list[str]) -> Check:
    check = command_check(name, command)
    if check.status != "PASS":
        return check
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=10, check=False
        )
        values = json.loads(result.stdout or "[]")
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        return Check(name, "FAIL", "invalid command response")
    return Check(
        name,
        "PASS" if values else "FAIL",
        f"{len(values)} configured" if values else "none configured",
    )


def port_check(port: int) -> Check:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return Check(f"Port {port}", "WARN", f"not available: {exc}")
    finally:
        sock.close()
    return Check(f"Port {port}", "PASS", "available")


def run(args: argparse.Namespace) -> list[Check]:
    checks = [
        Check(
            "Linux",
            "PASS" if platform.system() == "Linux" else "FAIL",
            platform.platform(),
        ),
        Check(
            "Incus installed",
            "PASS" if shutil.which("incus") else "FAIL",
            shutil.which("incus") or "missing",
        ),
        command_check("Incus reachable", ["incus", "info"]),
        json_list_check("Storage", ["incus", "storage", "list", "--format", "json"]),
        json_list_check("Network", ["incus", "network", "list", "--format", "json"]),
        Check(
            "Cgroups v2",
            "PASS" if Path("/sys/fs/cgroup/cgroup.controllers").is_file() else "WARN",
            "/sys/fs/cgroup/cgroup.controllers",
        ),
    ]
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    checks.extend(
        [
            Check(
                "RAM",
                "PASS" if memory.available >= args.min_ram_gib * 1024**3 else "FAIL",
                f"{memory.available / 1024**3:.1f} GiB available",
            ),
            Check(
                "Disk",
                "PASS" if disk.free >= args.min_disk_gib * 1024**3 else "FAIL",
                f"{disk.free / 1024**3:.1f} GiB free",
            ),
        ]
    )
    dri = Path("/dev/dri")
    nvidia = shutil.which("nvidia-smi")
    gpu_present = dri.exists() or bool(nvidia)
    checks.append(
        Check(
            "GPU",
            "PASS" if gpu_present else "WARN",
            str(dri) if dri.exists() else nvidia or "not detected",
        )
    )
    if nvidia:
        checks.append(command_check("nvidia-smi", [nvidia, "-L"], timeout=5))
    checks.extend(port_check(port) for port in args.required_port)
    required_modules = ["httpx", "psutil"]
    missing = [
        name for name in required_modules if importlib.util.find_spec(name) is None
    ]
    checks.append(
        Check(
            "Python agent deps",
            "FAIL" if missing else "PASS",
            "missing: " + ", ".join(missing) if missing else "httpx, psutil",
        )
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Linux Incus NODE without inventing PASS results."
    )
    parser.add_argument("--min-ram-gib", type=float, default=4)
    parser.add_argument("--min-disk-gib", type=float, default=20)
    parser.add_argument("--required-port", type=int, action="append", default=[])
    args = parser.parse_args()
    checks = run(args)
    print("NODE PREFLIGHT\n")
    for check in checks:
        print(f"{check.name:<20} {check.status:<5} {check.detail}")
    verdict = (
        "FAIL"
        if any(item.status == "FAIL" for item in checks)
        else ("PARTIAL" if any(item.status == "WARN" for item in checks) else "PASS")
    )
    print(f"\nVERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
