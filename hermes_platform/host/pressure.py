"""Fresh host-pressure observations; callers decide how to handle read errors."""
import os
from pathlib import Path
import subprocess
import time

from hermes_platform.host.facts import os_family


def _sysctl_int(key: str) -> int:
    result = subprocess.run(["/usr/sbin/sysctl", "-n", key], check=True,
                            capture_output=True, text=True, timeout=1)
    return int(result.stdout.strip())


def sample_host_pressure(marker: Path) -> dict:
    """Read load, logical CPUs, macOS VM pressure and marker mtime on each call.

    Missing marker means no observed stall. Every other read failure propagates
    to the dispatcher's elevated-and-log fallback; no /proc behavior is changed.
    """
    load1 = os.getloadavg()[0]
    if os_family() == "darwin":
        cpus = _sysctl_int("hw.logicalcpu")
        vm = _sysctl_int("kern.memorystatus_vm_pressure_level")
    else:
        cpus, vm = os.cpu_count(), None
    try:
        age = max(0.0, time.time() - marker.stat().st_mtime)
    except FileNotFoundError:
        age = None
    return {"load1": load1, "logicalcpu": cpus, "vm": vm, "stall_age": age}
