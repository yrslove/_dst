"""Validate operator-collected server-side player/machine coordinates.

The checker is read-only: it accepts one JSON evidence file and never connects
to a runtime or sends game input.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def _position(value: Any, label: str) -> tuple[float, float]:
    if not isinstance(value, dict):
        raise TypeError(f"{label} must be a JSON object")
    try:
        x = float(value["x"])
        z = float(value["z"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain numeric x and z") from exc
    if not math.isfinite(x) or not math.isfinite(z):
        raise ValueError(f"{label} coordinates must be finite")
    return x, z


def check(evidence: dict, gift_radius: float) -> dict:
    """Return a deterministic proximity result for one server-side snapshot."""
    if not isinstance(evidence.get("runtime_id"), int) or evidence["runtime_id"] < 1:
        raise ValueError("runtime_id must be a positive integer")
    if not isinstance(evidence.get("world_session_id"), str) or not evidence[
        "world_session_id"
    ]:
        raise ValueError("world_session_id is required")
    player = evidence.get("player")
    machine = evidence.get("researchlab")
    if not isinstance(player, dict) or player.get("alive") is not True:
        raise ValueError("player must be present and alive")
    if not isinstance(machine, dict) or machine.get("prefab") != "researchlab":
        raise ValueError("researchlab evidence must identify prefab researchlab")
    px, pz = _position(player, "player")
    mx, mz = _position(machine, "researchlab")
    if not math.isfinite(gift_radius) or gift_radius <= 0:
        raise ValueError("gift_radius must be a positive finite number")
    distance = math.hypot(px - mx, pz - mz)
    passed = distance <= gift_radius
    return {
        "status": "PASS" if passed else "FAIL",
        "SCIENCE_MACHINE_PROXIMITY": "PASS" if passed else "FAIL",
        "runtime_id": evidence["runtime_id"],
        "world_session_id": evidence["world_session_id"],
        "player": {"x": px, "z": pz, "alive": True},
        "researchlab": {
            "guid": machine.get("guid"),
            "prefab": "researchlab",
            "x": mx,
            "z": mz,
        },
        "distance": distance,
        "gift_radius": gift_radius,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("evidence", type=Path, help="server-side coordinate JSON")
    parser.add_argument(
        "--gift-radius", type=float, required=True,
        help="verified DST gift interaction radius in world units",
    )
    args = parser.parse_args()
    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        if not isinstance(evidence, dict):
            raise TypeError("evidence root must be a JSON object")
        result = check(evidence, args.gift_radius)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        result = {"status": "FAIL", "SCIENCE_MACHINE_PROXIMITY": "FAIL", "error": str(exc)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
