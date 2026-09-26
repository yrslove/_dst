#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def run_command(
    name: str, command: list[str], *, timeout: int = 20, warn: bool = False
) -> Check:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return Check(name, "FAIL", f"{command[0]} not installed")
    except subprocess.TimeoutExpired:
        return Check(name, "FAIL", "timed out")
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    status = "PASS" if result.returncode == 0 else ("WARN" if warn else "FAIL")
    return Check(
        name, status, detail[0][:120] if detail else f"exit={result.returncode}"
    )


def incus_exec(instance: str, *command: str) -> list[str]:
    return ["incus", "exec", instance, "--", *command]


def run(args: argparse.Namespace) -> list[Check]:
    instance = args.instance
    checks = [run_command("Container exists", ["incus", "info", instance])]
    try:
        listed = subprocess.run(
            ["incus", "list", instance, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        data = json.loads(listed.stdout or "[]") if listed.returncode == 0 else []
        running = bool(data and str(data[0].get("status", "")).upper() == "RUNNING")
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        running = False
    checks.append(
        Check(
            "Container boot",
            "PASS" if running else "FAIL",
            "RUNNING" if running else "not running",
        )
    )
    if not running:
        return checks
    checks.extend(
        [
            run_command(
                "Runtime agent",
                incus_exec(instance, "systemctl", "is-active", "dst-runtime-agent"),
            ),
            run_command(
                "Display",
                incus_exec(instance, "test", "-d", "/tmp/.X11-unix"),
            ),
            run_command(
                "Steam executable",
                incus_exec(
                    instance,
                    "sh",
                    "-lc",
                    "command -v steam || test -x /usr/games/steam",
                ),
            ),
            run_command(
                "Xpra HTML jQuery",
                incus_exec(
                    instance, "test", "-s", "/usr/share/xpra/www/js/lib/jquery.js"
                ),
            ),
            run_command(
                "Xpra system Pillow",
                incus_exec(instance, "python3", "-c", "import PIL"),
            ),
            run_command(
                "DST application",
                incus_exec(instance, "test", "-e", args.dst_path),
            ),
            run_command(
                "GPU visible",
                incus_exec(instance, "test", "-e", "/dev/dri"),
                warn=True,
            ),
            run_command(
                "Network",
                incus_exec(instance, "getent", "hosts", args.network_host),
            ),
        ]
    )
    if args.control_plane_url:
        probe = (
            "import urllib.request;"
            f"urllib.request.urlopen({args.control_plane_url.rstrip('/') + '/health/live'!r},timeout=5).read()"
        )
        checks.append(
            run_command(
                "Control-plane reachability",
                incus_exec(instance, "python3", "-c", probe),
            )
        )
    else:
        checks.append(
            Check(
                "Control-plane reachability", "WARN", "--control-plane-url not supplied"
            )
        )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate one Incus runtime; no gameplay behavior is tested."
    )
    parser.add_argument("instance")
    parser.add_argument(
        "--dst-path",
        default="/home/dst/.steam/steam/steamapps/common/Don't Starve Together/bin64/dontstarve_steam_x64",
    )
    parser.add_argument("--network-host", default="api.steampowered.com")
    parser.add_argument("--control-plane-url")
    args = parser.parse_args()
    checks = run(args)
    print("RUNTIME PREFLIGHT\n")
    for check in checks:
        print(f"{check.name:<28} {check.status:<5} {check.detail}")
    verdict = (
        "FAIL"
        if any(item.status == "FAIL" for item in checks)
        else ("PARTIAL" if any(item.status == "WARN" for item in checks) else "PASS")
    )
    print(f"\nVERDICT: {verdict}")
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
