from __future__ import annotations

import platform


def discover(incus_available: bool) -> dict:
    """Conservative node capabilities: unknown is safer than an inferred pass."""
    linux = platform.system() == "Linux"
    return {
        "incus_available": incus_available,
        "display_backend_supported": "xvfb" if linux else "UNKNOWN",
        "gpu_type": "UNKNOWN",
        "gpu_available": "UNKNOWN",
        "remote_view_backend": "UNKNOWN",
        "platform": platform.system(),
        "validation": "UNVALIDATED_ON_REAL_NODE",
    }
