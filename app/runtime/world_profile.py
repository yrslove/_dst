"""Canonical built-in safe idle profile and its runtime reconciliation helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# These are partial world-settings overrides supported by DST's
# worldgenoverride.lua layer. Keep the mapping stable and serialize keys sorted.
SAFE_PROFILE = {
    "day": "onlyday",
    "hunger": "nonlethal",
    "temperaturedamage": "nonlethal",
    "winter": "noseason",
    "spring": "noseason",
    "summer": "noseason",
    "hounds": "never",
    "shadowcreatures": "never",
    "weather": "never",
    "lightning": "never",
}

SAFE_PROFILE_VERSION = 1
FIXTURE_MANIFEST = "safe-world-fixture.json"

DEFAULT_USER_ROOT = Path("/home/dst/.klei/DoNotStarveTogether")
DEFAULT_EVIDENCE_PATH = Path("/run/dst-runtime/world-profile-evidence.json")


def render_profile(profile: dict[str, str] = SAFE_PROFILE) -> str:
    """Render one deterministic, partial DST user override."""
    overrides = "\n".join(
        f'        {key} = "{profile[key]}",' for key in sorted(profile)
    )
    return (
        "return {\n"
        "    override_enabled = true,\n"
        "    overrides = {\n"
        f"{overrides}\n"
        "    },\n"
        "}\n"
    )


def desired_profile_hash(profile: dict[str, str] = SAFE_PROFILE) -> str:
    return hashlib.sha256(render_profile(profile).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o640) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def reconcile_world_profile(
    *, user_root: Path = DEFAULT_USER_ROOT, profile: dict[str, str] = SAFE_PROFILE
) -> dict:
    """Converge the sole prepared Cluster_1 user override to the canonical bytes."""
    clusters = sorted(user_root.glob("*/Cluster_1"))
    if len(clusters) != 1 or not clusters[0].is_dir():
        raise RuntimeError("expected exactly one prepared Cluster_1 world")
    path = clusters[0] / "Master" / "worldgenoverride.lua"
    path.parent.mkdir(exist_ok=True)
    desired = render_profile(profile).encode("utf-8")
    existing = path.read_bytes() if path.is_file() else None
    changed = existing != desired
    if changed:
        _atomic_write(
            path,
            desired,
            mode=(path.stat().st_mode & 0o777) if path.exists() else 0o640,
        )
    applied = path.read_bytes()
    applied_hash = hashlib.sha256(applied).hexdigest()
    expected_hash = hashlib.sha256(desired).hexdigest()
    if applied_hash != expected_hash:
        raise RuntimeError("safe world profile reconciliation verification failed")
    legacy = clusters[0] / "worldgenoverride.lua"
    # Remove only the exact former orchestrator-owned file, not user content.
    if legacy.is_file() and legacy.read_bytes() == desired:
        legacy.unlink()
    return {
        "path": str(path),
        "changed": changed,
        "desired_profile_hash": expected_hash,
        "applied_profile_hash": applied_hash,
        "normalized_config": render_profile(profile),
    }


def record_process_evidence(
    *,
    reconciliation: dict,
    account_id: int,
    runtime_id: int,
    runtime_generation: int,
    process_id: int,
    process_start_ticks: int,
    process_started_at: str,
    evidence_path: Path = DEFAULT_EVIDENCE_PATH,
) -> dict:
    verified_at = datetime.now(UTC).isoformat()
    evidence = {
        "status": "CONFIG_PRESENT",
        "verification_scope": "CONFIG_FILE_AND_PROCESS",
        "profile_layer": "worldgenoverride.lua",
        "configuration_verified": True,
        "world_behavior_verified": False,
        "account_id": account_id,
        "runtime_id": runtime_id,
        "runtime_generation": runtime_generation,
        "world_path": reconciliation["path"],
        "desired_profile_hash": reconciliation["desired_profile_hash"],
        "applied_profile_hash": reconciliation["applied_profile_hash"],
        "config_written": bool(reconciliation["changed"]),
        "process_id": process_id,
        "process_start_ticks": process_start_ticks,
        "process_generation": (
            f"r{runtime_id}-g{runtime_generation}-p{process_id}-t{process_start_ticks}"
        ),
        "process_started_at": process_started_at,
        "verified_at": verified_at,
    }
    evidence_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write(
        evidence_path,
        (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode=0o600,
    )
    return evidence


def read_process_evidence(
    *, evidence_path: Path = DEFAULT_EVIDENCE_PATH
) -> dict | None:
    try:
        value = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def refresh_process_evidence(
    evidence: dict, *, evidence_path: Path = DEFAULT_EVIDENCE_PATH
) -> dict:
    refreshed = {**evidence, "verified_at": datetime.now(UTC).isoformat()}
    _atomic_write(
        evidence_path,
        (json.dumps(refreshed, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode=0o600,
    )
    return refreshed


def _table(source: str, field: str) -> str:
    """Read a balanced literal table, without executing save-file Lua."""
    match = re.search(r"\b" + re.escape(field) + r"\s*=\s*\{", source)
    if match is None:
        raise ValueError(f"missing saved table: {field}")
    start = match.end() - 1
    depth = 0
    quote = None
    escaped = False
    for i in range(start, len(source)):
        char = source[i]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start + 1 : i]
    raise ValueError(f"unterminated saved table: {field}")


def _fields(source: str) -> dict[str, str]:
    """Extract direct literal fields; nested tables are skipped, never evaluated."""
    depth = 0
    quoted = False
    escaped = False
    start = 0
    parts = []
    for i, char in enumerate(source + ","):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(source[start:i].strip())
            start = i + 1
    result = {}
    for part in parts:
        match = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*(.*)", part, re.DOTALL)
        if match:
            key, value = match.groups()
            if key in result:
                raise ValueError(f"duplicate saved field: {key}")
            result[key] = value.strip()
    return result


def saved_world(master: Path) -> dict:
    """Read settings and clock from the actual newest DST snapshot, fail closed."""
    sessions = [
        p
        for p in (master / "save/session").glob("*")
        if p.is_dir() and any(f.is_file() and f.name.isdecimal() for f in p.iterdir())
    ]
    if len(sessions) != 1:
        raise ValueError("expected exactly one prepared world session")
    path = max(
        (p for p in sessions[0].iterdir() if p.is_file() and p.name.isdecimal()),
        key=lambda p: int(p.name),
    )
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("prepared world save exceeds inspection limit")
    payload = path.read_bytes()
    source = payload.decode("utf-8")

    def block(name):
        match = re.search(
            r'tablefunctions\["'
            + name
            + r'_fn"\]\s*=\s*function\(\)\s*return\s*(.*?)\nend',
            source,
            re.DOTALL,
        )
        if match is None:
            raise ValueError(f"missing saved block: {name}")
        return match[1]

    meta = _fields(block("meta").strip()[1:-1])
    session_id = json.loads(meta["session_identifier"])
    if session_id != sessions[0].name:
        raise ValueError("save metadata/session directory mismatch")
    overrides = _fields(_table(_table(block("map"), "topology"), "overrides"))
    settings = {k: json.loads(overrides[k]) for k in SAFE_PROFILE}
    if settings != SAFE_PROFILE:
        raise ValueError("saved world settings do not match canonical safe profile")
    clock = _fields(_table(_table(block("world_network"), "clock"), "segs"))
    segments = {k: int(clock[k]) for k in ("day", "dusk", "night")}
    if segments != {"day": 16, "dusk": 0, "night": 0}:
        raise ValueError("persisted clock is not onlyday")
    return {
        "world_session_id": session_id,
        "save_path": str(path),
        "save_sha256": hashlib.sha256(payload).hexdigest(),
        "settings": settings,
        "clock_segments": segments,
        "settings_fingerprint": desired_profile_hash(settings),
    }


def attest_fixture(master: Path, *, application_log: str) -> dict:
    """One guarded provisioning step after DST applies and saves the profile."""
    world = saved_world(master)
    if not loaded_profile(application_log, world["world_session_id"]):
        raise ValueError("DST application/load markers missing for prepared world")
    path = master / FIXTURE_MANIFEST
    if path.exists():
        verified_fixture(master)
        return json.loads(path.read_text())
    manifest = {
        "profile_version": SAFE_PROFILE_VERSION,
        "desired_profile_hash": desired_profile_hash(),
        "world_session_id": world["world_session_id"],
        "baseline_save_sha256": world["save_sha256"],
        "settings_fingerprint": world["settings_fingerprint"],
        "application_method": "DST_SUPPORTED_EXISTING_WORLD_SETTINGS",
        "application_log_sha256": hashlib.sha256(application_log.encode()).hexdigest(),
        "attested_at": datetime.now(UTC).isoformat(),
    }
    _atomic_write(path, (json.dumps(manifest, sort_keys=True) + "\n").encode())
    owner = master.stat()
    os.chown(path, owner.st_uid, owner.st_gid)
    return manifest


def verified_fixture(master: Path) -> dict:
    manifest = json.loads((master / FIXTURE_MANIFEST).read_text())
    if not isinstance(manifest, dict):
        raise TypeError("invalid prepared safe world manifest")
    world = saved_world(master)
    if (
        manifest.get("profile_version") != SAFE_PROFILE_VERSION
        or manifest.get("desired_profile_hash") != desired_profile_hash()
        or manifest.get("settings_fingerprint") != world["settings_fingerprint"]
        or manifest.get("world_session_id") != world["world_session_id"]
    ):
        raise ValueError("stale or mismatched prepared safe world fixture")
    return {
        **world,
        "profile_version": SAFE_PROFILE_VERSION,
        "fixture_manifest_sha256": hashlib.sha256(
            (master / FIXTURE_MANIFEST).read_bytes()
        ).hexdigest(),
    }


def loaded_profile(log: str, session_id: str) -> bool:
    """Require settings application for the last loaded world, not an older world."""
    loads = list(re.finditer(r"Loading world: session/([A-F0-9]+)/[0-9]+", log))
    if not loads or loads[-1][1] != session_id:
        return False
    tail = log[loads[-1].end() :]
    applied = {}
    for key, value in re.findall(r"OVERRIDE: setting\s+(\w+)\s+to\s+(\w+)", tail):
        applied[key] = value
    return all(applied.get(k) == v for k, v in SAFE_PROFILE.items())


def config_profile_hash(path: Path) -> str:
    # DST SaveGame rewrites this USER file with the complete settings list.
    # Fingerprint the owned settings so a normal save is not mistaken for drift.
    source = path.read_text()
    if not re.search(r"\boverride_enabled\s*=\s*true\b", source):
        raise ValueError("user overrides disabled")
    fields = _fields(_table(source, "overrides"))
    profile = {k: json.loads(fields[k]) for k in SAFE_PROFILE}
    return desired_profile_hash(profile)
