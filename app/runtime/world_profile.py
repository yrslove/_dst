"""Canonical built-in safe idle profile and its runtime reconciliation helpers."""

from __future__ import annotations

import hashlib
import json
import os
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
    path = clusters[0] / "worldgenoverride.lua"
    desired = render_profile(profile).encode("utf-8")
    existing = path.read_bytes() if path.is_file() else None
    changed = existing != desired
    if changed:
        _atomic_write(path, desired, mode=(path.stat().st_mode & 0o777) if path.exists() else 0o640)
    applied = path.read_bytes()
    applied_hash = hashlib.sha256(applied).hexdigest()
    expected_hash = hashlib.sha256(desired).hexdigest()
    if applied_hash != expected_hash:
        raise RuntimeError("safe world profile reconciliation verification failed")
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
        "status": "VERIFIED",
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


def read_process_evidence(*, evidence_path: Path = DEFAULT_EVIDENCE_PATH) -> dict | None:
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
