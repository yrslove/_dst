from __future__ import annotations

import os
import subprocess
from pathlib import Path

import psutil


def _gpu() -> dict:
    result = {
        "gpu_present": Path("/dev/dri").exists(),
        "gpu_utilization": None,
        "vram_used_bytes": None,
        "vram_total_bytes": None,
    }
    try:
        process = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return result
    if process.returncode != 0 or not process.stdout.strip():
        return result
    try:
        utilization, used_mib, total_mib = [
            float(value.strip()) for value in process.stdout.splitlines()[0].split(",")
        ]
    except (ValueError, IndexError):
        return result
    result.update(
        {
            "gpu_present": True,
            "gpu_utilization": utilization,
            "vram_used_bytes": int(used_mib * 1024 * 1024),
            "vram_total_bytes": int(total_mib * 1024 * 1024),
        }
    )
    return result


def collect_resources() -> dict:
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    try:
        load_1, load_5, load_15 = os.getloadavg()
    except (AttributeError, OSError):
        load_1 = load_5 = load_15 = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "load_1": load_1,
        "load_5": load_5,
        "load_15": load_15,
        "ram_used_bytes": memory.used,
        "ram_total_bytes": memory.total,
        "disk_used_bytes": disk.used,
        "disk_total_bytes": disk.total,
        **_gpu(),
    }
