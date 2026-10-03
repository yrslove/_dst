#!/usr/bin/env python3
"""Deploy the committed host runtime package into an existing Incus guest."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

INSTANCE_ROOT = "/opt/dst-orchestrator"
SERVICE = "dst-runtime-agent.service"
IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


class DeployError(RuntimeError):
    pass


def run(
    command: Sequence[str], *, cwd: Path | None = None, input_text: str | None = None
) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise DeployError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def assert_clean_tree(status_lines: Sequence[str]) -> None:
    dirty = [line for line in status_lines if line.strip() and line != "?? IO_REPORT.md"]
    if dirty:
        raise DeployError(
            "refusing to deploy a dirty checkout; commit or remove changes first: "
            + ", ".join(line[3:] for line in dirty)
        )


def runtime_files(repo: Path) -> list[Path]:
    """Return only Runtime Agent code and its runtime-required assets."""
    selected = {
        path
        for path in (repo / "runtime_agent").rglob("*.py")
        if not any(part in IGNORED_DIRS for part in path.parts)
    }
    asset_dir = repo / "runtime_agent/gameworker/dst/assets"
    try:
        manifest = json.loads((asset_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DeployError(f"cannot read Runtime Agent asset manifest: {exc}") from exc
    assets = {"manifest.json"}
    for entry in manifest.get("templates", []):
        filename = entry.get("filename")
        if not isinstance(filename, str):
            raise DeployError("asset manifest contains an invalid filename")
        relative = PurePosixPath(filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise DeployError(f"unsafe asset manifest filename: {filename}")
        assets.add(filename)
    for asset in assets:
        path = asset_dir / asset
        if not path.is_file() or path.is_symlink():
            raise DeployError(f"required runtime asset is missing or unsafe: {path}")
        selected.add(path)

    selected.update(
        (
            repo / "app/__init__.py",
            repo / "app/subprocess_env.py",
            repo / "app/runtime/__init__.py",
            repo / "app/runtime/display.py",
            repo / "app/runtime/world_profile.py",
        )
    )
    missing = [path for path in selected if not path.is_file()]
    if missing:
        raise DeployError("required runtime source missing: " + ", ".join(map(str, missing)))
    return sorted(selected, key=lambda path: path.relative_to(repo).as_posix())


def make_metadata(revision: str, files: Sequence[Path], repo: Path) -> dict[str, str | bool]:
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        raise DeployError(f"invalid Git revision: {revision}")
    digest = hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(repo).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return {
        "commit": revision,
        "deployed_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "dirty": False,
        "files_sha256": digest.hexdigest(),
    }


def parse_metadata(payload: str) -> dict[str, object]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DeployError("guest deployment metadata is invalid JSON") from exc
    revision = value.get("commit") if isinstance(value, dict) else None
    if not isinstance(revision, str) or len(revision) != 40:
        raise DeployError("guest deployment metadata has no valid commit")
    return value


def require_revision(actual: str, expected: str) -> None:
    if actual != expected:
        raise DeployError(f"guest revision mismatch: expected {expected}, got {actual}")


def agent_loaded_since(instance: str, revision: str, since: float) -> bool:
    # Older agents read DEPLOYMENT.json on every heartbeat. A new metadata
    # revision alone therefore cannot prove that reload actually completed.
    # Adoption reuses the existing PID, so it emits the explicit preservation
    # event instead of runtime_agent_started.
    output = run([
        "incus", "exec", instance, "--", "journalctl", "-u", SERVICE,
        "--since", f"@{since:.6f}", "--no-pager", "-o", "cat",
    ])
    return (f"runtime_agent_started deployed_revision={revision}" in output
            or "runtime_agent_reload_preserving_managed_processes" in output)


def write_archive(repo: Path, files: Sequence[Path], metadata: dict[str, object], destination: Path) -> str:
    inventory = {
        path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files
    }
    with tarfile.open(destination, "w:gz") as archive:
        for path in files:
            archive.add(path, arcname=path.relative_to(repo).as_posix(), recursive=False)
        for name, payload in (
            ("DEPLOYMENT.json", json.dumps(metadata, sort_keys=True).encode() + b"\n"),
            ("FILES.sha256.json", json.dumps(inventory, sort_keys=True).encode() + b"\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(payload))
    return hashlib.sha256(destination.read_bytes()).hexdigest()


GUEST_INSTALLER = r'''import hashlib, json, os, pathlib, shutil, signal, subprocess, sys, tarfile, tempfile
archive, root, expected, *options = sys.argv[1:]
cold = options == ["cold"]
root = pathlib.Path(root)
stage = root / (".deploy-stage-" + expected)
backup = root / (".deploy-backup-" + expected)
if stage.exists(): shutil.rmtree(stage)
if backup.exists(): shutil.rmtree(backup)
stage.mkdir(mode=0o700)
backup.mkdir(mode=0o700)
previous_metadata = root / "DEPLOYMENT.json"
previous_text = previous_metadata.read_text() if previous_metadata.is_file() else "NONE"
def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""): h.update(block)
    return h.hexdigest()
try:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        allowed = {m.name for m in members if m.isfile()}
        if any(m.issym() or m.islnk() or m.isdev() or m.name.startswith("/") or ".." in pathlib.PurePosixPath(m.name).parts for m in members):
            raise RuntimeError("unsafe archive member")
        bundle.extractall(stage, filter="data")
    metadata = json.loads((stage / "DEPLOYMENT.json").read_text())
    if metadata.get("commit") != expected or metadata.get("dirty") is not False:
        raise RuntimeError("staged revision metadata mismatch")
    inventory = json.loads((stage / "FILES.sha256.json").read_text())
    if set(inventory) != allowed - {"DEPLOYMENT.json", "FILES.sha256.json"}:
        raise RuntimeError("staged file inventory mismatch")
    for name, checksum in inventory.items():
        path = stage / name
        if not path.is_file() or digest(path) != checksum: raise RuntimeError("staged file checksum mismatch: " + name)
    pid = int(subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", "dst-runtime-agent.service"], text=True).strip())
    active = subprocess.call(["systemctl", "is-active", "--quiet", "dst-runtime-agent.service"]) == 0
    if active and pid <= 1: raise RuntimeError("active Runtime Agent has no MainPID")
    if not active and pid > 1: raise RuntimeError("Runtime Agent state is inconsistent")
    replacements = [("runtime_agent", "runtime_agent")]
    (backup / "app").mkdir()
    moved = []
    if pid > 1: os.kill(pid, signal.SIGSTOP)
    try:
        for source, destination in replacements:
            target = root / destination
            staged = stage / source
            old = backup / destination
            old.parent.mkdir(parents=True, exist_ok=True)
            if not target.is_dir() or not staged.is_dir(): raise RuntimeError("runtime package tree missing")
            os.replace(target, old)
            moved.append((target, old, False))
            os.replace(staged, target)
            moved[-1] = (target, old, True)
        for name in (
            "app/__init__.py",
            "app/subprocess_env.py",
            "app/runtime/__init__.py",
            "app/runtime/display.py",
            "app/runtime/world_profile.py",
        ):
            target, staged_file = root / name, stage / name
            if not staged_file.is_file() or (not cold and not target.is_file()): raise RuntimeError("runtime module missing: " + name)
            old = backup / name
            old.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file(): shutil.copy2(target, old)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged_file, target)
            moved.append((target, old, True))
        deployment = stage / "DEPLOYMENT.json"
        temporary = root / ".DEPLOYMENT.json.new"
        shutil.copyfile(deployment, temporary)
        if previous_text != "NONE": (backup / "DEPLOYMENT.json").write_text(previous_text)
        os.replace(temporary, root / "DEPLOYMENT.json")
    except Exception:
        for target, old, installed in reversed(moved):
            if installed:
                if target.is_dir(): shutil.rmtree(target)
                elif target.exists(): target.unlink()
            if old.is_dir(): os.replace(old, target)
            elif old.exists(): os.replace(old, target)
        if (backup / "DEPLOYMENT.json").is_file(): os.replace(backup / "DEPLOYMENT.json", root / "DEPLOYMENT.json")
        raise
    finally:
        if pid > 1: os.kill(pid, signal.SIGCONT)
    print(json.dumps({"previous_metadata": previous_text, "revision": expected, "agent_pid": pid, "backup": str(backup), "stage": str(stage)}))
except Exception:
    if stage.exists(): shutil.rmtree(stage)
    if backup.exists() and not any(backup.iterdir()): backup.rmdir()
    raise
'''

GUEST_PROBE = r'''import json, os, pathlib, shlex, subprocess, time
values = {}
for line in pathlib.Path("/etc/dst-runtime/agent.env").read_text().splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        key, value = line.split("=", 1)
        parsed = shlex.split(value)
        values[key] = parsed[0] if parsed else ""
processes = {}
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit(): continue
    try: name = (entry / "comm").read_text().strip()
    except OSError: continue
    if name in {"Xvfb", "steam"} or name.startswith("dontstarve"):
        processes[name] = int(entry.name)
pid = int(subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", "dst-runtime-agent.service"], text=True).strip())
ready = {name: pathlib.Path(path).is_file() for name, path in (("steam", values.get("STEAM_READY_FILE", "/run/dst-runtime/steam.ready")), ("dst", values.get("DST_READY_FILE", "/run/dst-runtime/dst.ready")))}
unit = pathlib.Path("/etc/systemd/system/dst-runtime-agent.service").read_text()
venv_python = pathlib.Path("/opt/dst-orchestrator/.venv/bin/python")
agent_state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[0] if pid > 1 else "X"
agent_wchan = pathlib.Path(f"/proc/{pid}/wchan").read_text().strip() if pid > 1 else "unknown"
agent_uid = pathlib.Path(f"/proc/{pid}").stat().st_uid if pid > 1 else int(subprocess.check_output(["id", "-u", "dst"], text=True).strip())
agent_environment = (pathlib.Path(f"/proc/{pid}/environ").read_bytes().split(bytes([0])) if pid > 1 else [])
agent_adoption = {item.split(b"=", 1)[0].decode(): item.split(b"=", 1)[1].decode() for item in agent_environment if (item.startswith(b"RUNTIME_ADOPT_") or item.startswith(b"DST_ADOPT_GAME_PID=")) and b"=" in item}
adoption = {}
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit(): continue
    try:
        args = [arg.decode(errors="replace") for arg in (entry / "cmdline").read_bytes().split(bytes([0])) if arg]
        stat = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
    except OSError: continue
    name = (entry / "comm").read_text().strip()
    role = "DISPLAY" if name == "Xvfb" else (args[-1].upper() if len(args) >= 4 and args[1:3] == ["-m", "runtime_agent.launchers"] and args[-1] in {"steam", "dst"} else None)
    identity_isolated = int(stat[2]) == int(entry.name) and int(stat[3]) == int(entry.name)
    same_user = (entry / "").stat().st_uid == agent_uid
    if role in {"DISPLAY", "STEAM", "DST"} and role not in adoption and stat[0] != "Z" and identity_isolated and same_user:
        adoption[role] = f"{entry.name}:{int(stat[19])}"
steam_pid = int(adoption.get("STEAM", "0:0").split(":", 1)[0])
def descends_from(child, ancestor):
    current = child
    visited = set()
    for _ in range(64):
        if current == ancestor: return True
        if current <= 1 or current in visited: return False
        visited.add(current)
        try: stat = pathlib.Path(f"/proc/{current}/stat").read_text().rsplit(") ", 1)[1].split()
        except OSError: return False
        current = int(stat[1])
    return False
dst_game_pid = None
for entry in pathlib.Path("/proc").iterdir():
    if not entry.name.isdigit(): continue
    try:
        executable = (entry / "exe").resolve()
        stat = (entry / "stat").read_text().rsplit(") ", 1)[1].split()
    except (OSError, RuntimeError): continue
    if executable.name == "dontstarve_steam_x64" and entry.stat().st_uid == agent_uid and descends_from(int(entry.name), steam_pid):
        dst_game_pid = int(entry.name)
        break
try: heartbeat = json.loads(pathlib.Path("/run/dst-runtime/heartbeat.json").read_text())
except (OSError, json.JSONDecodeError): heartbeat = {}
heartbeat_age = time.time() - heartbeat.get("timestamp_unix", 0) if isinstance(heartbeat, dict) else 999999
heartbeat_fresh = 0 <= heartbeat_age <= 30 and heartbeat.get("runtime_id") == int(values.get("RUNTIME_ID", "0")) and heartbeat.get("phase") == "GAME_READY" and heartbeat.get("healthy") is True
service_core_ok = venv_python.is_file() and "WorkingDirectory=/opt/dst-orchestrator" in unit and "ExecStart=/opt/dst-orchestrator/.venv/bin/python -m runtime_agent.main" in unit and "ExecReload=/bin/kill -HUP $MAINPID" in unit and "KillMode=mixed" in unit
print(json.dumps({"active": subprocess.call(["systemctl", "is-active", "--quiet", "dst-runtime-agent.service"]) == 0, "agent_pid": pid, "agent_state": agent_state, "agent_wchan": agent_wchan, "agent_adoption": agent_adoption, "runtime_id": values.get("RUNTIME_ID"), "adoption": adoption, "dst_game_pid": dst_game_pid, "heartbeat": heartbeat, "heartbeat_fresh": heartbeat_fresh, "worker_mode": values.get("WORKER_MODE", "DISABLED").strip('"'), "worker_autostart": values.get("WORKER_AUTOSTART", "0").strip('"'), "validation_flow": values.get("WORKER_VALIDATION_FLOW_ENABLED", "0").strip('"'), "validation_movement": values.get("WORKER_VALIDATION_MOVEMENT_ENABLED", "0").strip('"'), "processes": processes, "ready": ready, "service_core_ok": service_core_ok, "service_layout_ok": service_core_ok and "RuntimeDirectoryPreserve=restart" in unit}))
'''

GUEST_RECOVER_STALLED_AGENT = r'''import json, os, pathlib, pwd, subprocess, sys
service, raw = sys.argv[1:]
adoption = json.loads(raw)
game_pid = int(adoption.pop("dst_game_pid", 0))
if set(adoption) != {"DISPLAY", "STEAM"} or game_pid <= 1:
    raise RuntimeError("verified display, Steam, and live game identities are required")
assignments = []
for name, identity in adoption.items():
    pid, ticks = identity.split(":", 1)
    if not pid.isdigit() or not ticks.isdigit(): raise RuntimeError("invalid adoption identity")
    assignments.append(f"RUNTIME_ADOPT_{name}={identity}")
assignments.append(f"DST_ADOPT_GAME_PID={game_pid}")
subprocess.run(["systemctl", "unset-environment", "RUNTIME_ADOPT_DST"], check=True)
subprocess.run(["systemctl", "set-environment", *assignments], check=True)
runtime = pathlib.Path("/run/dst-runtime")
runtime.mkdir(mode=0o700, exist_ok=True)
os.chown(runtime, pwd.getpwnam("dst").pw_uid, pwd.getpwnam("dst").pw_gid)
dropin_dir = pathlib.Path("/run/systemd/system") / f"{service}.d"
dropin_dir.mkdir(parents=True, exist_ok=True)
dropin = dropin_dir / "90-stalled-agent-adoption.conf"
dropin.write_text("[Service]\nKillMode=process\nTimeoutStopSec=5\nRuntimeDirectoryPreserve=restart\n")
subprocess.run(["systemctl", "daemon-reload"], check=True)
active = subprocess.call(["systemctl", "is-active", "--quiet", service]) == 0
if active:
    subprocess.run(["systemctl", "restart", service], check=True, timeout=20)
else:
    subprocess.run(["systemctl", "reset-failed", service], check=True)
    subprocess.run(["systemctl", "start", service], check=True, timeout=20)
runtime.mkdir(mode=0o700, exist_ok=True)
os.chown(runtime, pwd.getpwnam("dst").pw_uid, pwd.getpwnam("dst").pw_gid)
auth = runtime / "Xauthority"
auth.touch(mode=0o600, exist_ok=True)
auth.chmod(0o600)
os.chown(auth, pwd.getpwnam("dst").pw_uid, pwd.getpwnam("dst").pw_gid)
steam_ready = runtime / "steam.ready"
steam_ready.touch()
steam_ready.chmod(0o600)
os.chown(steam_ready, pwd.getpwnam("dst").pw_uid, pwd.getpwnam("dst").pw_gid)
print(json.dumps({"main_pid": subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", service], text=True).strip(), "adoption": adoption}))
'''

GUEST_FINISH_STALLED_AGENT_RECOVERY = r'''import pathlib, subprocess, sys
service = sys.argv[1]
subprocess.run(["systemctl", "unset-environment", "RUNTIME_ADOPT_DISPLAY", "RUNTIME_ADOPT_STEAM", "RUNTIME_ADOPT_DST", "DST_ADOPT_GAME_PID"], check=True)
dropin = pathlib.Path("/run/systemd/system") / f"{service}.d/90-stalled-agent-adoption.conf"
dropin.unlink(missing_ok=True)
subprocess.run(["systemctl", "daemon-reload"], check=True)
'''


def guest_probe(
    instance: str, *, allow_inactive: bool = False, allow_starting: bool = False
) -> dict[str, object]:
    value = json.loads(
        run(["incus", "exec", instance, "--", "python3", "-c", GUEST_PROBE])
    )
    active = value.get("active")
    if active and value.get("agent_pid", 0) <= 1:
        raise DeployError("Runtime Agent service is not active with a valid MainPID")
    if not active and (not allow_inactive or value.get("agent_pid", 0) > 1):
        raise DeployError("Runtime Agent service is not active with a valid MainPID")
    if not value.get("service_layout_ok") and not (
        allow_inactive and value.get("service_core_ok")
    ):
        raise DeployError("installed venv or Runtime Agent service layout is incompatible")
    if active and value.get("agent_state") in {"T", "t", "Z", "X"}:
        raise DeployError("Runtime Agent MainPID is stopped or not runnable")
    if value.get("worker_mode") != "DISABLED" or value.get("worker_autostart") != "0":
        raise DeployError("GameWorker must remain DISABLED with autostart off for deployment")
    if value.get("validation_flow") not in {"0", "false", "False"} or value.get(
        "validation_movement"
    ) not in {"0", "false", "False"}:
        raise DeployError("validation flags must remain false during deployment")
    processes = value.get("processes", {})
    if not isinstance(processes, dict) or not {"Xvfb", "steam"}.issubset(processes):
        raise DeployError("expected healthy Xvfb and Steam processes were not found")
    if not value.get("dst_game_pid") or not any(name.startswith("dontstarve") for name in processes):
        raise DeployError("expected DST process was not found")
    if active and not allow_starting and value.get("ready") != {"steam": True, "dst": True}:
        raise DeployError("Steam/DST readiness markers are not both present")
    if not value.get("runtime_id"):
        raise DeployError("Runtime Agent runtime identity is missing")
    return value


def recover_stalled_agent(instance: str, probe: dict[str, object]) -> None:
    adoption = probe.get("adoption")
    inactive_safe = not probe.get("active") and probe.get("agent_pid") == 0
    stalled_safe = probe.get("agent_wchan") == "anon_pipe_read"
    if (not inactive_safe and not stalled_safe) or not isinstance(adoption, dict):
        raise DeployError("Runtime Agent did not respond to reload and no safe adoption recovery was proven")
    if set(adoption) != {"DISPLAY", "STEAM"} or not probe.get("dst_game_pid"):
        raise DeployError("cannot recover Runtime Agent without verified display, Steam, and game processes")
    adoption = {**adoption, "dst_game_pid": probe["dst_game_pid"]}
    run([
        "incus", "exec", instance, "--", "python3", "-c", GUEST_RECOVER_STALLED_AGENT,
        SERVICE, json.dumps(adoption, sort_keys=True),
    ])


def cold_probe(instance: str) -> dict[str, object]:
    """Install into an idle guest without touching an authenticated live session."""
    value = json.loads(run(["incus", "exec", instance, "--", "python3", "-c", GUEST_PROBE]))
    if value.get("active") or value.get("agent_pid", 0) or value.get("processes"):
        raise DeployError("cold deployment requires a stopped agent and no display/Steam/DST processes")
    if not value.get("runtime_id") or value.get("worker_mode") != "DISABLED" or value.get("worker_autostart") != "0":
        raise DeployError("cold deployment requires runtime identity and disabled worker")
    return value


def deploy(instance: str, repo: Path, *, cold: bool = False) -> None:
    status = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo
    )
    assert_clean_tree(status.splitlines())
    revision = run(["git", "rev-parse", "HEAD"], cwd=repo)
    files = runtime_files(repo)
    metadata = make_metadata(revision, files, repo)
    before = cold_probe(instance) if cold else guest_probe(instance, allow_inactive=True)
    with tempfile.TemporaryDirectory(prefix="dst-runtime-deploy-") as temporary:
        archive_path = Path(temporary) / f"runtime-{revision}.tar.gz"
        archive_digest = write_archive(repo, files, metadata, archive_path)
        remote_archive = f"/tmp/dst-runtime-{revision}.tar.gz"
        run(["incus", "file", "push", "--uid", "0", "--gid", "0", "--mode", "0600", str(archive_path), f"{instance}{remote_archive}"])
        previous_raw = run([
            "incus", "exec", instance, "--", "sh", "-c",
            f"test -f {INSTANCE_ROOT}/DEPLOYMENT.json && cat {INSTANCE_ROOT}/DEPLOYMENT.json || printf NONE",
        ])
        try:
            previous = str(parse_metadata(previous_raw)["commit"])
        except DeployError:
            previous = "NONE"
        print(f"host revision: {revision}")
        print(f"guest previous revision: {previous}")
        print(f"package sha256: {archive_digest}")
        try:
            result = run([
                "incus", "exec", instance, "--", "python3", "-c", GUEST_INSTALLER,
                remote_archive, INSTANCE_ROOT, revision, *(["cold"] if cold else []),
            ])
        except DeployError:
            run(["incus", "exec", instance, "--", "rm", "-f", remote_archive])
            raise
        installed = json.loads(result)
        run(["incus", "exec", instance, "--", "rm", "-f", remote_archive])
        print(f"guest staged revision: {installed['revision']}")
        try:
            previous = str(parse_metadata(installed["previous_metadata"])["commit"])
        except DeployError:
            previous = "NONE"
        print(f"guest previous revision: {previous}")
        # Install the matching unit before reload so restart preservation and
        # KillMode are in force for every future agent replacement.
        unit_source = repo / "deploy/systemd/dst-runtime-agent.service"
        unit_stage = f"/tmp/dst-runtime-agent-{revision}.service"
        run(["incus", "file", "push", "--uid", "0", "--gid", "0", "--mode", "0644", str(unit_source), f"{instance}{unit_stage}"])
        run(["incus", "exec", instance, "--", "install", "-o", "root", "-g", "root", "-m", "0644", unit_stage, "/etc/systemd/system/dst-runtime-agent.service"])
        run(["incus", "exec", instance, "--", "rm", "-f", unit_stage])
        run(["incus", "exec", instance, "--", "systemctl", "daemon-reload"])
        if cold:
            cold_probe(instance)
            current = run(["incus", "exec", instance, "--", "cat", f"{INSTANCE_ROOT}/DEPLOYMENT.json"])
            require_revision(str(parse_metadata(current)["commit"]), revision)
            run(["incus", "exec", instance, "--", "rm", "-rf", installed["backup"], installed["stage"]])
            print(f"guest deployed revision: {revision}; agent remains stopped")
            return
        # The agent is paused only for the tree swap. Its HUP path re-execs the
        # new code; an already failed agent is safely started through adoption.
        reload_started = time.time()
        adopted = False
        if before.get("active"):
            run(["incus", "exec", instance, "--", "systemctl", "reload", SERVICE])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                current = run(["incus", "exec", instance, "--", "cat", f"{INSTANCE_ROOT}/DEPLOYMENT.json"])
                require_revision(str(parse_metadata(current)["commit"]), revision)
                if run(["incus", "exec", instance, "--", "systemctl", "is-active", SERVICE]) == "active":
                    after = guest_probe(instance, allow_starting=True)
                    if after["processes"] != before["processes"]:
                        raise DeployError("managed Xvfb/Steam/DST process identities changed during agent reload")
                    heartbeat = after.get("heartbeat", {})
                    if (
                        after.get("heartbeat_fresh")
                        and heartbeat.get("revision") == revision
                        and heartbeat.get("timestamp_unix", 0) >= reload_started
                        and agent_loaded_since(instance, revision, reload_started)
                    ):
                        adopted = True
                        break
                time.sleep(2)
        if not adopted:
            stalled = guest_probe(instance, allow_inactive=True)
            recover_stalled_agent(instance, stalled)
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                after = guest_probe(instance, allow_starting=True)
                if after["processes"] != before["processes"]:
                    raise DeployError("managed Xvfb/Steam/DST process identities changed during stalled-agent recovery")
                heartbeat = after.get("heartbeat", {})
                if (
                    after.get("active")
                    and after.get("agent_adoption", {}).get("RUNTIME_ADOPT_DISPLAY") == after["adoption"].get("DISPLAY")
                    and after.get("agent_adoption", {}).get("RUNTIME_ADOPT_STEAM") == after["adoption"].get("STEAM")
                    and after.get("agent_adoption", {}).get("DST_ADOPT_GAME_PID") == str(after["dst_game_pid"])
                    and after.get("dst_game_pid") == before.get("dst_game_pid")
                    and after.get("heartbeat_fresh")
                    and heartbeat.get("revision") == revision
                    and heartbeat.get("timestamp_unix", 0) >= reload_started
                    and agent_loaded_since(instance, revision, reload_started)
                ):
                    run([
                        "incus", "exec", instance, "--", "python3", "-c",
                        GUEST_FINISH_STALLED_AGENT_RECOVERY, SERVICE,
                    ])
                    adopted = True
                    break
                time.sleep(2)
        if not adopted:
            raise DeployError("Runtime Agent did not prove healthy adoption of the deployed revision")
        run(["incus", "exec", instance, "--", "rm", "-rf", installed["backup"], installed["stage"]])
        print(f"guest deployed revision: {revision}")
        print("Runtime Agent health: fresh accepted GAME_READY heartbeat after managed-process adoption")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("instance", help="existing Incus instance name")
    parser.add_argument("--cold", action="store_true", help="install only into an idle guest; leave agent stopped")
    args = parser.parse_args(argv)
    try:
        deploy(args.instance, Path(__file__).resolve().parents[1], cold=args.cold)
    except (DeployError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"deployment failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
