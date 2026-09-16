"""Collect a local hardware report (CPU / RAM / GPU / disk) as JSON.

Usage:
    python scripts/hardware_report.py [--json]
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<error: {exc}>"


def cpu_info() -> dict:
    info = {
        "processor": platform.processor(),
        "machine": platform.machine(),
        "logical_cores": os.cpu_count(),
    }
    ps = (
        "Get-CimInstance Win32_Processor | "
        "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed | "
        "ConvertTo-Json -Compress"
    )
    raw = _run(["powershell", "-NoProfile", "-Command", ps])
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            data = data[0]
        info.update({
            "name": data.get("Name", "").strip(),
            "physical_cores": data.get("NumberOfCores"),
            "max_clock_mhz": data.get("MaxClockSpeed"),
        })
    except Exception:  # noqa: BLE001
        info["name"] = "<unavailable>"
    return info


def ram_info() -> dict:
    ps = (
        "Get-CimInstance Win32_ComputerSystem | Select-Object TotalPhysicalMemory | "
        "ConvertTo-Json -Compress"
    )
    total_bytes = None
    raw = _run(["powershell", "-NoProfile", "-Command", ps])
    try:
        total_bytes = int(json.loads(raw)["TotalPhysicalMemory"])
    except Exception:  # noqa: BLE001
        pass

    modules = []
    ps2 = (
        "Get-CimInstance Win32_PhysicalMemory | "
        "Select-Object Capacity,Speed,Manufacturer,PartNumber | ConvertTo-Json -Compress"
    )
    raw2 = _run(["powershell", "-NoProfile", "-Command", ps2])
    try:
        mods = json.loads(raw2)
        if isinstance(mods, dict):
            mods = [mods]
        for m in mods:
            modules.append({
                "capacity_gb": round(int(m.get("Capacity", 0)) / 1024**3, 1),
                "speed_mts": m.get("Speed"),
                "part": (m.get("PartNumber") or "").strip(),
            })
    except Exception:  # noqa: BLE001
        pass

    return {
        "total_gb": round(total_bytes / 1024**3, 1) if total_bytes else None,
        "modules": modules,
    }


def gpu_info() -> list[dict]:
    if not shutil.which("nvidia-smi"):
        return [{"note": "nvidia-smi not found"}]
    raw = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ])
    gpus = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        gpus.append({
            "name": parts[0],
            "vram_total_mb": int(parts[1]),
            "vram_used_mb": int(parts[2]),
            "driver": parts[3],
            "compute_capability": parts[4],
        })
    cuda = _run(["nvidia-smi"]).split("\n")
    for line in cuda:
        if "CUDA Version" in line:
            gpus.append({"cuda_runtime": line.strip()})
            break
    return gpus


def disk_info() -> list[dict]:
    ps = (
        "Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | "
        "Select-Object DeviceID,Size,FreeSpace | ConvertTo-Json -Compress"
    )
    raw = _run(["powershell", "-NoProfile", "-Command", ps])
    disks = []
    try:
        items = json.loads(raw)
        if isinstance(items, dict):
            items = [items]
        for d in items:
            disks.append({
                "drive": d.get("DeviceID"),
                "total_gb": round(int(d.get("Size", 0)) / 1024**3, 1),
                "free_gb": round(int(d.get("FreeSpace", 0)) / 1024**3, 1),
            })
    except Exception:  # noqa: BLE001
        pass
    return disks


def main() -> int:
    report = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "cpu": cpu_info(),
        "ram": ram_info(),
        "gpus": gpu_info(),
        "disks": disk_info(),
        "python": sys.version.split()[0],
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
